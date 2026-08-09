"""Deterministic canonical reference normalization and URL hardening."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Literal, TypedDict, cast
from urllib.parse import quote, urlsplit, urlunsplit


MAX_CANONICAL_REFERENCES = 50
MAX_REFERENCE_TEXT_CHARS = 500
MAX_REFERENCE_URL_CHARS = 2_000

PMID_PATTERN = re.compile(
    r"(?:(?:PMID|MED)\s*:\s*)?([1-9][0-9]{0,8})",
    re.I,
)
PMCID_PATTERN = re.compile(r"(PMC[1-9][0-9]*)", re.I)
DOI_PATTERN = re.compile(r"(10\.[0-9]{4,9}/[-._;()/:A-Z0-9]+)", re.I)
CLINVAR_PATTERN = re.compile(
    r"((?:VCV|RCV|SCV)[0-9]{9}(?:\.[0-9]+)?)",
    re.I,
)
CSPEC_PATTERN = re.compile(r"([A-Z]{2}[0-9]+)", re.I)
REFERENCE_ID_PATTERN = re.compile(r"R([1-9][0-9]*)")

ReferenceURLStatus = Literal["validated", "unavailable"]


class CanonicalReferenceError(ValueError):
    """Raised when a canonical reference violates the hardened contract."""


class CanonicalReference(TypedDict):
    """One deterministic source record exposed to reports and citations."""

    reference_id: str
    source: str
    identifier_type: str | None
    identifier: str | None
    title: str | None
    canonical_url: str | None
    url_status: ReferenceURLStatus


REFERENCE_FIELDS = frozenset(CanonicalReference.__required_keys__)


def _text(
    value: object,
    *,
    maximum: int = MAX_REFERENCE_TEXT_CHARS,
) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    if any(ord(character) < 32 for character in normalized):
        return None
    return normalized


def _trusted_hostname(hostname: str) -> bool:
    exact = {
        "doi.org",
        "europepmc.org",
        "pubmed.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
        "search.clinicalgenome.org",
        "rest.ensembl.org",
        "www.ensembl.org",
        "myvariant.info",
        "www.myvariant.info",
        "genebe.net",
        "api.genebe.net",
    }
    return (
        hostname in exact
        or hostname == "clinicalgenome.org"
        or hostname.endswith(".clinicalgenome.org")
        or hostname == "genome.network"
        or hostname.endswith(".genome.network")
    )


def _trusted_url(value: object) -> str | None:
    text = _text(value, maximum=MAX_REFERENCE_URL_CHARS)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not hostname
        or not _trusted_hostname(hostname)
        or parsed.fragment
    ):
        return None
    return urlunsplit(
        (
            "https",
            parsed.netloc.casefold(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def validated_reference_url(value: object) -> str | None:
    """Return a normalized URL only when its provider domain is allowlisted."""

    return _trusted_url(value)


def _navigable_provider_url(
    source: str,
    value: str | None,
) -> str | None:
    """Reject provider write/query endpoints that are not browser records."""

    if value is None:
        return None
    path = urlsplit(value).path.casefold().rstrip("/")
    source_key = source.casefold()
    if "ensembl vep" in source_key and path.endswith(
        "/vep/homo_sapiens/region"
    ):
        return None
    if "genebe" in source_key and path.endswith(
        "/api-public/v1/variants"
    ):
        return None
    return value


def _identifier(
    source: str,
    value: object,
) -> tuple[str | None, str | None, str | None]:
    text = _text(value)
    if text is None:
        return None, None, None
    source_key = source.casefold()
    pmid = PMID_PATTERN.fullmatch(text)
    if pmid and "europe pmc" in source_key:
        normalized = pmid.group(1)
        return (
            "Europe PMC identifier",
            f"MED:{normalized}",
            f"https://europepmc.org/article/MED/{normalized}",
        )
    if pmid and any(key in source_key for key in ("pubmed", "litvar")):
        normalized = pmid.group(1)
        return (
            "PMID",
            normalized,
            f"https://pubmed.ncbi.nlm.nih.gov/{normalized}/",
        )
    pmcid = PMCID_PATTERN.fullmatch(text)
    if pmcid:
        normalized = pmcid.group(1).upper()
        return (
            "PMCID",
            normalized,
            f"https://pmc.ncbi.nlm.nih.gov/articles/{normalized}/",
        )
    doi = DOI_PATTERN.fullmatch(text.removeprefix("https://doi.org/"))
    if doi:
        normalized = doi.group(1)
        return (
            "DOI",
            normalized,
            f"https://doi.org/{quote(normalized, safe='/-._;():')}",
        )
    clinvar = CLINVAR_PATTERN.fullmatch(text)
    if clinvar:
        normalized = clinvar.group(1).upper()
        if normalized.startswith("VCV"):
            variation_id = str(int(normalized[3:].split(".", 1)[0]))
            return (
                "ClinVar accession",
                normalized,
                f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{variation_id}/",
            )
        return "ClinVar accession", normalized, None
    if "cspec" in source_key and CSPEC_PATTERN.fullmatch(text):
        return "CSpec record", text.upper(), None
    if "clingen" in source_key:
        return "ClinGen record", text, None
    return "Provider record", text, None


def canonicalize_reference(
    *,
    source: object,
    identifier: object = None,
    title: object = None,
    url: object = None,
    reference_id: str = "R1",
) -> CanonicalReference:
    """Normalize one reference and build or validate its exact link."""

    normalized_source = _text(source)
    if normalized_source is None:
        raise CanonicalReferenceError("Reference source must be bounded text.")
    if not REFERENCE_ID_PATTERN.fullmatch(reference_id):
        raise CanonicalReferenceError("Reference ID is invalid.")
    identifier_type, normalized_identifier, built_url = _identifier(
        normalized_source,
        identifier,
    )
    supplied_url = _navigable_provider_url(
        normalized_source,
        _trusted_url(url),
    )
    if (
        identifier_type == "CSpec record"
        and normalized_identifier is not None
        and supplied_url is not None
        and not urlsplit(supplied_url).path.rstrip("/").casefold().endswith(
            f"/{normalized_identifier.casefold()}"
        )
    ):
        supplied_url = None
    canonical_url = built_url or supplied_url
    normalized_title = _text(title)
    if normalized_identifier is None and canonical_url is None:
        identifier_type = None
    return {
        "reference_id": reference_id,
        "source": normalized_source,
        "identifier_type": identifier_type,
        "identifier": normalized_identifier,
        "title": normalized_title,
        "canonical_url": canonical_url,
        "url_status": "validated" if canonical_url is not None else "unavailable",
    }


def validate_canonical_reference(value: object) -> CanonicalReference:
    """Validate persisted canonical reference state without network access."""

    if not isinstance(value, dict) or set(value) != REFERENCE_FIELDS:
        raise CanonicalReferenceError("Canonical reference has invalid fields.")
    rebuilt = canonicalize_reference(
        source=value["source"],
        identifier=value["identifier"],
        title=value["title"],
        url=value["canonical_url"],
        reference_id=cast(str, value["reference_id"]),
    )
    if value["identifier_type"] != rebuilt["identifier_type"]:
        raise CanonicalReferenceError("Reference identifier type is invalid.")
    if value["canonical_url"] != rebuilt["canonical_url"]:
        raise CanonicalReferenceError("Reference canonical URL is invalid.")
    if value["url_status"] != rebuilt["url_status"]:
        raise CanonicalReferenceError("Reference URL status is invalid.")
    if value != rebuilt:
        raise CanonicalReferenceError("Canonical reference is not normalized.")
    return deepcopy(rebuilt)


def build_canonical_references(
    evidence: Mapping[str, object],
) -> list[CanonicalReference]:
    """Build a stable, deduplicated reference catalog from Evidence Object data."""

    pending: list[CanonicalReference] = []
    seen: set[tuple[str, str]] = set()

    def add(
        source: object,
        identifier: object = None,
        title: object = None,
        url: object = None,
    ) -> None:
        if len(pending) >= MAX_CANONICAL_REFERENCES:
            return
        try:
            reference = canonicalize_reference(
                source=source,
                identifier=identifier,
                title=title,
                url=url,
                reference_id=f"R{len(pending) + 1}",
            )
        except CanonicalReferenceError:
            return
        key = (
            ("url", reference["canonical_url"])
            if reference["canonical_url"]
            else (
                reference["identifier_type"] or reference["source"],
                reference["identifier"] or reference["title"] or "",
            )
        )
        if key in seen:
            return
        seen.add(cast(tuple[str, str], key))
        reference["reference_id"] = f"R{len(pending) + 1}"
        pending.append(reference)

    accession = evidence.get("clinvar_accession")
    if accession is not None:
        add("NCBI ClinVar", accession)
    raw_references = evidence.get("references")
    if isinstance(raw_references, list):
        for item in raw_references:
            if isinstance(item, Mapping):
                add(item.get("source"), url=item.get("url"))
    curations = evidence.get("clingen_curations")
    if isinstance(curations, list):
        for item in curations:
            if not isinstance(item, Mapping):
                continue
            add(
                "ClinGen / GenCC",
                item.get("disease_id"),
                item.get("disease"),
                item.get("report_url"),
            )
            pmids = item.get("pmids")
            if isinstance(pmids, list):
                for pmid in pmids:
                    add("PubMed", pmid)
    pathogenicity = evidence.get("pathogenicity")
    cspec = (
        pathogenicity.get("cspec_context")
        if isinstance(pathogenicity, Mapping)
        else None
    )
    if isinstance(cspec, list):
        for item in cspec:
            if not isinstance(item, Mapping):
                continue
            add(
                "ClinGen CSpec Registry",
                item.get("specification_id"),
                item.get("title"),
                item.get("specification_url"),
            )
            for field in ("concept_doi", "document_doi"):
                if item.get(field):
                    add("DOI", item.get(field), item.get("title"))
    enrichment = evidence.get("conditional_enrichment")
    literature = (
        enrichment.get("literature")
        if isinstance(enrichment, Mapping)
        else None
    )
    articles = (
        literature.get("articles")
        if isinstance(literature, Mapping)
        else None
    )
    if isinstance(articles, list):
        for item in articles:
            if not isinstance(item, Mapping):
                continue
            source = item.get("provider") or item.get("source") or "Literature"
            identifier = (
                item.get("pmid")
                or item.get("pmcid")
                or item.get("doi")
                or item.get("id")
            )
            add(source, identifier, item.get("title"))
    return [validate_canonical_reference(item) for item in pending]


def cited_reference_ids(*texts: object) -> list[str]:
    """Return unique bracket citation IDs in first-appearance order."""

    identifiers: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            continue
        for match in re.finditer(r"\[(R[1-9][0-9]*)\]", text):
            identifier = match.group(1)
            if identifier not in identifiers:
                identifiers.append(identifier)
    return identifiers


def render_canonical_reference_markdown(value: object) -> str:
    """Render one canonical reference with a clickable or explicit fallback target."""

    reference = validate_canonical_reference(value)
    parts = [reference["reference_id"], reference["source"]]
    if reference["identifier"] is not None:
        parts.append(
            f"{reference['identifier_type'] or 'Identifier'}: "
            f"{reference['identifier']}"
        )
    if reference["title"] is not None:
        parts.append(reference["title"])
    label = " — ".join(parts).replace("[", "\\[").replace("]", "\\]")
    if reference["canonical_url"] is not None:
        return f"[{label}]({reference['canonical_url']})"
    return f"{label} — validated link unavailable"


__all__ = [
    "CanonicalReference",
    "CanonicalReferenceError",
    "MAX_CANONICAL_REFERENCES",
    "build_canonical_references",
    "canonicalize_reference",
    "cited_reference_ids",
    "render_canonical_reference_markdown",
    "validated_reference_url",
    "validate_canonical_reference",
]
