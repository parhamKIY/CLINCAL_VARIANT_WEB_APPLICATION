"""Bounded, on-demand access to public literature reference metadata."""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Literal, TypedDict, cast

import requests

from backend.provider_resilience import (
    ProviderRetryPolicy,
    ProviderTimeouts,
    call_provider_with_policy,
)
from backend.references import (
    CanonicalReferenceError,
    validate_canonical_reference,
)
from config import settings


MAX_REFERENCE_CONTENT_BYTES = 1_000_000
MAX_REFERENCE_TITLE_CHARS = 1_000
MAX_REFERENCE_ABSTRACT_CHARS = 12_000
MAX_REFERENCE_AUTHORS = 20
NCBI_REFERENCE_REQUEST_INTERVAL_SECONDS = 0.35
_NCBI_REFERENCE_LOCK = Lock()
_LAST_NCBI_REFERENCE_REQUEST_AT = 0.0

ReferenceContentStatus = Literal[
    "available",
    "no_match",
    "unavailable",
    "invalid_response",
    "unsupported",
]


class LiteratureReferenceContent(TypedDict):
    """Public, bounded article metadata loaded independently of report state."""

    status: ReferenceContentStatus
    failure_category: str | None
    provider: str
    identifier_type: str | None
    identifier: str | None
    pmid: str | None
    pmcid: str | None
    doi: str | None
    title: str | None
    authors: list[str]
    journal: str | None
    publication_date: str | None
    abstract: str | None


def _empty_content(
    reference: Mapping[str, object],
    *,
    status: ReferenceContentStatus,
    failure_category: str | None,
) -> LiteratureReferenceContent:
    return {
        "status": status,
        "failure_category": failure_category,
        "provider": "PubMed E-utilities",
        "identifier_type": (
            str(reference.get("identifier_type"))
            if reference.get("identifier_type") is not None
            else None
        ),
        "identifier": (
            str(reference.get("identifier"))
            if reference.get("identifier") is not None
            else None
        ),
        "pmid": None,
        "pmcid": None,
        "doi": None,
        "title": None,
        "authors": [],
        "journal": None,
        "publication_date": None,
        "abstract": None,
    }


def _bounded_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).strip()
    if not normalized or any(ord(character) < 32 for character in normalized):
        return None
    return normalized[:maximum]


def _element_text(element: ET.Element | None, maximum: int) -> str | None:
    if element is None:
        return None
    return _bounded_text("".join(element.itertext()), maximum)


def _wait_for_ncbi_reference_slot(sleep: Callable[[float], None]) -> None:
    global _LAST_NCBI_REFERENCE_REQUEST_AT
    with _NCBI_REFERENCE_LOCK:
        elapsed = time.monotonic() - _LAST_NCBI_REFERENCE_REQUEST_AT
        remaining = NCBI_REFERENCE_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            sleep(remaining)
        _LAST_NCBI_REFERENCE_REQUEST_AT = time.monotonic()


def _request(
    session: requests.Session,
    endpoint: str,
    *,
    params: dict[str, str | int],
    sleep: Callable[[float], None],
) -> tuple[object | None, str]:
    def operation(timeout: tuple[float, float], _attempt: int) -> object:
        _wait_for_ncbi_reference_slot(sleep)
        return session.get(
            f"{settings.PUBMED_BASE_URL}/{endpoint}",
            params=params,
            headers={
                "Accept": "application/json, application/xml, text/xml",
                "User-Agent": "ClinicalVariantInterpretation/0.1",
            },
            timeout=timeout,
            verify=True,
        )

    outcome = call_provider_with_policy(
        provider="pubmed_reference_content",
        operation_name=endpoint.removesuffix(".fcgi"),
        operation=operation,
        timeouts=ProviderTimeouts(
            connect=min(float(settings.PUBMED_TIMEOUT), 5.0),
            read=float(settings.PUBMED_TIMEOUT),
        ),
        retry_policy=ProviderRetryPolicy(
            max_attempts=min(
                settings.CONDITIONAL_ENRICHMENT_MAX_RETRIES + 1,
                2,
            ),
            backoff_base_seconds=0.5,
            backoff_max_seconds=2.0,
            max_retry_after_seconds=2.0,
        ),
        sleep=sleep,
    )
    return outcome.value, outcome.status


def _response_bytes(value: object) -> bytes:
    content = getattr(value, "content", None)
    if not isinstance(content, bytes) or not content:
        raise ValueError("Provider response body is unavailable.")
    if len(content) > MAX_REFERENCE_CONTENT_BYTES:
        raise ValueError("Provider response exceeds the bounded size limit.")
    return content


def _search_pmid(
    session: requests.Session,
    *,
    term: str,
    sleep: Callable[[float], None],
) -> tuple[str | None, str]:
    response, status = _request(
        session,
        "esearch.fcgi",
        params={
            "db": "pubmed",
            "retmode": "json",
            "retmax": 1,
            "term": term,
            "tool": "clinical_variant_app",
        },
        sleep=sleep,
    )
    if status != "success" or response is None:
        return None, status
    try:
        body = _response_bytes(response)
        payload = json.loads(body.decode("utf-8"))
        search = payload["esearchresult"]
        identifiers = search["idlist"]
        if not isinstance(identifiers, list):
            raise TypeError
        pmids = [item for item in identifiers if re.fullmatch(r"[1-9][0-9]{0,8}", str(item))]
    except (KeyError, TypeError, UnicodeDecodeError, ValueError):
        return None, "invalid_response"
    return (str(pmids[0]), "success") if pmids else (None, "no_match")


