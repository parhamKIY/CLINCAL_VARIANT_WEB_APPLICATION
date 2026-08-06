"""Triggered, bounded population and literature evidence enrichment."""

from __future__ import annotations

import ast
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any, TypedDict
from urllib.parse import quote, urlencode

import requests

from backend.logging_config import get_logger
from config import settings


LOGGER = get_logger("conditional_enrichment")
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
MEANINGFUL_SEVERITIES = {"moderate", "major", "critical"}
RSID_PATTERN = re.compile(r"rs[1-9][0-9]*", re.IGNORECASE)
MAX_POPULATIONS = 20
MAX_QUERY_TERMS = 8
MAX_LITVAR_RESPONSE_LINES = 1000
MAX_LITVAR_RESPONSE_CHARS = 5_000_000
NCBI_REQUEST_INTERVAL = 0.5
_NCBI_RATE_LOCK = Lock()
_LAST_NCBI_REQUEST_AT = 0.0

class ConditionalEnrichmentError(ValueError):
    """Raised when conditional enrichment input is invalid."""


class ProviderResponseError(ConditionalEnrichmentError):
    """Raised for a classified invalid provider response."""

    def __init__(
        self,
        reason: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


class ConditionalEnrichmentResult(TypedDict):
    """Ordered enriched candidates and bounded aggregate status."""

    variants: list[dict[str, Any]]
    status: str
    message: str
    variant_count: int
    triggered_count: int
    population_status: str
    litvar_status: str
    europe_pmc_status: str
    pubmed_status: str


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object, maximum: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:maximum] if cleaned else None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _retry_delay(attempt: int, response: object = None) -> float:
    if response is not None:
        headers = getattr(response, "headers", {})
        if isinstance(headers, Mapping):
            retry_after = headers.get("Retry-After")
            try:
                if retry_after is not None:
                    return min(max(float(retry_after), 0.0), 5.0)
            except (TypeError, ValueError):
                pass
    return min(float(2**attempt), 5.0)


def _wait_for_ncbi_slot(session: object) -> None:
    if not isinstance(session, requests.Session):
        return
    global _LAST_NCBI_REQUEST_AT
    with _NCBI_RATE_LOCK:
        elapsed = time.monotonic() - _LAST_NCBI_REQUEST_AT
        remaining = NCBI_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)
        _LAST_NCBI_REQUEST_AT = time.monotonic()


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str | int | float | bool] | None = None,
    ncbi: bool = False,
    litvar: bool = False,
) -> tuple[object, int, int]:
    """Return JSON, final HTTP status, and attempt count."""

    max_retries = settings.CONDITIONAL_ENRICHMENT_MAX_RETRIES
    for attempt in range(max_retries + 1):
        if ncbi:
            _wait_for_ncbi_slot(session)
        try:
            response = session.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ClinicalVariantInterpretation/0.1",
                },
                timeout=settings.CONDITIONAL_ENRICHMENT_TIMEOUT,
                verify=True,
            )
        except requests.RequestException:
            if attempt >= max_retries:
                raise
            time.sleep(_retry_delay(attempt))
            continue
        if (
            response.status_code in TRANSIENT_HTTP_STATUSES
            and attempt < max_retries
        ):
            time.sleep(_retry_delay(attempt, response))
            continue
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError(
                f"HTTP {response.status_code}",
                response=response,
            )
        headers = getattr(response, "headers", {})
        content_type = ""
        if isinstance(headers, Mapping):
            content_type = str(
                headers.get(
                    "Content-Type",
                    headers.get("content-type", ""),
                )
            ).partition(";")[0].strip().casefold()
        valid_content_type = (
            not content_type
            or content_type == "application/json"
            or content_type.endswith("+json")
            or (
                litvar
                and content_type
                in {
                    "application/x-ndjson",
                    "application/ndjson",
                    "text/plain",
                }
            )
        )
        if not valid_content_type:
            raise ProviderResponseError(
                "invalid_content_type",
                http_status=response.status_code,
            )
        try:
            return response.json(), response.status_code, attempt + 1
        except ValueError as exc:
            if litvar:
                try:
                    parsed_body = _parse_litvar_body(
                        getattr(response, "text", None)
                    )
                except ConditionalEnrichmentError as parse_exc:
                    raise ProviderResponseError(
                        "invalid_json",
                        http_status=response.status_code,
                    ) from parse_exc
                return (
                    parsed_body,
                    response.status_code,
                    attempt + 1,
                )
            raise ProviderResponseError(
                "invalid_json",
                http_status=response.status_code,
            ) from exc
    raise RuntimeError("Conditional request retry loop ended unexpectedly.")


def _parse_litvar_body(value: object) -> list[dict[str, Any]]:
    """Parse LitVar2's bounded NDJSON/Python-literal response format."""

    if not isinstance(value, str):
        raise ConditionalEnrichmentError(
            "LitVar returned an invalid response body."
        )
    text = value.strip()
    if not text:
        return []
    if len(text) > MAX_LITVAR_RESPONSE_CHARS:
        raise ConditionalEnrichmentError(
            "LitVar response exceeded the size limit."
        )
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(raw_lines) > MAX_LITVAR_RESPONSE_LINES:
        raise ConditionalEnrichmentError(
            "LitVar response exceeded the row limit."
        )
    rows: list[dict[str, Any]] = []
    for line in raw_lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(line)
            except (SyntaxError, ValueError) as exc:
                raise ConditionalEnrichmentError(
                    "LitVar returned malformed NDJSON."
                ) from exc
        if not isinstance(parsed, dict):
            raise ConditionalEnrichmentError(
                "LitVar NDJSON rows must be dictionaries."
            )
        rows.append(parsed)
    return rows


def _finite_frequency(value: object) -> float | None:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= float(value) <= 1.0
    ):
        return None
    return float(value)


