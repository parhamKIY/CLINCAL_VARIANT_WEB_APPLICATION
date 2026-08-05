"""Triggered, bounded gnomAD and literature evidence enrichment."""

from __future__ import annotations

import ast
import json
import math
import time
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any, TypedDict
from urllib.parse import quote

import requests

from backend.logging_config import get_logger
from config import settings


LOGGER = get_logger("conditional_enrichment")
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
MEANINGFUL_SEVERITIES = {"moderate", "major", "critical"}
GNOMAD_DATASETS = {
    "GRCh37": ("gnomad_r2_1", "2.1.1"),
    "GRCh38": ("gnomad_r4", "4.1.0"),
}
MAX_POPULATIONS = 20
MAX_QUERY_TERMS = 8
MAX_LITVAR_RESPONSE_LINES = 1000
MAX_LITVAR_RESPONSE_CHARS = 5_000_000
NCBI_REQUEST_INTERVAL = 0.5
_NCBI_RATE_LOCK = Lock()
_LAST_NCBI_REQUEST_AT = 0.0

GNOMAD_QUERY = """
query ConditionalVariant($variantId: String!, $dataset: DatasetId!) {
  variant(variantId: $variantId, dataset: $dataset) {
    variant_id
    reference_genome
    chrom
    pos
    ref
    alt
    faf95_joint { popmax popmax_population }
    exome {
      ac
      an
      homozygote_count
      filters
      faf95 { popmax popmax_population }
      populations { id ac an homozygote_count }
    }
    genome {
      ac
      an
      homozygote_count
      filters
      faf95 { popmax popmax_population }
      populations { id ac an homozygote_count }
    }
    joint {
      ac
      an
      homozygote_count
      filters
      populations { id ac an homozygote_count }
    }
  }
}
""".strip()


class ConditionalEnrichmentError(ValueError):
    """Raised when conditional enrichment input is invalid."""