def _parse_pubmed_xml(
    value: object,
    *,
    expected_pmid: str,
) -> LiteratureReferenceContent:
    content = _response_bytes(value)
    if b"<!ENTITY" in content.upper():
        raise ValueError("Unsupported XML declaration.")
    root = ET.fromstring(content)
    article = root.find(".//PubmedArticle")
    if article is None:
        raise LookupError("No PubMed article was returned.")
    pmid = _element_text(article.find("./MedlineCitation/PMID"), 20)
    if pmid != expected_pmid:
        raise ValueError("PubMed returned a mismatched article identifier.")
    article_node = article.find("./MedlineCitation/Article")
    if article_node is None:
        raise ValueError("PubMed article metadata is unavailable.")

    authors: list[str] = []
    for author in article_node.findall("./AuthorList/Author")[:MAX_REFERENCE_AUTHORS]:
        collective = _element_text(author.find("./CollectiveName"), 200)
        last_name = _element_text(author.find("./LastName"), 120)
        initials = _element_text(author.find("./Initials"), 40)
        name = collective or " ".join(
            item for item in (last_name, initials) if item
        )
        if name:
            authors.append(name)

    abstract_parts: list[str] = []
    for part in article_node.findall("./Abstract/AbstractText"):
        text = _element_text(part, MAX_REFERENCE_ABSTRACT_CHARS)
        if text:
            label = _bounded_text(part.attrib.get("Label"), 80)
            abstract_parts.append(f"{label}: {text}" if label else text)

    pub_date = article_node.find("./Journal/JournalIssue/PubDate")
    publication_date = (
        _bounded_text(" ".join(pub_date.itertext()), 100)
        if pub_date is not None
        else None
    )
    identifiers: dict[str, str] = {}
    for item in article.findall("./PubmedData/ArticleIdList/ArticleId"):
        kind = str(item.attrib.get("IdType", "")).casefold()
        text = _element_text(item, 300)
        if kind and text:
            identifiers[kind] = text

    return {
        "status": "available",
        "failure_category": None,
        "provider": "PubMed E-utilities",
        "identifier_type": "PMID",
        "identifier": expected_pmid,
        "pmid": expected_pmid,
        "pmcid": identifiers.get("pmc") or identifiers.get("pmcid"),
        "doi": identifiers.get("doi"),
        "title": _element_text(
            article_node.find("./ArticleTitle"),
            MAX_REFERENCE_TITLE_CHARS,
        ),
        "authors": authors,
        "journal": _element_text(article_node.find("./Journal/Title"), 500),
        "publication_date": publication_date,
        "abstract": _bounded_text(
            " ".join(abstract_parts),
            MAX_REFERENCE_ABSTRACT_CHARS,
        ),
    }


def fetch_literature_reference_content(
    reference: Mapping[str, object],
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LiteratureReferenceContent:
    """Load a public article summary without changing retained report evidence."""

    try:
        canonical = validate_canonical_reference(reference)
    except CanonicalReferenceError:
        return _empty_content(
            reference,
            status="unsupported",
            failure_category="invalid_reference",
        )
    identifier_type = canonical["identifier_type"]
    identifier = canonical["identifier"]
    if identifier_type not in {"PMID", "PMCID", "DOI"} or not identifier:
        return _empty_content(
            canonical,
            status="unsupported",
            failure_category="unsupported_reference_type",
        )

    owns_session = session is None
    client = session or requests.Session()
    try:
        if identifier_type == "PMID":
            pmid = identifier
            lookup_status = "success"
        else:
            term = (
                f"{identifier}[PMCID]"
                if identifier_type == "PMCID"
                else f'"{identifier}"[AID]'
            )
            pmid, lookup_status = _search_pmid(
                client,
                term=term,
                sleep=sleep,
            )
        if lookup_status == "no_match" or (
            pmid is None and lookup_status == "success"
        ):
            return _empty_content(
                canonical,
                status="no_match",
                failure_category="reference_not_found",
            )
        if lookup_status != "success" or pmid is None:
            return _empty_content(
                canonical,
                status=(
                    "invalid_response"
                    if lookup_status == "invalid_response"
                    else "unavailable"
                ),
                failure_category=(
                    "invalid_provider_response"
                    if lookup_status == "invalid_response"
                    else "provider_unavailable"
                ),
            )

        response, fetch_status = _request(
            client,
            "efetch.fcgi",
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml",
                "tool": "clinical_variant_app",
            },
            sleep=sleep,
        )
        if fetch_status != "success" or response is None:
            return _empty_content(
                canonical,
                status="unavailable",
                failure_category="provider_unavailable",
            )
        try:
            parsed = _parse_pubmed_xml(response, expected_pmid=pmid)
        except (ET.ParseError, LookupError, TypeError, ValueError):
            return _empty_content(
                canonical,
                status="invalid_response",
                failure_category="invalid_provider_response",
            )
        if (
            identifier_type == "PMCID"
            and str(parsed["pmcid"] or "").upper() != identifier.upper()
        ) or (
            identifier_type == "DOI"
            and str(parsed["doi"] or "").casefold() != identifier.casefold()
        ):
            return _empty_content(
                canonical,
                status="invalid_response",
                failure_category="identifier_mismatch",
            )
        parsed["identifier_type"] = identifier_type
        parsed["identifier"] = identifier
        return parsed
    finally:
        if owns_session:
            cast(requests.Session, client).close()


__all__ = [
    "LiteratureReferenceContent",
    "MAX_REFERENCE_CONTENT_BYTES",
    "fetch_literature_reference_content",
]