def _request_failure_reason(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return "timeout"
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        return "rate_limited"
    if status == 403:
        return "forbidden"
    if isinstance(status, int) and status >= 500:
        return "upstream_error"
    if isinstance(status, int):
        return "http_error"
    return "network_error"


def _candidate_rsid(candidate: Mapping[str, object]) -> str | None:
    sources = _mapping(candidate.get("sources"))
    myvariant = _mapping(sources.get("myvariant"))
    for value in (
        myvariant.get("rsid"),
        candidate.get("rsid"),
    ):
        rsid = _text(value, 32)
        if rsid is not None and RSID_PATTERN.fullmatch(rsid):
            return rsid.casefold()
    return None


def _population_source_url(rsid: str | None) -> str | None:
    if rsid is None:
        return None
    path = quote(rsid, safe="")
    return (
        f"{settings.ENSEMBL_VARIATION_BASE_URL}/variation/human/"
        f"{path}?{urlencode({'pops': 1})}"
    )


def _empty_population_evidence(
    *,
    status: str,
    candidate: Mapping[str, object],
    failure_reason: str | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    rsid = _candidate_rsid(candidate)
    return {
        "status": status,
        "response_status": status,
        "provider": "Ensembl REST Variation",
        "provider_version": None,
        "upstream_sources": [],
        "retrieved_at": (
            None if status == "not_triggered" else _timestamp()
        ),
        "assembly": _text(candidate.get("assembly"), 20),
        "dataset": None,
        "release": None,
        "query_identifier": rsid,
        "http_status": http_status,
        "source_url": _population_source_url(rsid),
        "derivation": "direct",
        "most_severe_consequence": None,
        "minor_allele": None,
        "global_maf": None,
        "populations": [],
        "warnings": [],
        "failure_reason": failure_reason,
    }


def _ensembl_population_rows(
    value: object,
) -> tuple[list[dict[str, Any]], int]:
    if value is None:
        return [], 0
    if not isinstance(value, list):
        raise ProviderResponseError("invalid_schema")
    rows: list[dict[str, Any]] = []
    invalid = 0
    for item in value[:MAX_POPULATIONS]:
        if not isinstance(item, dict):
            invalid += 1
            continue
        population = _text(item.get("population"), 150)
        allele = _text(item.get("allele"), 10_000)
        frequency = _finite_frequency(item.get("frequency"))
        if population is None or allele is None or frequency is None:
            invalid += 1
            continue
        rows.append(
            {
                "population": population,
                "allele": allele,
                "frequency": frequency,
            }
        )
    return rows, invalid


def fetch_ensembl_population_evidence(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch direct Ensembl population frequencies for one rsID."""

    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    evidence = _empty_population_evidence(
        status="unavailable",
        candidate=candidate,
    )
    rsid = evidence["query_identifier"]
    if rsid is None:
        evidence["status"] = "missing_identifier"
        evidence["response_status"] = "missing_identifier"
        evidence["failure_reason"] = "missing_rsid"
        return evidence

    owns_session = session is None
    client = session or requests.Session()
    try:
        try:
            payload, http_status, _ = _request_json(
                client,
                (
                    f"{settings.ENSEMBL_VARIATION_BASE_URL}/"
                    f"variation/human/{quote(rsid, safe='')}"
                ),
                params={"pops": 1},
            )
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            evidence["http_status"] = status
            if status == 404:
                evidence["status"] = "no_match"
                evidence["response_status"] = "no_match"
                return evidence
            evidence["failure_reason"] = _request_failure_reason(exc)
            return evidence
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            evidence["http_status"] = getattr(
                response,
                "status_code",
                None,
            )
            evidence["failure_reason"] = _request_failure_reason(exc)
            return evidence
        except ProviderResponseError as exc:
            evidence["status"] = "invalid_response"
            evidence["response_status"] = "invalid_response"
            evidence["http_status"] = exc.http_status
            evidence["failure_reason"] = exc.reason
            return evidence
    finally:
        if owns_session:
            client.close()

    evidence["http_status"] = http_status
    if not isinstance(payload, dict):
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
        return evidence
    name = _text(payload.get("name"), 32)
    synonyms = payload.get("synonyms")
    synonym_values = (
        {
            item.casefold()
            for item in synonyms
            if isinstance(item, str)
        }
        if isinstance(synonyms, list)
        else set()
    )
    if name is None:
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
        return evidence
    if name.casefold() != rsid and rsid not in synonym_values:
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "variant_identity_mismatch"
        return evidence
    try:
        populations, invalid = _ensembl_population_rows(
            payload.get("populations")
        )
    except ProviderResponseError as exc:
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = exc.reason
        return evidence

    source = _text(payload.get("source"), 100)
    release_value = payload.get("release")
    release = (
        _text(release_value, 100)
        if isinstance(release_value, str)
        else (
            str(release_value)
            if isinstance(release_value, int)
            and not isinstance(release_value, bool)
            else None
        )
    )
    evidence.update(
        {
            "provider_version": release,
            "upstream_sources": [source] if source else [],
            "dataset": source,
            "release": release,
            "most_severe_consequence": _text(
                payload.get("most_severe_consequence"),
                200,
            ),
            "minor_allele": _text(payload.get("minor_allele"), 100),
            "global_maf": _finite_frequency(payload.get("MAF")),
            "populations": populations,
        }
    )
    if not populations:
        evidence["status"] = (
            "invalid_response" if invalid else "no_match"
        )
        evidence["response_status"] = evidence["status"]
        evidence["failure_reason"] = (
            "invalid_schema" if invalid else None
        )
    elif invalid:
        evidence["status"] = "partial"
        evidence["response_status"] = "partial"
        evidence["warnings"] = [
            "Ensembl returned partial population-frequency evidence."
        ]
    else:
        evidence["status"] = "available"
        evidence["response_status"] = "available"
    return evidence


def fetch_gnomad_evidence(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Return an explicit disabled result for the deprecated adapter."""

    _ = session
    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    return {
        "status": "deprecated",
        "response_status": "deprecated",
        "provider": "gnomAD",
        "active": False,
        "retrieved_at": None,
        "failure_reason": "adapter_disabled",
    }


def _candidate_identifiers(
    candidate: Mapping[str, object],
) -> tuple[list[str], str | None]:
    sources = _mapping(candidate.get("sources"))
    myvariant = _mapping(sources.get("myvariant"))
    gene = _text(candidate.get("gene"), 100)
    identifiers: list[str] = []
    for value in (
        myvariant.get("rsid"),
        candidate.get("hgvsp"),
        candidate.get("protein_change"),
        candidate.get("hgvsc"),
    ):
        text = _text(value, 200)
        if text is not None and text not in identifiers:
            identifiers.append(text)
    return identifiers[:MAX_QUERY_TERMS], gene


def _flatten_strings(value: object, *, depth: int = 0) -> list[str]:
    if depth > 3:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [
            text
            for item in value[:100]
            for text in _flatten_strings(item, depth=depth + 1)
        ]
    if isinstance(value, dict):
        return [
            text
            for item in value.values()
            for text in _flatten_strings(item, depth=depth + 1)
        ]
    return []


def _litvar_candidates(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if any(
            field in payload
            for field in ("_id", "litvar_id", "variant_id", "variantId")
        ):
            return [payload]
        for field in ("results", "result", "variants", "data"):
            value = payload.get(field)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise ConditionalEnrichmentError("LitVar returned an invalid schema.")


def _litvar_variant_id(
    payload: object,
    *,
    identifiers: list[str],
    gene: str | None,
) -> str | None:
    for item in _litvar_candidates(payload):
        strings = {
            value.strip().casefold()
            for value in _flatten_strings(item)
            if value.strip()
        }
        identifier_match = any(
            identifier.strip().casefold() in strings
            for identifier in identifiers
        )
        gene_match = (
            gene is None
            or gene.strip().casefold() in strings
        )
        if not identifier_match or not gene_match:
            continue
        for field in (
            "_id",
            "litvar_id",
            "variant_id",
            "variantId",
            "id",
        ):
            variant_id = _text(item.get(field), 300)
            if variant_id is not None:
                return variant_id
    return None


def _publication_ids(payload: object) -> dict[str, set[str]]:
    ids = {"pmids": set(), "pmcids": set()}
    if isinstance(payload, dict):
        direct_pmids = payload.get("pmids")
        direct_pmcids = payload.get("pmcids")
        if isinstance(direct_pmids, list):
            for item in direct_pmids[:1000]:
                if (
                    isinstance(item, int)
                    and not isinstance(item, bool)
                    and item > 0
                ):
                    ids["pmids"].add(str(item))
                elif isinstance(item, str) and item.isdigit():
                    ids["pmids"].add(item)
        if isinstance(direct_pmcids, list):
            for item in direct_pmcids[:1000]:
                if (
                    isinstance(item, str)
                    and item.upper().startswith("PMC")
                ):
                    ids["pmcids"].add(item.upper())
        if isinstance(direct_pmids, list) or isinstance(
            direct_pmcids,
            list,
        ):
            return ids

    values = payload
    if isinstance(payload, dict):
        for field in ("publications", "results", "result", "data"):
            if isinstance(payload.get(field), list):
                values = payload[field]
                break
    if not isinstance(values, list):
        raise ConditionalEnrichmentError(
            "LitVar publications returned an invalid schema."
        )
    for item in values[:1000]:
        if (
            isinstance(item, int)
            and not isinstance(item, bool)
            and item > 0
        ):
            ids["pmids"].add(str(item))
            continue
        if isinstance(item, str):
            if item.isdigit():
                ids["pmids"].add(item)
            elif item.upper().startswith("PMC"):
                ids["pmcids"].add(item.upper())
            continue
        if not isinstance(item, dict):
            continue
        for field in ("pmid", "PMID"):
            pmid = _text(item.get(field), 20)
            if pmid is not None and pmid.isdigit():
                ids["pmids"].add(pmid)
        for field in ("pmcid", "PMCID"):
            pmcid = _text(item.get(field), 30)
            if pmcid is not None and pmcid.upper().startswith("PMC"):
                ids["pmcids"].add(pmcid.upper())
    return ids


def _pubmed_query(
    candidate: Mapping[str, object],
    identifiers: list[str],
    gene: str | None,
) -> str | None:
    terms: list[str] = []
    if gene is not None:
        terms.append(f'"{gene}"[Title/Abstract]')
    if identifiers:
        variant_terms = " OR ".join(
            f'"{item}"[Title/Abstract]' for item in identifiers[:4]
        )
        terms.append(f"({variant_terms})")
    phenotype = _mapping(candidate.get("mydisease"))
    diseases = phenotype.get("diseases")
    if not identifiers and isinstance(diseases, list):
        for disease in diseases[:1]:
            if isinstance(disease, dict):
                name = _text(disease.get("disease_name"), 200)
                if name is not None:
                    terms.append(f'"{name}"[Title/Abstract]')
    return " AND ".join(terms) if len(terms) >= 2 else None


def _pubmed_search_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        raise ConditionalEnrichmentError(
            "PubMed search returned an invalid schema."
        )
    search = payload.get("esearchresult")
    if not isinstance(search, dict) or not isinstance(
        search.get("idlist"),
        list,
    ):
        raise ConditionalEnrichmentError(
            "PubMed search returned an invalid schema."
        )
    return [
        item
        for item in search["idlist"]
        if isinstance(item, str) and item.isdigit()
    ]


def _summary_articles(
    payload: object,
    pmids: list[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("result"),
        dict,
    ):
        raise ConditionalEnrichmentError(
            "PubMed summary returned an invalid schema."
        )
    result = payload["result"]
    articles: dict[str, dict[str, Any]] = {}
    for pmid in pmids:
        item = result.get(pmid)
        if not isinstance(item, dict):
            continue
        authors = item.get("authors")
        doi: str | None = None
        article_ids = item.get("articleids")
        if isinstance(article_ids, list):
            for identifier in article_ids:
                if (
                    isinstance(identifier, dict)
                    and str(identifier.get("idtype")).casefold() == "doi"
                ):
                    doi = _text(identifier.get("value"), 200)
                    break
        articles[pmid] = {
            "pmid": pmid,
            "pmcid": None,
            "title": _text(item.get("title"), 500),
            "journal": _text(
                item.get("fulljournalname", item.get("source")),
                300,
            ),
            "publication_date": _text(item.get("pubdate"), 100),
            "authors": (
                [
                    name
                    for author in authors[:20]
                    if isinstance(author, dict)
                    for name in [_text(author.get("name"), 150)]
                    if name is not None
                ]
                if isinstance(authors, list)
                else []
            ),
            "doi": doi,
            "source_providers": ["PubMed"],
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
    return articles


def _provider_status(
    status: str,
    *,
    provider: str,
    upstream_sources: list[str],
    query_identifier: str | None = None,
    source_url: str | None = None,
    dataset: str | None = None,
    release: str | None = None,
    derivation: str = "direct",
    retrieved_at: str | None = None,
    http_status: int | None = None,
    failure_reason: str | None = None,
    result_count: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "response_status": status,
        "provider": provider,
        "upstream_sources": upstream_sources,
        "query_identifier": query_identifier,
        "retrieved_at": retrieved_at,
        "source_url": source_url,
        "dataset": dataset,
        "release": release,
        "derivation": derivation,
        "http_status": http_status,
        "result_count": result_count,
        "failure_reason": failure_reason,
    }


def _literature_provider_status(
    name: str,
    status: str,
    **kwargs: Any,
) -> dict[str, Any]:
    metadata = {
        "litvar": (
            "LitVar2",
            ["LitVar2", "PubMed"],
            "LitVar2",
            settings.LITVAR_BASE_URL,
        ),
        "europe_pmc": (
            "Europe PMC",
            ["Europe PMC", "PubMed"],
            "Europe PMC",
            f"{settings.EUROPE_PMC_BASE_URL}/search",
        ),
        "pubmed": (
            "PubMed E-utilities",
            ["PubMed"],
            "PubMed",
            f"{settings.PUBMED_BASE_URL}/esearch.fcgi",
        ),
    }
    provider, upstream, dataset, source_url = metadata[name]
    details: dict[str, Any] = {
        "dataset": dataset,
        "source_url": source_url,
    }
    details.update(kwargs)
    return _provider_status(
        status,
        provider=provider,
        upstream_sources=upstream,
        **details,
    )


def _empty_literature(status: str) -> dict[str, Any]:
    retrieved_at = None if status == "not_triggered" else _timestamp()
    return {
        "status": status,
        "response_status": status,
        "provider": "LitVar2 with Europe PMC and PubMed fallbacks",
        "provider_version": None,
        "upstream_sources": ["LitVar2", "Europe PMC", "PubMed"],
        "retrieved_at": retrieved_at,
        "query_basis": [],
        "providers": {
            name: _literature_provider_status(
                name,
                (
                    status
                    if name == "litvar" or status == "not_triggered"
                    else "not_triggered"
                ),
                retrieved_at=(
                    retrieved_at
                    if name == "litvar"
                    else None
                ),
            )
            for name in ("litvar", "europe_pmc", "pubmed")
        },
        "articles": [],
        "warnings": [],
        "failure_reason": None,
    }


def _empty_article(
    *,
    pmid: str | None = None,
    pmcid: str | None = None,
    doi: str | None = None,
    provider: str,
) -> dict[str, Any]:
    return {
        "pmid": pmid,
        "pmcid": pmcid,
        "title": None,
        "journal": None,
        "publication_date": None,
        "authors": [],
        "doi": doi,
        "source_providers": [provider],
        "url": (
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            if pmid is not None
            else (
                f"https://europepmc.org/article/MED/{pmcid}"
                if pmcid is not None
                else None
            )
        ),
    }


def _normalized_pmid(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    text = _text(value, 20)
    return text if text is not None and text.isdigit() else None


def _normalized_pmcid(value: object) -> str | None:
    text = _text(value, 30)
    if text is None or not text.upper().startswith("PMC"):
        return None
    return text.upper()


def _normalized_doi(value: object) -> str | None:
    text = _text(value, 200)
    return text.casefold() if text is not None else None


def _litvar_articles(
    payload: object,
) -> tuple[list[dict[str, Any]], int]:
    if isinstance(payload, dict):
        for field in ("publications", "results", "result", "data"):
            if isinstance(payload.get(field), list):
                payload = payload[field]
                break
    if isinstance(payload, list):
        articles: list[dict[str, Any]] = []
        invalid = 0
        for item in payload[:1000]:
            if isinstance(item, (int, str)):
                pmid = _normalized_pmid(item)
                pmcid = _normalized_pmcid(item)
                if pmid is not None or pmcid is not None:
                    articles.append(
                        _empty_article(
                            pmid=pmid,
                            pmcid=pmcid,
                            provider="LitVar2",
                        )
                    )
                else:
                    invalid += 1
                continue
            if not isinstance(item, dict):
                invalid += 1
                continue
            pmid = _normalized_pmid(
                item.get("pmid", item.get("PMID"))
            )
            pmcid = _normalized_pmcid(
                item.get("pmcid", item.get("PMCID"))
            )
            doi = _normalized_doi(item.get("doi"))
            if pmid is None and pmcid is None and doi is None:
                invalid += 1
                continue
            article = _empty_article(
                pmid=pmid,
                pmcid=pmcid,
                doi=doi,
                provider="LitVar2",
            )
            article.update(
                {
                    "title": _text(item.get("title"), 500),
                    "journal": _text(item.get("journal"), 300),
                    "publication_date": _text(
                        item.get("publication_date"),
                        100,
                    ),
                }
            )
            articles.append(article)
        return articles, invalid
    identifiers = _publication_ids(payload)
    articles = [
        _empty_article(pmid=pmid, provider="LitVar2")
        for pmid in sorted(
            identifiers["pmids"],
            key=int,
            reverse=True,
        )
    ]
    articles.extend(
        _empty_article(pmcid=pmcid, provider="LitVar2")
        for pmcid in sorted(identifiers["pmcids"])
    )
    return articles, 0


def _europe_pmc_articles(
    payload: object,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, dict):
        raise ProviderResponseError("invalid_schema")
    result_list = payload.get("resultList")
    if not isinstance(result_list, dict) or not isinstance(
        result_list.get("result"),
        list,
    ):
        raise ProviderResponseError("invalid_schema")
    articles: list[dict[str, Any]] = []
    invalid = 0
    for item in result_list["result"][
        : settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES
    ]:
        if not isinstance(item, dict):
            invalid += 1
            continue
        pmid = _normalized_pmid(item.get("pmid"))
        pmcid = _normalized_pmcid(item.get("pmcid"))
        doi = _normalized_doi(item.get("doi"))
        if pmid is None and pmcid is None and doi is None:
            invalid += 1
            continue
        author_text = _text(item.get("authorString"), 1000)
        article = _empty_article(
            pmid=pmid,
            pmcid=pmcid,
            doi=doi,
            provider="Europe PMC",
        )
        article.update(
            {
                "title": _text(item.get("title"), 500),
                "journal": _text(item.get("journalTitle"), 300),
                "publication_date": _text(
                    item.get(
                        "firstPublicationDate",
                        item.get("pubYear"),
                    ),
                    100,
                ),
                "authors": [author_text] if author_text else [],
                "url": (
                    f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                    if pmid
                    else (
                        f"https://europepmc.org/article/PMC/{pmcid}"
                        if pmcid
                        else f"https://doi.org/{doi}"
                    )
                ),
            }
        )
        articles.append(article)
    return articles, invalid


def _article_matches(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    """Match by the strongest identifier shared by both records."""

    for field in ("pmid", "pmcid", "doi"):
        left_value = left.get(field)
        right_value = right.get(field)
        if isinstance(left_value, str) and isinstance(right_value, str):
            return left_value.casefold() == right_value.casefold()
    return False


def _deduplicate_articles(
    articles: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []

    def merge(
        existing: dict[str, Any],
        article: Mapping[str, Any],
    ) -> None:
        for field in (
            "pmid",
            "pmcid",
            "title",
            "journal",
            "publication_date",
            "doi",
            "url",
        ):
            if (
                existing.get(field) is None
                and article.get(field) is not None
            ):
                existing[field] = deepcopy(article[field])
        existing["authors"] = list(
            dict.fromkeys(
                [
                    *existing.get("authors", []),
                    *article.get("authors", []),
                ]
            )
        )[:20]
        existing["source_providers"] = list(
            dict.fromkeys(
                [
                    *existing.get("source_providers", []),
                    *article.get("source_providers", []),
                ]
            )
        )

    for article in articles:
        matching_indexes = [
            index
            for index, item in enumerate(deduplicated)
            if _article_matches(item, article)
        ]
        if not matching_indexes:
            deduplicated.append(deepcopy(article))
            continue
        primary = deduplicated[matching_indexes[0]]
        merge(primary, article)
        for index in reversed(matching_indexes[1:]):
            merge(primary, deduplicated[index])
            deduplicated.pop(index)
    return deduplicated[
        : settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES
    ]


def _provider_failure_status(
    exc: requests.RequestException,
) -> tuple[str, int | None, str]:
    response = getattr(exc, "response", None)
    return (
        "unavailable",
        getattr(response, "status_code", None),
        _request_failure_reason(exc),
    )


def fetch_literature_evidence(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch LitVar2 citations with failure-only bounded fallbacks."""

    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    result = _empty_literature("unavailable")
    identifiers, gene = _candidate_identifiers(candidate)
    query = _pubmed_query(candidate, identifiers, gene)
    result["query_basis"] = [
        item for item in ([gene] + identifiers) if item is not None
    ][:MAX_QUERY_TERMS]
    if not identifiers and query is None:
        result["status"] = "missing_identifier"
        result["response_status"] = "missing_identifier"
        for name in result["providers"]:
            result["providers"][name] = _literature_provider_status(
                name,
                "missing_identifier",
                retrieved_at=result["retrieved_at"],
                failure_reason="insufficient_query_identifiers",
            )
        result["failure_reason"] = "insufficient_query_identifiers"
        return result

    owns_session = session is None
    client = session or requests.Session()
    articles: list[dict[str, Any]] = []
    litvar_failed = False
    litvar_http_status: int | None = None
    if identifiers:
        autocomplete_query = (
            f"{gene} {identifiers[0]}" if gene else identifiers[0]
        )
        autocomplete_url = (
            f"{settings.LITVAR_BASE_URL}/variant/autocomplete/"
        )
        try:
            payload, status, _ = _request_json(
                client,
                autocomplete_url,
                params={"query": autocomplete_query, "limit": 10},
                ncbi=True,
                litvar=True,
            )
            litvar_http_status = status
            litvar_id = _litvar_variant_id(
                payload,
                identifiers=identifiers,
                gene=gene,
            )
            if litvar_id is None:
                result["providers"]["litvar"] = (
                    _literature_provider_status(
                    "litvar",
                    "no_match",
                    retrieved_at=result["retrieved_at"],
                    query_identifier=identifiers[0],
                    source_url=autocomplete_url,
                    http_status=status,
                )
                )
            else:
                encoded_id = quote(litvar_id, safe="")
                publication_url = (
                    f"{settings.LITVAR_BASE_URL}/variant/get/"
                    f"{encoded_id}/publications"
                )
                publication_payload, publication_status, _ = _request_json(
                    client,
                    publication_url,
                    ncbi=True,
                    litvar=True,
                )
                litvar_http_status = publication_status
                litvar_articles, invalid_articles = _litvar_articles(
                    publication_payload
                )
                articles.extend(litvar_articles)
                litvar_state = (
                    "partial"
                    if invalid_articles and litvar_articles
                    else (
                        "invalid_response"
                        if invalid_articles
                        else (
                            "available"
                            if litvar_articles
                            else "no_match"
                        )
                    )
                )
                litvar_failed = invalid_articles > 0
                result["providers"]["litvar"] = (
                    _literature_provider_status(
                        "litvar",
                        litvar_state,
                        retrieved_at=result["retrieved_at"],
                        query_identifier=litvar_id,
                        source_url=publication_url,
                        http_status=publication_status,
                        result_count=len(litvar_articles),
                        failure_reason=(
                            "invalid_schema"
                            if invalid_articles
                            else None
                        ),
                    )
                )
        except requests.RequestException as exc:
            litvar_failed = True
            status, http_status, reason = _provider_failure_status(exc)
            result["providers"]["litvar"] = _literature_provider_status(
                "litvar",
                status,
                retrieved_at=result["retrieved_at"],
                query_identifier=identifiers[0],
                source_url=autocomplete_url,
                http_status=http_status,
                failure_reason=reason,
            )
        except ProviderResponseError as exc:
            litvar_failed = True
            result["providers"]["litvar"] = _literature_provider_status(
                "litvar",
                "invalid_response",
                retrieved_at=result["retrieved_at"],
                query_identifier=identifiers[0],
                source_url=autocomplete_url,
                http_status=exc.http_status,
                failure_reason=exc.reason,
            )
        except ConditionalEnrichmentError:
            litvar_failed = True
            result["providers"]["litvar"] = _literature_provider_status(
                "litvar",
                "invalid_response",
                retrieved_at=result["retrieved_at"],
                query_identifier=identifiers[0],
                source_url=autocomplete_url,
                http_status=litvar_http_status,
                failure_reason="invalid_schema",
            )
    else:
        result["providers"]["litvar"] = _literature_provider_status(
            "litvar",
            "missing_identifier",
            retrieved_at=result["retrieved_at"],
            failure_reason="missing_variant_identifier",
        )

    europe_failed = False
    europe_http_status: int | None = None
    if litvar_failed and query is not None:
        europe_url = f"{settings.EUROPE_PMC_BASE_URL}/search"
        try:
            europe_payload, europe_status, _ = _request_json(
                client,
                europe_url,
                params={
                    "query": query,
                    "format": "json",
                    "pageSize": (
                        settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES
                    ),
                },
            )
            europe_http_status = europe_status
            europe_articles, invalid = _europe_pmc_articles(
                europe_payload
            )
            articles.extend(europe_articles)
            europe_state = (
                "partial"
                if invalid and europe_articles
                else (
                    "invalid_response"
                    if invalid
                    else (
                        "available"
                        if europe_articles
                        else "no_match"
                    )
                )
            )
            europe_failed = europe_state in {
                "partial",
                "invalid_response",
            }
            result["providers"]["europe_pmc"] = (
                _literature_provider_status(
                    "europe_pmc",
                    europe_state,
                    retrieved_at=result["retrieved_at"],
                    query_identifier=query,
                    source_url=europe_url,
                    http_status=europe_status,
                    result_count=len(europe_articles),
                    failure_reason=(
                        "invalid_schema" if invalid else None
                    ),
                )
            )
        except requests.RequestException as exc:
            europe_failed = True
            status, http_status, reason = _provider_failure_status(exc)
            result["providers"]["europe_pmc"] = (
                _literature_provider_status(
                    "europe_pmc",
                    status,
                    retrieved_at=result["retrieved_at"],
                    query_identifier=query,
                    source_url=europe_url,
                    http_status=http_status,
                    failure_reason=reason,
                )
            )
        except ProviderResponseError as exc:
            europe_failed = True
            result["providers"]["europe_pmc"] = (
                _literature_provider_status(
                    "europe_pmc",
                    "invalid_response",
                    retrieved_at=result["retrieved_at"],
                    query_identifier=query,
                    source_url=europe_url,
                    http_status=(
                        exc.http_status or europe_http_status
                    ),
                    failure_reason=exc.reason,
                )
            )
    elif litvar_failed:
        result["providers"]["europe_pmc"] = (
            _literature_provider_status(
                "europe_pmc",
                "missing_identifier",
                retrieved_at=result["retrieved_at"],
                failure_reason="insufficient_query_identifiers",
            )
        )

    if europe_failed and query is not None:
        search_url = f"{settings.PUBMED_BASE_URL}/esearch.fcgi"
        pubmed_pmids: list[str] = []
        pubmed_http_status: int | None = None
        try:
            search_payload, search_status, _ = _request_json(
                client,
                search_url,
                params={
                    "db": "pubmed",
                    "retmode": "json",
                    "retmax": (
                        settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES
                    ),
                    "sort": "relevance",
                    "tool": "clinical_variant_app",
                    "term": query,
                },
                ncbi=True,
            )
            pubmed_http_status = search_status
            pubmed_pmids = _pubmed_search_ids(search_payload)
            pubmed_articles = [
                _empty_article(pmid=pmid, provider="PubMed")
                for pmid in pubmed_pmids
            ]
            articles.extend(pubmed_articles)
            result["providers"]["pubmed"] = (
                _literature_provider_status(
                    "pubmed",
                    "available" if pubmed_pmids else "no_match",
                    retrieved_at=result["retrieved_at"],
                    query_identifier=query,
                    source_url=search_url,
                    http_status=search_status,
                    result_count=len(pubmed_pmids),
                )
            )
        except requests.RequestException as exc:
            status, http_status, reason = _provider_failure_status(exc)
            result["providers"]["pubmed"] = (
                _literature_provider_status(
                    "pubmed",
                    status,
                    retrieved_at=result["retrieved_at"],
                    query_identifier=query,
                    source_url=search_url,
                    http_status=http_status,
                    failure_reason=reason,
                )
            )
        except ProviderResponseError as exc:
            result["providers"]["pubmed"] = (
                _literature_provider_status(
                    "pubmed",
                    "invalid_response",
                    retrieved_at=result["retrieved_at"],
                    query_identifier=query,
                    source_url=search_url,
                    http_status=exc.http_status,
                    failure_reason=exc.reason,
                )
            )
        except ConditionalEnrichmentError:
            result["providers"]["pubmed"] = (
                _literature_provider_status(
                    "pubmed",
                    "invalid_response",
                    retrieved_at=result["retrieved_at"],
                    query_identifier=query,
                    source_url=search_url,
                    http_status=pubmed_http_status,
                    failure_reason="invalid_schema",
                )
            )

        if pubmed_pmids:
            try:
                summary_payload, _, _ = _request_json(
                    client,
                    f"{settings.PUBMED_BASE_URL}/esummary.fcgi",
                    params={
                        "db": "pubmed",
                        "retmode": "json",
                        "id": ",".join(pubmed_pmids),
                        "tool": "clinical_variant_app",
                    },
                    ncbi=True,
                )
                articles.extend(
                    _summary_articles(
                        summary_payload,
                        pubmed_pmids,
                    ).values()
                )
            except (
                requests.RequestException,
                ConditionalEnrichmentError,
            ):
                result["warnings"].append(
                    "PubMed metadata was unavailable; identifiers "
                    "were retained."
                )
                provider = result["providers"]["pubmed"]
                provider["status"] = "partial"
                provider["response_status"] = "partial"
                provider["failure_reason"] = "metadata_unavailable"
    if owns_session:
        client.close()

    result["articles"] = _deduplicate_articles(articles)
    provider_statuses = {
        item["status"] for item in result["providers"].values()
    }
    failures = provider_statuses & {
        "partial",
        "unavailable",
        "invalid_response",
    }
    if result["articles"]:
        result["status"] = "partial" if failures else "available"
    elif "no_match" in provider_statuses and not failures:
        result["status"] = "no_match"
    elif "invalid_response" in failures and "unavailable" not in failures:
        result["status"] = "invalid_response"
    elif failures:
        result["status"] = "unavailable"
    else:
        result["status"] = "missing_identifier"
    result["response_status"] = result["status"]
    result["failure_reason"] = (
        "provider_failure" if failures and not result["articles"] else None
    )
    return result


def _frequency_values(candidate: Mapping[str, object]) -> list[float]:
    sources = _mapping(candidate.get("sources"))
    values: list[float] = []
    containers = (
        (
            _mapping(_mapping(sources.get("myvariant")).get(
                "population_frequencies"
            )),
            False,
        ),
        (
            _mapping(_mapping(sources.get("genebe")).get(
                "population_annotations"
            )),
            True,
        ),
    )
    for container, require_frequency_key in containers:
        for key, value in container.items():
            normalized_key = str(key).casefold()
            if require_frequency_key and not (
                normalized_key.endswith("_af")
                or "frequency" in normalized_key
            ):
                continue
            frequency = _finite_frequency(value)
            if frequency is not None:
                values.append(frequency)
    return values


def determine_enrichment_triggers(
    evidence: Mapping[str, object],
    candidate: Mapping[str, object],
) -> list[str]:
    """Return conservative deterministic Stage 32 trigger reasons."""

    audit = _mapping(_mapping(evidence.get("conflict_audit")).get(
        "pre_review"
    ))
    severity = _text(audit.get("routing_severity"), 20)
    normalized = audit.get("normalized_classifications")
    labels = {
        item.get("normalized_label")
        for item in normalized
        if isinstance(item, dict)
    } if isinstance(normalized, list) else set()
    triggers: list[str] = []
    if severity in MEANINGFUL_SEVERITIES:
        triggers.append("meaningful_conflict")
    if "VUS" in labels:
        triggers.append("vus")
    if not labels:
        triggers.append("insufficient_evidence")
    frequencies = _frequency_values(candidate)
    positive = [value for value in frequencies if value > 0]
    if (
        len(frequencies) >= 2
        and (
            (0.0 in frequencies and positive)
            or (
                len(positive) >= 2
                and max(positive) / min(positive) >= 10.0
            )
        )
    ):
        triggers.append("population_evidence_ambiguity")
    if (
        candidate.get("literature_evidence_requested") is True
        or set(triggers)
        & {"meaningful_conflict", "vus", "insufficient_evidence"}
    ):
        triggers.append("literature_evidence_need")
    return triggers


def _myvariant_fallback(
    candidate: Mapping[str, object],
    *,
    population_needed: bool,
    population_status: str,
) -> dict[str, Any]:
    myvariant = _mapping(
        _mapping(candidate.get("sources")).get("myvariant")
    )
    frequencies = _mapping(myvariant.get("population_frequencies"))
    usable = myvariant.get("status") == "success" and bool(frequencies)
    used = (
        population_needed
        and population_status
        in {
            "no_match",
            "unavailable",
            "invalid_response",
            "missing_identifier",
        }
        and usable
    )
    return {
        "used": used,
        "status": (
            "used"
            if used
            else (
                "unavailable"
                if population_needed and not usable
                else "not_needed"
            )
        ),
        "reason": (
            f"direct_population_{population_status}"
            if used
            else None
        ),
        "provider": "MyVariant.info",
        "upstream_sources": [
            item
            for item in myvariant.get("upstream_sources", [])
            if isinstance(item, str)
        ],
        "independent_evidence": False,
    }


def _status_summary(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if not values or set(values) == {"not_triggered"}:
        return "skipped"
    if any(value in {"available", "partial"} for value in values):
        return "success"
    if all(
        value in {"no_match", "missing_identifier", "not_triggered"}
        for value in values
    ):
        return "no_match"
    if any(
        value in {"unavailable", "invalid_response"} for value in values
    ):
        return "warning"
    return "warning"


def enrich_conditionally(
    candidates: Iterable[dict[str, Any]],
    preliminary_evidence: Iterable[Mapping[str, object]],
    *,
    population_session: requests.Session | None = None,
    literature_session: requests.Session | None = None,
) -> ConditionalEnrichmentResult:
    """Enrich only triggered variants without changing their order."""

    if isinstance(candidates, (str, bytes, dict)) or isinstance(
        preliminary_evidence,
        (str, bytes, dict),
    ):
        raise ConditionalEnrichmentError("Inputs must be iterables.")
    candidate_items = list(candidates)
    evidence_items = list(preliminary_evidence)
    if len(candidate_items) != len(evidence_items):
        raise ConditionalEnrichmentError(
            "Candidate and evidence counts must match."
        )

    enriched: list[dict[str, Any]] = []
    triggered_count = 0
    population_statuses: list[str] = []
    litvar_statuses: list[str] = []
    europe_pmc_statuses: list[str] = []
    pubmed_statuses: list[str] = []
    for index, (candidate, evidence) in enumerate(
        zip(candidate_items, evidence_items, strict=True)
    ):
        if not isinstance(candidate, dict) or not isinstance(
            evidence,
            Mapping,
        ):
            raise ConditionalEnrichmentError(
                "Conditional enrichment items must be mappings."
            )
        item = deepcopy(candidate)
        triggers = determine_enrichment_triggers(evidence, item)
        within_limit = (
            triggered_count
            < settings.CONDITIONAL_ENRICHMENT_MAX_VARIANTS
        )
        population_needed = bool(
            set(triggers)
            & {
                "vus",
                "insufficient_evidence",
                "population_evidence_ambiguity",
            }
        )
        literature_needed = "literature_evidence_need" in triggers
        if (population_needed or literature_needed) and within_limit:
            triggered_count += 1
        elif not within_limit and (
            population_needed or literature_needed
        ):
            population = _empty_population_evidence(
                status="not_triggered",
                candidate=item,
                failure_reason="analysis_enrichment_limit",
            )
            literature = _empty_literature("not_triggered")
            literature["failure_reason"] = "analysis_enrichment_limit"
            item["conditional_enrichment"] = {
                "triggered": False,
                "triggers": triggers,
                "population_frequency": population,
                "literature": literature,
                "myvariant_fallback": _myvariant_fallback(
                    item,
                    population_needed=False,
                    population_status="not_triggered",
                ),
                "warnings": [
                    "Conditional enrichment was skipped by the analysis limit."
                ],
            }
            enriched.append(item)
            population_statuses.append("not_triggered")
            litvar_statuses.append("not_triggered")
            europe_pmc_statuses.append("not_triggered")
            pubmed_statuses.append("not_triggered")
            continue

        population = (
            fetch_ensembl_population_evidence(
                item,
                session=population_session,
            )
            if population_needed
            else _empty_population_evidence(
                status="not_triggered",
                candidate=item,
            )
        )
        literature = (
            fetch_literature_evidence(
                item,
                session=literature_session,
            )
            if literature_needed
            else _empty_literature("not_triggered")
        )
        litvar_status = literature["providers"]["litvar"]["status"]
        europe_pmc_status = literature["providers"][
            "europe_pmc"
        ]["status"]
        pubmed_status = literature["providers"]["pubmed"]["status"]
        item["conditional_enrichment"] = {
            "triggered": bool(population_needed or literature_needed),
            "triggers": triggers,
            "population_frequency": population,
            "literature": literature,
            "myvariant_fallback": _myvariant_fallback(
                item,
                population_needed=population_needed,
                population_status=population["status"],
            ),
            "warnings": [
                warning
                for warning in (
                    *population["warnings"],
                    *literature["warnings"],
                )
                if isinstance(warning, str)
            ],
        }
        enriched.append(item)
        population_statuses.append(population["status"])
        litvar_statuses.append(litvar_status)
        europe_pmc_statuses.append(europe_pmc_status)
        pubmed_statuses.append(pubmed_status)

    overall = _status_summary(
        [
            *population_statuses,
            *litvar_statuses,
            *europe_pmc_statuses,
            *pubmed_statuses,
        ]
    )
    return {
        "variants": enriched,
        "status": overall,
        "message": (
            f"Conditional enrichment ran for {triggered_count} of "
            f"{len(enriched)} variants."
        ),
        "variant_count": len(enriched),
        "triggered_count": triggered_count,
        "population_status": _status_summary(population_statuses),
        "litvar_status": _status_summary(litvar_statuses),
        "europe_pmc_status": _status_summary(
            europe_pmc_statuses
        ),
        "pubmed_status": _status_summary(pubmed_statuses),
    }


__all__ = [
    "ConditionalEnrichmentError",
    "ConditionalEnrichmentResult",
    "determine_enrichment_triggers",
    "enrich_conditionally",
    "fetch_ensembl_population_evidence",
    "fetch_gnomad_evidence",
    "fetch_literature_evidence",
]