class ConditionalEnrichmentResult(TypedDict):
    """Ordered enriched candidates and bounded aggregate status."""

    variants: list[dict[str, Any]]
    status: str
    message: str
    variant_count: int
    triggered_count: int
    gnomad_status: str
    litvar_status: str
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
    method: str,
    url: str,
    *,
    params: dict[str, object] | None = None,
    json_body: dict[str, object] | None = None,
    ncbi: bool = False,
    litvar: bool = False,
) -> tuple[object, int, int]:
    """Return JSON, final HTTP status, and attempt count."""

    max_retries = settings.CONDITIONAL_ENRICHMENT_MAX_RETRIES
    for attempt in range(max_retries + 1):
        if ncbi:
            _wait_for_ncbi_slot(session)
        try:
            if method == "POST":
                response = session.post(
                    url,
                    json=json_body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "ClinicalVariantInterpretation/0.1",
                    },
                    timeout=settings.CONDITIONAL_ENRICHMENT_TIMEOUT,
                    verify=True,
                )
            else:
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
        try:
            return response.json(), response.status_code, attempt + 1
        except ValueError as exc:
            if litvar:
                return (
                    _parse_litvar_body(getattr(response, "text", None)),
                    response.status_code,
                    attempt + 1,
                )
            raise ConditionalEnrichmentError(
                "Provider returned invalid JSON."
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
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= float(value) <= 1.0
    ):
        return None
    return float(value)


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _population_block(
    value: object,
    *,
    filtering_af: object = None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConditionalEnrichmentError(
            "gnomAD population block is invalid."
        )
    ac = _non_negative_int(value.get("ac"))
    an = _non_negative_int(value.get("an"))
    hom = _non_negative_int(
        value.get("homozygote_count", value.get("ac_hom"))
    )
    if ac is None or an is None or hom is None or ac > an:
        raise ConditionalEnrichmentError(
            "gnomAD population counts are invalid."
        )
    populations: list[dict[str, Any]] = []
    raw_populations = value.get("populations")
    if raw_populations is not None and not isinstance(
        raw_populations,
        list,
    ):
        raise ConditionalEnrichmentError(
            "gnomAD ancestry populations are invalid."
        )
    for item in (raw_populations or [])[:MAX_POPULATIONS]:
        if not isinstance(item, dict):
            continue
        population_id = _text(item.get("id"), 50)
        pop_ac = _non_negative_int(item.get("ac"))
        pop_an = _non_negative_int(item.get("an"))
        pop_hom = _non_negative_int(
            item.get("homozygote_count", item.get("ac_hom"))
        )
        if (
            population_id is None
            or pop_ac is None
            or pop_an is None
            or pop_hom is None
            or pop_ac > pop_an
        ):
            continue
        populations.append(
            {
                "id": population_id,
                "ac": pop_ac,
                "an": pop_an,
                "af": pop_ac / pop_an if pop_an else None,
                "homozygote_count": pop_hom,
            }
        )
    faf = _mapping(
        filtering_af
        if filtering_af is not None
        else value.get("faf95")
    )
    filters = value.get("filters")
    return {
        "ac": ac,
        "an": an,
        "af": ac / an if an else None,
        "homozygote_count": hom,
        "filtering_af": _finite_frequency(faf.get("popmax")),
        "filtering_af_population": _text(
            faf.get("popmax_population"),
            50,
        ),
        "filters": (
            [
                item
                for item in filters[:20]
                if isinstance(item, str) and item.strip()
            ]
            if isinstance(filters, list)
            else []
        ),
        "populations": populations,
    }


def _variant_id(candidate: Mapping[str, object]) -> str | None:
    variant = _mapping(candidate.get("variant"))
    chrom = _text(variant.get("chrom"), 10)
    pos = variant.get("pos")
    ref = _text(variant.get("ref"), 10_000)
    alt = _text(variant.get("alt"), 10_000)
    if (
        chrom is None
        or chrom.removeprefix("chr") not in {
            *(str(value) for value in range(1, 23)),
            "X",
            "Y",
        }
        or isinstance(pos, bool)
        or not isinstance(pos, int)
        or pos <= 0
        or ref is None
        or alt is None
    ):
        return None
    return (
        f"{chrom.removeprefix('chr')}-{pos}-"
        f"{ref.upper()}-{alt.upper()}"
    )


def _empty_gnomad(
    *,
    status: str,
    candidate: Mapping[str, object],
    failure_reason: str | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    assembly = _text(candidate.get("assembly"), 20)
    dataset, release = GNOMAD_DATASETS.get(
        assembly or "",
        (None, None),
    )
    return {
        "status": status,
        "provider": "gnomAD",
        "provider_version": release,
        "retrieved_at": None if status == "not_triggered" else _timestamp(),
        "assembly": assembly,
        "dataset": dataset,
        "query_variant_id": _variant_id(candidate),
        "http_status": http_status,
        "exome": None,
        "genome": None,
        "joint": None,
        "warnings": [],
        "failure_reason": failure_reason,
    }


def fetch_gnomad_evidence(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch exact direct gnomAD population evidence for one variant."""

    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    evidence = _empty_gnomad(status="unavailable", candidate=candidate)
    variant_id = evidence["query_variant_id"]
    dataset = evidence["dataset"]
    if variant_id is None or dataset is None:
        evidence["status"] = "not_applicable"
        evidence["failure_reason"] = "unsupported_variant_or_assembly"
        return evidence

    owns_session = session is None
    client = session or requests.Session()
    try:
        try:
            payload, http_status, _ = _request_json(
                client,
                "POST",
                settings.GNOMAD_BASE_URL,
                json_body={
                    "query": GNOMAD_QUERY,
                    "variables": {
                        "variantId": variant_id,
                        "dataset": dataset,
                    },
                },
            )
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            evidence["http_status"] = getattr(
                response,
                "status_code",
                None,
            )
            evidence["failure_reason"] = (
                "timeout"
                if isinstance(exc, requests.Timeout)
                else "http_or_network_error"
            )
            return evidence
        except ConditionalEnrichmentError:
            evidence["status"] = "invalid_response"
            evidence["failure_reason"] = "invalid_json"
            return evidence
    finally:
        if owns_session:
            client.close()

    evidence["http_status"] = http_status
    if not isinstance(payload, dict):
        evidence["status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
        return evidence
    graph_errors = payload.get("errors")
    if graph_errors is not None and not isinstance(graph_errors, list):
        evidence["status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
        return evidence
    data = payload.get("data")
    if not isinstance(data, dict) or "variant" not in data:
        evidence["status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
        return evidence
    variant = data.get("variant")
    if variant is None and not graph_errors:
        evidence["status"] = "no_association"
        return evidence
    if not isinstance(variant, dict):
        evidence["status"] = "invalid_response"
        evidence["failure_reason"] = "graphql_error"
        return evidence
    returned_id = _text(
        variant.get("variant_id", variant.get("variantId")),
        10_050,
    )
    if returned_id is not None and returned_id != variant_id:
        evidence["status"] = "invalid_response"
        evidence["failure_reason"] = "variant_identity_mismatch"
        return evidence
    returned_assembly = _text(variant.get("reference_genome"), 20)
    if (
        returned_assembly is not None
        and returned_assembly != evidence["assembly"]
    ):
        evidence["status"] = "invalid_response"
        evidence["failure_reason"] = "assembly_mismatch"
        return evidence
    expected_parts = variant_id.split("-", 3)
    returned_chrom = _text(variant.get("chrom"), 10)
    returned_ref = _text(variant.get("ref"), 10_000)
    returned_alt = _text(variant.get("alt"), 10_000)
    returned_parts = (
        (
            returned_chrom.removeprefix("chr")
            if returned_chrom is not None
            else None
        ),
        variant.get("pos"),
        returned_ref.upper() if returned_ref is not None else None,
        returned_alt.upper() if returned_alt is not None else None,
    )
    if any(value is not None for value in returned_parts) and (
        returned_parts
        != (
            expected_parts[0],
            int(expected_parts[1]),
            expected_parts[2],
            expected_parts[3],
        )
    ):
        evidence["status"] = "invalid_response"
        evidence["failure_reason"] = "variant_identity_mismatch"
        return evidence

    valid_blocks = 0
    invalid_blocks = 0
    for field in ("exome", "genome", "joint"):
        try:
            block = _population_block(
                variant.get(field),
                filtering_af=(
                    variant.get("faf95_joint")
                    if field == "joint"
                    else None
                ),
            )
        except ConditionalEnrichmentError:
            block = None
            invalid_blocks += 1
        evidence[field] = block
        if block is not None:
            valid_blocks += 1
    if valid_blocks == 0:
        evidence["status"] = (
            "invalid_response"
            if invalid_blocks or graph_errors
            else "no_association"
        )
        evidence["failure_reason"] = (
            "invalid_population_schema"
            if invalid_blocks
            else ("graphql_error" if graph_errors else None)
        )
        return evidence
    if invalid_blocks or graph_errors:
        evidence["status"] = "partial"
        evidence["warnings"] = [
            "gnomAD returned partial population evidence."
        ]
    else:
        evidence["status"] = "available"
    return evidence


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
    http_status: int | None = None,
    failure_reason: str | None = None,
    result_count: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "http_status": http_status,
        "result_count": result_count,
        "failure_reason": failure_reason,
    }


def _empty_literature(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "provider": "LitVar2/PubMed",
        "provider_version": "LitVar2; E-utilities",
        "retrieved_at": None if status == "not_triggered" else _timestamp(),
        "query_basis": [],
        "providers": {
            "litvar": _provider_status(status),
            "pubmed": _provider_status(status),
        },
        "articles": [],
        "pmcids": [],
        "warnings": [],
        "failure_reason": None,
    }


def fetch_literature_evidence(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch bounded variant-focused LitVar2 and PubMed citations."""

    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    result = _empty_literature("unavailable")
    identifiers, gene = _candidate_identifiers(candidate)
    query = _pubmed_query(candidate, identifiers, gene)
    result["query_basis"] = [
        item for item in ([gene] + identifiers) if item is not None
    ][:MAX_QUERY_TERMS]
    if not identifiers and query is None:
        result["status"] = "not_applicable"
        result["providers"]["litvar"] = _provider_status(
            "not_applicable"
        )
        result["providers"]["pubmed"] = _provider_status(
            "not_applicable"
        )
        result["failure_reason"] = "insufficient_query_identifiers"
        return result

    owns_session = session is None
    client = session or requests.Session()
    litvar_pmids: set[str] = set()
    litvar_pmcids: set[str] = set()
    if identifiers:
        autocomplete_query = (
            f"{gene} {identifiers[0]}" if gene else identifiers[0]
        )
        try:
            payload, status, _ = _request_json(
                client,
                "GET",
                (
                    f"{settings.LITVAR_BASE_URL}/variant/"
                    "autocomplete/"
                ),
                params={"query": autocomplete_query, "limit": 10},
                ncbi=True,
                litvar=True,
            )
            litvar_id = _litvar_variant_id(
                payload,
                identifiers=identifiers,
                gene=gene,
            )
            if litvar_id is None:
                result["providers"]["litvar"] = _provider_status(
                    "no_association",
                    http_status=status,
                )
            else:
                publication_payload, publication_status, _ = _request_json(
                    client,
                    "GET",
                    (
                        f"{settings.LITVAR_BASE_URL}/variant/get/"
                        f"{quote(litvar_id, safe='')}/publications"
                    ),
                    ncbi=True,
                    litvar=True,
                )
                publication_ids = _publication_ids(publication_payload)
                litvar_pmids = publication_ids["pmids"]
                litvar_pmcids = publication_ids["pmcids"]
                result["providers"]["litvar"] = _provider_status(
                    (
                        "available"
                        if litvar_pmids or litvar_pmcids
                        else "no_association"
                    ),
                    http_status=publication_status,
                    result_count=len(litvar_pmids) + len(litvar_pmcids),
                )
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            result["providers"]["litvar"] = _provider_status(
                "unavailable",
                http_status=getattr(response, "status_code", None),
                failure_reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "http_or_network_error"
                ),
            )
        except ConditionalEnrichmentError:
            result["providers"]["litvar"] = _provider_status(
                "invalid_response",
                failure_reason="invalid_schema",
            )
    else:
        result["providers"]["litvar"] = _provider_status(
            "not_applicable"
        )

    pubmed_pmids: list[str] = []
    if query is not None:
        try:
            search_payload, search_status, _ = _request_json(
                client,
                "GET",
                f"{settings.PUBMED_BASE_URL}/esearch.fcgi",
                params={
                    "db": "pubmed",
                    "retmode": "json",
                    "retmax": settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES,
                    "sort": "relevance",
                    "tool": "clinical_variant_app",
                    "term": query,
                },
                ncbi=True,
            )
            pubmed_pmids = _pubmed_search_ids(search_payload)
            result["providers"]["pubmed"] = _provider_status(
                "available" if pubmed_pmids else "no_association",
                http_status=search_status,
                result_count=len(pubmed_pmids),
            )
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            result["providers"]["pubmed"] = _provider_status(
                "unavailable",
                http_status=getattr(response, "status_code", None),
                failure_reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "http_or_network_error"
                ),
            )
        except ConditionalEnrichmentError:
            result["providers"]["pubmed"] = _provider_status(
                "invalid_response",
                failure_reason="invalid_schema",
            )
    else:
        result["providers"]["pubmed"] = _provider_status(
            "not_applicable"
        )

    all_pmids = list(
        dict.fromkeys(
            [
                *sorted(litvar_pmids, key=int, reverse=True),
                *pubmed_pmids,
            ]
        )
    )[: settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES]
    articles = {
        pmid: {
            "pmid": pmid,
            "pmcid": None,
            "title": None,
            "journal": None,
            "publication_date": None,
            "authors": [],
            "doi": None,
            "source_providers": (
                ["LitVar2", "PubMed"]
                if pmid in litvar_pmids and pmid in pubmed_pmids
                else (["LitVar2"] if pmid in litvar_pmids else ["PubMed"])
            ),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
        for pmid in all_pmids
    }
    if all_pmids:
        try:
            summary_payload, summary_status, _ = _request_json(
                client,
                "GET",
                f"{settings.PUBMED_BASE_URL}/esummary.fcgi",
                params={
                    "db": "pubmed",
                    "retmode": "json",
                    "id": ",".join(all_pmids),
                    "tool": "clinical_variant_app",
                },
                ncbi=True,
            )
            summaries = _summary_articles(summary_payload, all_pmids)
            for pmid, summary in summaries.items():
                summary["source_providers"] = list(
                    dict.fromkeys(
                        [
                            *articles[pmid]["source_providers"],
                            "PubMed",
                        ]
                    )
                )
                articles[pmid] = summary
            if (
                summaries
                and result["providers"]["pubmed"]["status"]
                != "available"
            ):
                result["providers"]["pubmed"] = _provider_status(
                    "partial",
                    http_status=summary_status,
                    result_count=len(summaries),
                    failure_reason="search_not_available",
                )
        except requests.RequestException as exc:
            result["warnings"].append(
                "PubMed metadata was unavailable; identifiers were retained."
            )
            response = getattr(exc, "response", None)
            result["providers"]["pubmed"] = _provider_status(
                "partial" if pubmed_pmids else "unavailable",
                http_status=getattr(response, "status_code", None),
                result_count=len(pubmed_pmids),
                failure_reason=(
                    "metadata_timeout"
                    if isinstance(exc, requests.Timeout)
                    else "metadata_http_or_network_error"
                ),
            )
        except ConditionalEnrichmentError:
            result["warnings"].append(
                "PubMed metadata was invalid; identifiers were retained."
            )
            result["providers"]["pubmed"] = _provider_status(
                "partial" if pubmed_pmids else "invalid_response",
                result_count=len(pubmed_pmids),
                failure_reason="metadata_invalid_schema",
            )
    if owns_session:
        client.close()

    result["pmcids"] = sorted(litvar_pmcids)[
        : settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES
    ]
    result["articles"] = list(articles.values())
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
    elif provider_statuses <= {"no_association", "not_applicable"}:
        result["status"] = "no_association"
    elif "invalid_response" in failures and "unavailable" not in failures:
        result["status"] = "invalid_response"
    else:
        result["status"] = "unavailable"
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
    gnomad_needed: bool,
    gnomad_status: str,
) -> dict[str, Any]:
    myvariant = _mapping(
        _mapping(candidate.get("sources")).get("myvariant")
    )
    frequencies = _mapping(myvariant.get("population_frequencies"))
    usable = myvariant.get("status") == "success" and bool(frequencies)
    used = (
        gnomad_needed
        and gnomad_status
        in {
            "no_association",
            "unavailable",
            "invalid_response",
            "not_applicable",
        }
        and usable
    )
    return {
        "used": used,
        "status": (
            "used"
            if used
            else ("unavailable" if gnomad_needed and not usable else "not_needed")
        ),
        "reason": (
            f"direct_gnomad_{gnomad_status}"
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
        value in {"no_association", "not_applicable", "not_triggered"}
        for value in values
    ):
        return "no_association"
    if any(
        value in {"unavailable", "invalid_response"} for value in values
    ):
        return "warning"
    return "warning"


def enrich_conditionally(
    candidates: Iterable[dict[str, Any]],
    preliminary_evidence: Iterable[Mapping[str, object]],
    *,
    gnomad_session: requests.Session | None = None,
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
    gnomad_statuses: list[str] = []
    litvar_statuses: list[str] = []
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
        gnomad_needed = bool(
            set(triggers)
            & {
                "vus",
                "insufficient_evidence",
                "population_evidence_ambiguity",
            }
        )
        literature_needed = "literature_evidence_need" in triggers
        if (gnomad_needed or literature_needed) and within_limit:
            triggered_count += 1
        elif not within_limit and (gnomad_needed or literature_needed):
            gnomad = _empty_gnomad(
                status="not_triggered",
                candidate=item,
                failure_reason="analysis_enrichment_limit",
            )
            literature = _empty_literature("not_triggered")
            literature["failure_reason"] = "analysis_enrichment_limit"
            item["conditional_enrichment"] = {
                "triggered": False,
                "triggers": triggers,
                "gnomad": gnomad,
                "literature": literature,
                "myvariant_fallback": _myvariant_fallback(
                    item,
                    gnomad_needed=False,
                    gnomad_status="not_triggered",
                ),
                "warnings": [
                    "Conditional enrichment was skipped by the analysis limit."
                ],
            }
            enriched.append(item)
            gnomad_statuses.append("not_triggered")
            litvar_statuses.append("not_triggered")
            pubmed_statuses.append("not_triggered")
            continue

        gnomad = (
            fetch_gnomad_evidence(item, session=gnomad_session)
            if gnomad_needed
            else _empty_gnomad(status="not_triggered", candidate=item)
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
        pubmed_status = literature["providers"]["pubmed"]["status"]
        item["conditional_enrichment"] = {
            "triggered": bool(gnomad_needed or literature_needed),
            "triggers": triggers,
            "gnomad": gnomad,
            "literature": literature,
            "myvariant_fallback": _myvariant_fallback(
                item,
                gnomad_needed=gnomad_needed,
                gnomad_status=gnomad["status"],
            ),
            "warnings": [
                warning
                for warning in (
                    *gnomad["warnings"],
                    *literature["warnings"],
                )
                if isinstance(warning, str)
            ],
        }
        enriched.append(item)
        gnomad_statuses.append(gnomad["status"])
        litvar_statuses.append(litvar_status)
        pubmed_statuses.append(pubmed_status)

    overall = _status_summary(
        [
            *gnomad_statuses,
            *litvar_statuses,
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
        "gnomad_status": _status_summary(gnomad_statuses),
        "litvar_status": _status_summary(litvar_statuses),
        "pubmed_status": _status_summary(pubmed_statuses),
    }


__all__ = [
    "ConditionalEnrichmentError",
    "ConditionalEnrichmentResult",
    "determine_enrichment_triggers",
    "enrich_conditionally",
    "fetch_gnomad_evidence",
    "fetch_literature_evidence",
]
