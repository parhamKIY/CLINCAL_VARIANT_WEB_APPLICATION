"""Triggered, bounded population and literature evidence enrichment."""

from __future__ import annotations

import ast
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, TypedDict
from urllib.parse import quote, urlencode

import requests

from backend.evidence_readiness import (
    EvidenceReadinessError,
    validate_evidence_readiness_audit,
)
from backend.logging_config import get_logger
from backend.provider_resilience import (
    ProviderCircuitState,
    ProviderInvalidResponseError,
    ProviderRetryPolicy,
    ProviderStatus,
    ProviderTimeouts,
    build_provider_provenance,
    call_provider_with_policy,
    should_trigger_fallback,
)
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
GNOMAD_REQUEST_INTERVAL = 6.0
_NCBI_RATE_LOCK = Lock()
_LAST_NCBI_REQUEST_AT = 0.0
_GNOMAD_RATE_LOCK = Lock()
_LAST_GNOMAD_REQUEST_AT = 0.0
EnrichmentProgressCallback = Callable[
    [int, int, str, str],
    None,
]
ProviderProgressCallback = Callable[[str, str], None]

GNOMAD_VARIANT_QUERY = """
query Variant($variantId: String!, $dataset: DatasetId!) {
  variant(variantId: $variantId, dataset: $dataset) {
    variant_id
    ref
    alt
    rsid
    joint { ac an populations { id ac an } }
    exome { ac an ac_hom ac_hemi populations { id ac an ac_hom ac_hemi } }
    genome { ac an ac_hom ac_hemi populations { id ac an ac_hom ac_hemi } }
  }
}
""".strip()

# Verified against UCSC's hub API during the population-frequency fallback
# audit.  The tracks are assembly-specific gnomAD mirrors, not independent
# population cohorts.
UCSC_GNOMAD_TRACKS: dict[str, dict[str, object]] = {
    "GRCh37": {
        "genome": "hg19",
        "release": "gnomAD v2.1.1",
        "tracks": {
            "exome": "gnomadExomes",
            "genome": "gnomadGenomes",
        },
    },
    "GRCh38": {
        "genome": "hg38",
        "release": "gnomAD v4.1",
        "tracks": {
            "exome": "gnomadExomesVariantsV4_1",
            "genome": "gnomadGenomesVariantsV4_1",
        },
    },
}

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


@dataclass(frozen=True)
class _ParsedGraphQLResponse:
    status_code: int
    headers: Mapping[str, object]
    payload: Mapping[str, object]


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


def _wait_for_gnomad_slot(session: object) -> None:
    if not isinstance(session, requests.Session):
        return
    global _LAST_GNOMAD_REQUEST_AT
    with _GNOMAD_RATE_LOCK:
        elapsed = time.monotonic() - _LAST_GNOMAD_REQUEST_AT
        remaining = GNOMAD_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)
        _LAST_GNOMAD_REQUEST_AT = time.monotonic()


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str | int | float | bool] | None = None,
    ncbi: bool = False,
    litvar: bool = False,
    timeout: int,
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
                timeout=timeout,
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


def _request_graphql(
    session: requests.Session,
    url: str,
    *,
    query: str,
    variables: dict[str, str],
    timeout: int,
    circuit_state: ProviderCircuitState,
) -> tuple[
    object | None,
    int | None,
    int,
    ProviderStatus,
    bool,
]:
    """Return one bounded gnomAD GraphQL response with retry metadata."""

    def request(
        request_timeout: tuple[float, float],
        _attempt: int,
    ) -> object:
        _wait_for_gnomad_slot(session)
        response = session.post(
            url,
            json={"query": query, "variables": variables},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ClinicalVariantInterpretation/0.1",
            },
            timeout=request_timeout,
            verify=True,
        )
        if not 200 <= response.status_code < 300:
            return response
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderInvalidResponseError(
                http_status=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderInvalidResponseError(
                http_status=response.status_code,
            )
        if payload.get("errors"):
            raise ProviderInvalidResponseError(
                http_status=response.status_code,
            )
        headers = getattr(response, "headers", {})
        return _ParsedGraphQLResponse(
            status_code=response.status_code,
            headers=headers if isinstance(headers, Mapping) else {},
            payload=payload,
        )

    timeout_seconds = float(timeout)
    result = call_provider_with_policy(
        provider="gnomad",
        operation_name="lookup_variant",
        operation=request,
        timeouts=ProviderTimeouts(
            connect=min(timeout_seconds, 5.0),
            read=timeout_seconds,
        ),
        retry_policy=ProviderRetryPolicy(
            max_attempts=min(
                settings.CONDITIONAL_ENRICHMENT_MAX_RETRIES + 1,
                2,
            ),
            backoff_base_seconds=1.0,
            backoff_max_seconds=5.0,
            max_retry_after_seconds=5.0,
        ),
        circuit_state=circuit_state,
        sleep=time.sleep,
    )
    if result.status == "success" and isinstance(
        result.value,
        _ParsedGraphQLResponse,
    ):
        return (
            result.value.payload,
            result.value.status_code,
            result.attempts,
            result.status,
            result.circuit_open,
        )
    return (
        None,
        result.http_status,
        result.attempts,
        result.status,
        result.circuit_open,
    )


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


def _candidate_identity(
    candidate: Mapping[str, object],
) -> tuple[str, int, str, str, str] | None:
    variant = _mapping(candidate.get("variant"))
    chrom = _text(variant.get("chrom"), 32)
    pos = variant.get("pos")
    ref = _text(variant.get("ref"), 10_000)
    alt = _text(variant.get("alt"), 10_000)
    assembly = _text(candidate.get("assembly"), 20)
    if (
        chrom is None
        or isinstance(pos, bool)
        or not isinstance(pos, int)
        or pos < 1
        or ref is None
        or alt is None
        or assembly is None
    ):
        return None
    normalized_chrom = chrom.removeprefix("chr").removeprefix("CHR")
    if normalized_chrom.casefold() in {"m", "mt"}:
        normalized_chrom = "M"
    return (
        normalized_chrom,
        pos,
        ref.upper(),
        alt.upper(),
        assembly,
    )


def _ucsc_gnomad_region(
    identity: tuple[str, int, str, str, str],
) -> tuple[str, int, int]:
    """Return the exact UCSC interval for an already-normalized allele."""

    chrom, position, reference, _, _ = identity
    return (
        "chrM" if chrom == "M" else f"chr{chrom}",
        position - 1,
        position - 1 + len(reference),
    )


def _ucsc_gnomad_record_matches(
    record: Mapping[str, object],
    identity: tuple[str, int, str, str, str],
) -> bool:
    """Require exact UCSC assembly-coordinate-REF-ALT identity."""

    chromosome, start, _ = _ucsc_gnomad_region(identity)
    returned_start = record.get("chromStart")
    return (
        _text(record.get("chrom"), 32) == chromosome
        and isinstance(returned_start, int)
        and not isinstance(returned_start, bool)
        and returned_start == start
        and _text(record.get("ref"), 10_000) == identity[2]
        and _text(record.get("alt"), 10_000) == identity[3]
    )


def _ucsc_gnomad_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _ucsc_gnomad_population_rows(
    record: Mapping[str, object],
    *,
    source: str,
    alternate: str,
) -> list[dict[str, Any]]:
    """Retain only explicitly supplied UCSC ancestry rows."""

    rows: list[dict[str, Any]] = []
    for key, value in record.items():
        if not isinstance(key, str) or not key.casefold().startswith("af_"):
            continue
        suffix = key[3:]
        if not suffix or suffix.casefold() == "grpmax":
            continue
        frequency = _finite_frequency(value)
        if frequency is None:
            continue
        ac = _ucsc_gnomad_count(record.get(f"AC_{suffix}"))
        an = _ucsc_gnomad_count(record.get(f"AN_{suffix}"))
        if ac is None or an is None or an == 0 or ac > an:
            continue
        rows.append(
            {
                "population": f"{source}:{suffix.upper()}",
                "allele": alternate,
                "frequency": frequency,
                "allele_count": ac,
                "allele_number": an,
            }
        )
        if len(rows) >= MAX_POPULATIONS:
            break
    return rows


def _ucsc_gnomad_block(
    record: Mapping[str, object],
    *,
    source: str,
    alternate: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one UCSC gnomAD track record without fabricating values."""

    frequency = _finite_frequency(record.get("AF"))
    allele_count = _ucsc_gnomad_count(record.get("AC"))
    allele_number = _ucsc_gnomad_count(record.get("AN"))
    filter_status = _text(record.get("FILTER"), 100)
    if filter_status != "PASS":
        return None, "filtered_record"
    if (
        frequency is None
        or allele_count is None
        or allele_number is None
        or allele_number == 0
        or allele_count > allele_number
    ):
        return None, "insufficient_evidence"
    return (
        {
            "allele_frequency": frequency,
            "allele_count": allele_count,
            "allele_number": allele_number,
            "homozygote_count": _ucsc_gnomad_count(record.get("nhomalt")),
            "filter": filter_status,
            "popmax_frequency": _finite_frequency(
                record.get("AF_grpmax")
            ),
            "popmax_population": _text(record.get("grpmax"), 100),
            "track": source,
            "populations": _ucsc_gnomad_population_rows(
                record,
                source=source,
                alternate=alternate,
            ),
        },
        None,
    )


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
    provider: str = "gnomAD",
) -> dict[str, Any]:
    rsid = _candidate_rsid(candidate)
    identity = _candidate_identity(candidate)
    assembly = identity[4] if identity is not None else None
    dataset = {
        "GRCh37": settings.GNOMAD_DATASET_GRCH37,
        "GRCh38": settings.GNOMAD_DATASET_GRCH38,
    }.get(assembly)
    variant_id = (
        f"{identity[0]}-{identity[1]}-{identity[2]}-{identity[3]}"
        if identity is not None
        else None
    )
    is_gnomad = provider == "gnomAD"
    return {
        "status": status,
        "response_status": status,
        "provider": provider,
        "provider_version": dataset if is_gnomad else None,
        "upstream_sources": ["gnomAD"] if is_gnomad else [],
        "retrieved_at": (
            None if status == "not_triggered" else _timestamp()
        ),
        "assembly": assembly,
        "dataset": dataset if is_gnomad else None,
        "release": dataset if is_gnomad else None,
        "query_identifier": variant_id if is_gnomad else rsid,
        "http_status": http_status,
        "source_url": (
            (
                f"https://gnomad.broadinstitute.org/variant/{variant_id}"
                f"?dataset={dataset}"
            )
            if is_gnomad and variant_id is not None and dataset is not None
            else _population_source_url(rsid)
        ),
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
    *,
    alternate: str,
) -> tuple[list[dict[str, Any]], int]:
    if value is None:
        return [], 0
    if not isinstance(value, list):
        raise ProviderResponseError("invalid_schema")
    rows: list[dict[str, Any]] = []
    invalid = 0
    for item in value:
        if not isinstance(item, dict):
            invalid += 1
            continue
        population = _text(item.get("population"), 150)
        allele = _text(item.get("allele"), 10_000)
        frequency = _finite_frequency(item.get("frequency"))
        if population is None or allele is None or frequency is None:
            invalid += 1
            continue
        if allele.upper() != alternate.upper():
            continue
        rows.append(
            {
                "population": population,
                "allele": allele,
                "frequency": frequency,
            }
        )
        if len(rows) >= MAX_POPULATIONS:
            break
    return rows, invalid


def _has_exact_ensembl_mapping(
    payload: Mapping[str, object],
    candidate: Mapping[str, object],
) -> bool:
    identity = _candidate_identity(candidate)
    mappings = payload.get("mappings")
    if identity is None or not isinstance(mappings, list):
        return False
    chrom, pos, ref, alt, assembly = identity
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        alleles = _text(mapping.get("allele_string"), 20_000)
        allele_values = (
            [item.upper() for item in alleles.split("/")]
            if alleles is not None
            else []
        )
        if (
            _text(mapping.get("assembly_name"), 20) == assembly
            and _text(mapping.get("seq_region_name"), 32) == chrom
            and mapping.get("start") == pos
            and allele_values
            and allele_values[0] == ref
            and alt in allele_values[1:]
        ):
            return True
    return False


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
        provider="Ensembl REST Variation",
    )
    provenance = build_provider_provenance(
        capability="population_frequency",
        provider="ensembl_variation",
        provider_role="primary",
        primary_provider="ensembl_variation",
    )
    evidence.update(
        {key: value for key, value in provenance.items() if key != "provider"}
    )
    evidence["operational_provider"] = provenance["provider"]
    evidence["source"] = "ensembl_variation"
    evidence["fallback_for"] = None
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
                timeout=settings.ENSEMBL_VARIATION_TIMEOUT,
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
    identity = _candidate_identity(candidate)
    if identity is None or not _has_exact_ensembl_mapping(
        payload,
        candidate,
    ):
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "variant_identity_mismatch"
        return evidence
    try:
        populations, invalid = _ensembl_population_rows(
            payload.get("populations"),
            alternate=identity[3],
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
    evidence["population_frequency"] = evidence["global_maf"]
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
    circuit_state: ProviderCircuitState | None = None,
) -> dict[str, Any]:
    """Fetch exact-allele population evidence from gnomAD GraphQL."""

    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    identity = _candidate_identity(candidate)
    assembly = identity[4] if identity is not None else None
    dataset = {
        "GRCh37": settings.GNOMAD_DATASET_GRCH37,
        "GRCh38": settings.GNOMAD_DATASET_GRCH38,
    }.get(assembly)
    variant_id = (
        f"{identity[0]}-{identity[1]}-{identity[2]}-{identity[3]}"
        if identity is not None
        else None
    )
    evidence: dict[str, Any] = {
        "status": "unavailable",
        "response_status": "unavailable",
        "provider": "gnomAD",
        "provider_version": dataset,
        "upstream_sources": ["gnomAD"],
        "retrieved_at": _timestamp(),
        "assembly": assembly,
        "dataset": dataset,
        "release": dataset,
        "query_identifier": variant_id,
        "http_status": None,
        "source_url": (
            f"https://gnomad.broadinstitute.org/variant/{variant_id}"
            f"?dataset={dataset}"
            if variant_id is not None and dataset is not None
            else None
        ),
        "derivation": "direct",
        "variant_id": variant_id,
        "rsid": None,
        "minor_allele": None,
        "global_maf": None,
        "joint": None,
        "exome": None,
        "genome": None,
        "populations": [],
        "warnings": [],
        "failure_reason": None,
        "source": "gnomad",
        "fallback_for": None,
        "request_attempts": 0,
        "circuit_open": False,
    }
    provenance = build_provider_provenance(
        capability="population_frequency",
        provider="gnomad",
        provider_role="primary",
        primary_provider="gnomad",
    )
    evidence.update(
        {key: value for key, value in provenance.items() if key != "provider"}
    )
    evidence["operational_provider"] = provenance["provider"]
    if identity is None:
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_variant_identity"
        return evidence
    if dataset is None:
        evidence["status"] = "unsupported"
        evidence["response_status"] = "unsupported"
        evidence["failure_reason"] = "unsupported_assembly"
        return evidence

    owns_session = session is None
    client = session or requests.Session()
    analysis_circuit = circuit_state or ProviderCircuitState()
    try:
        (
            payload,
            http_status,
            attempts,
            provider_status,
            circuit_open,
        ) = _request_graphql(
            client,
            settings.GNOMAD_BASE_URL,
            query=GNOMAD_VARIANT_QUERY,
            variables={
                "variantId": variant_id,
                "dataset": dataset,
            },
            timeout=settings.GNOMAD_TIMEOUT,
            circuit_state=analysis_circuit,
        )
    finally:
        if owns_session:
            client.close()

    evidence["http_status"] = http_status
    evidence["request_attempts"] = attempts
    evidence["circuit_open"] = circuit_open
    if provider_status != "success":
        evidence["status"] = (
            "invalid_response"
            if provider_status == "invalid_response"
            else "unavailable"
        )
        evidence["response_status"] = evidence["status"]
        evidence["failure_reason"] = provider_status
        evidence["primary_failure"] = provider_status
        return evidence
    data = _mapping(_mapping(payload).get("data"))
    variant = data.get("variant")
    if variant is None:
        evidence["status"] = "no_match"
        evidence["response_status"] = "no_match"
        return evidence
    if not isinstance(variant, dict):
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
        return evidence
    if (
        _text(variant.get("variant_id"), 20_000) != variant_id
        or _text(variant.get("ref"), 10_000) != identity[2]
        or _text(variant.get("alt"), 10_000) != identity[3]
    ):
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "variant_identity_mismatch"
        return evidence

    def count(value: object) -> int | None:
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            else None
        )

    def block(value: object) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        ac = count(value.get("ac"))
        an = count(value.get("an"))
        if ac is None or an is None or an == 0 or ac > an:
            return None
        return {
            "allele_count": ac,
            "allele_number": an,
            "allele_frequency": ac / an,
            "homozygote_count": count(value.get("ac_hom")),
            "hemizygote_count": count(value.get("ac_hemi")),
        }

    for name in ("joint", "exome", "genome"):
        evidence[name] = block(variant.get(name))
    joint = evidence["joint"]
    if isinstance(joint, dict):
        evidence["global_maf"] = joint["allele_frequency"]
        if joint["allele_count"] > 0:
            evidence["minor_allele"] = identity[3]

    joint_source = _mapping(variant.get("joint"))
    populations = joint_source.get("populations")
    invalid_population_count = 0
    if populations is not None and not isinstance(populations, list):
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
        return evidence
    for item in populations or []:
        if not isinstance(item, dict):
            invalid_population_count += 1
            continue
        population = _text(item.get("id"), 100)
        ac = count(item.get("ac"))
        an = count(item.get("an"))
        if population is not None and (
            population.endswith("_XX")
            or population.endswith("_XY")
            or population in {"XX", "XY"}
        ):
            continue
        if (
            population is None
            or ac is None
            or an is None
            or an == 0
            or ac > an
        ):
            if population not in {None, ""}:
                invalid_population_count += 1
            continue
        evidence["populations"].append(
            {
                "population": population,
                "allele": identity[3],
                "frequency": ac / an,
                "allele_count": ac,
                "allele_number": an,
            }
        )
        if len(evidence["populations"]) >= MAX_POPULATIONS:
            break

    evidence["rsid"] = _text(variant.get("rsid"), 100)
    usable = any(
        isinstance(evidence[name], dict)
        for name in ("joint", "exome", "genome")
    )
    if not usable:
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_schema"
    elif invalid_population_count:
        evidence["status"] = "partial"
        evidence["response_status"] = "partial"
        evidence["warnings"] = [
            "gnomAD returned partial population-frequency evidence."
        ]
    else:
        evidence["status"] = "available"
        evidence["response_status"] = "available"
    return evidence


def fetch_ucsc_gnomad_evidence(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch exact-allele gnomAD mirror evidence from UCSC tracks."""

    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    identity = _candidate_identity(candidate)
    assembly = identity[4] if identity is not None else None
    configuration = UCSC_GNOMAD_TRACKS.get(assembly or "")
    release = (
        configuration.get("release")
        if isinstance(configuration, dict)
        else None
    )
    tracks = (
        configuration.get("tracks")
        if isinstance(configuration, dict)
        else None
    )
    genome = (
        configuration.get("genome")
        if isinstance(configuration, dict)
        else None
    )
    variant_id = (
        f"{identity[0]}-{identity[1]}-{identity[2]}-{identity[3]}"
        if identity is not None
        else None
    )
    endpoint = f"{settings.UCSC_GNOMAD_BASE_URL}/getData/track"
    evidence: dict[str, Any] = {
        "status": "unavailable",
        "response_status": "unavailable",
        "provider": "UCSC gnomAD",
        "provider_version": release,
        "operational_provider": "ucsc_gnomad",
        "upstream_sources": ["gnomAD"],
        "underlying_dataset": "gnomAD",
        "retrieved_at": _timestamp(),
        "assembly": assembly,
        "dataset": release,
        "release": release,
        "query_identifier": variant_id,
        "http_status": None,
        "source_url": None,
        "derivation": "mirror",
        "variant_id": variant_id,
        "global_maf": None,
        "population_frequency": None,
        "global_af": {},
        "exome": None,
        "genome": None,
        "populations": [],
        "filter_status": {},
        "track_results": [],
        "warnings": [],
        "failure_reason": None,
        "source": "ucsc_gnomad",
        "fallback_for": None,
        "request_attempts": 0,
    }
    provenance = build_provider_provenance(
        capability="population_frequency",
        provider="ucsc_gnomad",
        provider_role="primary",
        primary_provider="ucsc_gnomad",
    )
    evidence.update(
        {key: value for key, value in provenance.items() if key != "provider"}
    )
    if identity is None:
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = "invalid_variant_identity"
        return evidence
    if (
        not isinstance(genome, str)
        or not isinstance(release, str)
        or not isinstance(tracks, dict)
    ):
        evidence["status"] = "unsupported"
        evidence["response_status"] = "unsupported"
        evidence["failure_reason"] = "unsupported_assembly"
        return evidence

    chromosome, start, end = _ucsc_gnomad_region(identity)
    evidence["source_url"] = (
        f"{endpoint}?{urlencode({'genome': genome, 'chrom': chromosome, 'start': start, 'end': end})}"
    )
    owns_session = session is None
    client = session or requests.Session()
    valid_track_response = False
    exact_records = 0
    response_errors: list[str] = []
    rejected_reasons: list[str] = []
    try:
        for source, track in tracks.items():
            if source not in {"exome", "genome"} or not isinstance(track, str):
                continue
            params = {
                "genome": genome,
                "track": track,
                "chrom": chromosome,
                "start": start,
                "end": end,
            }
            try:
                payload, http_status, attempts = _request_json(
                    client,
                    endpoint,
                    params=params,
                    timeout=settings.UCSC_GNOMAD_TIMEOUT,
                )
            except requests.HTTPError as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if isinstance(status, int) and not isinstance(status, bool):
                    evidence["http_status"] = status
                response_errors.append(_request_failure_reason(exc))
                continue
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if isinstance(status, int) and not isinstance(status, bool):
                    evidence["http_status"] = status
                response_errors.append(_request_failure_reason(exc))
                continue
            except ProviderResponseError as exc:
                evidence["http_status"] = exc.http_status
                response_errors.append(exc.reason)
                continue

            evidence["http_status"] = http_status
            evidence["request_attempts"] += attempts
            if not isinstance(payload, dict):
                response_errors.append("invalid_schema")
                continue
            records = payload.get(track)
            if not isinstance(records, list):
                response_errors.append("invalid_schema")
                continue
            valid_track_response = True
            matching = [
                record
                for record in records
                if isinstance(record, dict)
                and _ucsc_gnomad_record_matches(record, identity)
            ]
            if not matching:
                continue
            exact_records += len(matching)
            # Multiple exact records for one track would be ambiguous rather
            # than evidence to merge.  UCSC's multiple ALT records are safely
            # resolved by the exact match above.
            if len(matching) != 1:
                rejected_reasons.append("ambiguous_exact_records")
                continue
            block, reason = _ucsc_gnomad_block(
                matching[0],
                source=source,
                alternate=identity[3],
            )
            if block is None:
                rejected_reasons.append(reason or "insufficient_evidence")
                continue
            evidence[source] = {
                key: value
                for key, value in block.items()
                if key != "populations"
            }
            evidence["global_af"][source] = block["allele_frequency"]
            evidence["filter_status"][source] = block["filter"]
            evidence["track_results"].append(
                {
                    "source": source,
                    "track": track,
                    "record_identity": {
                        "chrom": chromosome,
                        "pos": identity[1],
                        "ref": identity[2],
                        "alt": identity[3],
                    },
                }
            )
            evidence["populations"].extend(block["populations"])
    finally:
        if owns_session:
            client.close()

    usable_blocks = [
        evidence[source]
        for source in ("exome", "genome")
        if isinstance(evidence[source], dict)
    ]
    if usable_blocks:
        if response_errors or rejected_reasons:
            evidence["status"] = "partial"
            evidence["response_status"] = "partial"
            evidence["warnings"] = [
                "UCSC gnomAD returned partial mirror evidence."
            ]
        else:
            evidence["status"] = "available"
            evidence["response_status"] = "available"
        return evidence
    if valid_track_response and exact_records == 0 and not response_errors:
        evidence["status"] = "no_match"
        evidence["response_status"] = "no_match"
        return evidence
    if rejected_reasons:
        evidence["status"] = "invalid_response"
        evidence["response_status"] = "invalid_response"
        evidence["failure_reason"] = rejected_reasons[0]
        return evidence
    evidence["status"] = (
        "invalid_response" if "invalid_schema" in response_errors else "unavailable"
    )
    evidence["response_status"] = evidence["status"]
    evidence["failure_reason"] = (
        response_errors[0] if response_errors else "request_error"
    )
    return evidence


def _promote_population_fallback(
    fallback: dict[str, Any],
    *,
    primary: Mapping[str, object],
    fallback_provider: str,
    warning: str,
    intermediate: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Attach strict operational-fallback provenance to usable evidence."""

    primary_failure = primary.get("primary_failure")
    if not isinstance(primary_failure, str) or not should_trigger_fallback(
        primary_failure
    ):
        raise ConditionalEnrichmentError(
            "Population fallback requires an operational gnomAD failure."
        )
    provenance = build_provider_provenance(
        capability="population_frequency",
        provider=fallback_provider,
        provider_role="fallback",
        primary_provider="gnomad",
        primary_failure=primary_failure,
    )
    fallback.update(
        {
            **{key: value for key, value in provenance.items() if key != "provider"},
            "capability_provider": fallback_provider,
            "operational_provider": fallback_provider,
            "fallback_for": "gnomad",
            "primary_http_status": primary.get("http_status"),
            "primary_request_attempts": primary.get("request_attempts"),
            "primary_circuit_open": primary.get("circuit_open"),
        }
    )
    if intermediate is not None:
        fallback.update(
            {
                "intermediate_provider": "ucsc_gnomad",
                "intermediate_status": intermediate.get("status"),
                "intermediate_failure_reason": intermediate.get(
                    "failure_reason"
                ),
                "intermediate_http_status": intermediate.get("http_status"),
            }
        )
    fallback.setdefault("warnings", []).insert(0, warning)
    return fallback


def _continue_population_verification_after_no_match(
    evidence: dict[str, Any],
    *,
    provider: str,
) -> dict[str, Any]:
    """Retain a no-match continuation without mislabeling it as failure fallback."""

    evidence.update(
        {
            "capability_provider": provider,
            "preceding_provider": "gnomad",
            "preceding_status": "no_match",
        }
    )
    evidence.setdefault("warnings", []).insert(
        0,
        "Direct gnomAD returned no usable exact record; the next verification source was checked.",
    )
    return evidence


def fetch_population_evidence_with_fallback(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
    circuit_state: ProviderCircuitState | None = None,
) -> dict[str, Any]:
    """Use UCSC then Ensembl after an operational gnomAD failure."""

    primary = fetch_gnomad_evidence(
        candidate,
        session=session,
        circuit_state=circuit_state,
    )
    primary_failure = primary.get("primary_failure")
    continued_after_no_match = primary.get("status") == "no_match"
    if (
        not continued_after_no_match
        and (
            primary_failure is None
            or not should_trigger_fallback(primary_failure)
        )
    ):
        return primary

    ucsc = fetch_ucsc_gnomad_evidence(
        candidate,
        session=session,
    )
    if ucsc.get("status") in {"available", "partial"}:
        if continued_after_no_match:
            return _continue_population_verification_after_no_match(
                ucsc,
                provider="ucsc_gnomad",
            )
        return _promote_population_fallback(
            ucsc,
            primary=primary,
            fallback_provider="ucsc_gnomad",
            warning="gnomAD was unavailable; UCSC gnomAD mirror fallback was used.",
        )

    fallback = fetch_ensembl_population_evidence(
        candidate,
        session=session,
    )
    if fallback.get("status") == "missing_identifier":
        primary.update(
            {
                "fallback_attempted": False,
                "fallback_provider": "ensembl_variation",
                "fallback_status": "missing_identifier",
                "fallback_http_status": None,
                "fallback_failure_reason": fallback.get("failure_reason"),
                "intermediate_provider": "ucsc_gnomad",
                "intermediate_status": ucsc.get("status"),
                "intermediate_failure_reason": ucsc.get("failure_reason"),
                "intermediate_http_status": ucsc.get("http_status"),
            }
        )
        primary.setdefault("warnings", []).insert(
            0,
            "gnomAD was unavailable; Ensembl Variation fallback could not "
            "run because the required rsID was unavailable.",
        )
        return primary
    if fallback.get("status") in {
        "unavailable",
        "invalid_response",
    }:
        primary.update(
            {
                "fallback_attempted": True,
                "fallback_provider": "ensembl_variation",
                "fallback_status": fallback.get("status"),
                "fallback_http_status": fallback.get("http_status"),
                "fallback_failure_reason": fallback.get("failure_reason"),
                "intermediate_provider": "ucsc_gnomad",
                "intermediate_status": ucsc.get("status"),
                "intermediate_failure_reason": ucsc.get("failure_reason"),
                "intermediate_http_status": ucsc.get("http_status"),
            }
        )
        primary.setdefault("warnings", []).insert(
            0,
            "gnomAD was unavailable; Ensembl Variation fallback was "
            "attempted but returned no usable evidence.",
        )
        return primary
    if continued_after_no_match:
        return _continue_population_verification_after_no_match(
            fallback,
            provider="ensembl_variation",
        )
    return _promote_population_fallback(
        fallback,
        primary=primary,
        fallback_provider="ensembl_variation",
        warning="gnomAD and UCSC gnomAD were unavailable; Ensembl Variation fallback was used.",
        intermediate=ucsc,
    )


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
    provider_role: str = "primary",
    fallback_for: str | None = None,
    primary_failure: ProviderStatus | None = None,
    fallback_reason: str | None = None,
    article_identifiers: list[str] | None = None,
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
        "search_provider": provider,
        "provider_role": provider_role,
        "fallback_for": fallback_for,
        "primary_failure": primary_failure,
        "fallback_reason": fallback_reason,
        "article_identifiers": list(article_identifiers or []),
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


def _literature_operational_status(
    provider: Mapping[str, object],
) -> ProviderStatus:
    """Map one legacy literature failure into the shared taxonomy."""

    reason = provider.get("failure_reason")
    if reason == "timeout":
        return "timeout"
    if reason == "forbidden":
        return "forbidden"
    if reason == "rate_limited":
        return "rate_limited"
    if reason == "upstream_error":
        return "server_error"
    if provider.get("status") in {"invalid_response", "partial"}:
        return "invalid_response"
    return "unavailable"


def _literature_fallback_metadata(
    fallback_for: str,
    primary: Mapping[str, object],
) -> dict[str, object]:
    primary_failure = _literature_operational_status(primary)
    return {
        "provider_role": "fallback",
        "fallback_for": fallback_for,
        "primary_failure": primary_failure,
        "fallback_reason": primary_failure,
    }


def _article_identifier(article: Mapping[str, object]) -> str | None:
    """Return the strongest canonical identifier for one article."""

    pmid = _normalized_pmid(article.get("pmid"))
    if pmid is not None:
        return f"PMID:{pmid}"
    pmcid = _normalized_pmcid(article.get("pmcid"))
    if pmcid is not None:
        return pmcid
    doi = _normalized_doi(article.get("doi"))
    return f"DOI:{doi}" if doi is not None else None


def fetch_literature_evidence(
    candidate: Mapping[str, object],
    *,
    session: requests.Session | None = None,
    progress_callback: ProviderProgressCallback | None = None,
) -> dict[str, Any]:
    """Fetch LitVar2 citations with failure-only bounded fallbacks."""

    if not isinstance(candidate, Mapping):
        raise ConditionalEnrichmentError("Candidate must be a mapping.")
    result = _empty_literature("unavailable")

    def notify(provider: str, status: str) -> None:
        if progress_callback is not None:
            progress_callback(provider, status)

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
        for name in ("litvar", "europe_pmc", "pubmed"):
            notify(name, "missing_identifier")
        return result

    owns_session = session is None
    client = session or requests.Session()
    articles: list[dict[str, Any]] = []
    litvar_failed = False
    litvar_http_status: int | None = None
    if identifiers:
        notify("litvar", "running")
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
                timeout=settings.LITVAR_TIMEOUT,
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
                    timeout=settings.LITVAR_TIMEOUT,
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
    notify("litvar", str(result["providers"]["litvar"]["status"]))

    europe_failed = False
    europe_http_status: int | None = None
    europe_should_run = query is not None and (
        litvar_failed or not identifiers
    )
    europe_role = (
        _literature_fallback_metadata(
            "litvar",
            result["providers"]["litvar"],
        )
        if litvar_failed
        else {}
    )
    if europe_should_run:
        notify("europe_pmc", "running")
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
                timeout=settings.EUROPE_PMC_TIMEOUT,
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
                    **europe_role,
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
                    **europe_role,
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
                    **europe_role,
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
    notify(
        "europe_pmc",
        str(result["providers"]["europe_pmc"]["status"]),
    )

    if europe_failed and query is not None:
        notify("pubmed", "running")
        search_url = f"{settings.PUBMED_BASE_URL}/esearch.fcgi"
        pubmed_pmids: list[str] = []
        pubmed_http_status: int | None = None
        pubmed_role = _literature_fallback_metadata(
            "europe_pmc",
            result["providers"]["europe_pmc"],
        )
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
                timeout=settings.PUBMED_TIMEOUT,
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
                    **pubmed_role,
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
                    **pubmed_role,
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
                    **pubmed_role,
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
                    **pubmed_role,
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
                    timeout=settings.PUBMED_TIMEOUT,
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
    notify("pubmed", str(result["providers"]["pubmed"]["status"]))
    if owns_session:
        client.close()

    result["articles"] = _deduplicate_articles(articles)
    provider_markers = {
        "litvar": "LitVar2",
        "europe_pmc": "Europe PMC",
        "pubmed": "PubMed",
    }
    for name, marker in provider_markers.items():
        result["providers"][name]["article_identifiers"] = [
            identifier
            for article in result["articles"]
            if marker in article.get("source_providers", [])
            if (identifier := _article_identifier(article)) is not None
        ][: settings.CONDITIONAL_ENRICHMENT_MAX_ARTICLES]
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
    readiness_audit: Mapping[str, object] | None = None,
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
    if readiness_audit is not None:
        try:
            readiness = validate_evidence_readiness_audit(readiness_audit)
        except EvidenceReadinessError as exc:
            raise ConditionalEnrichmentError(
                "Evidence readiness audit is invalid."
            ) from exc
        planned_actions = {
            action["action"]
            for action in readiness["rescue_actions"]
            if action["status"] == "planned"
        }
        if (
            "population_evidence_rescue" in planned_actions
            and "readiness_population_deficit" not in triggers
        ):
            triggers.append("readiness_population_deficit")
        if (
            "literature_context_rescue" in planned_actions
            and "readiness_context_deficit" not in triggers
        ):
            triggers.append("readiness_context_deficit")
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
            "unavailable",
            "invalid_response",
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
    readiness_audits: Iterable[Mapping[str, object]] | None = None,
    progress_callback: EnrichmentProgressCallback | None = None,
) -> ConditionalEnrichmentResult:
    """Enrich only triggered variants without changing their order."""

    if isinstance(candidates, (str, bytes, dict)) or isinstance(
        preliminary_evidence,
        (str, bytes, dict),
    ):
        raise ConditionalEnrichmentError("Inputs must be iterables.")
    candidate_items = list(candidates)
    evidence_items = list(preliminary_evidence)
    readiness_items = (
        list(readiness_audits)
        if readiness_audits is not None
        else [None] * len(candidate_items)
    )
    if len(candidate_items) != len(evidence_items):
        raise ConditionalEnrichmentError(
            "Candidate and evidence counts must match."
        )
    if len(candidate_items) != len(readiness_items):
        raise ConditionalEnrichmentError(
            "Candidate and readiness-audit counts must match."
        )

    enriched: list[dict[str, Any]] = []
    triggered_count = 0
    population_statuses: list[str] = []
    litvar_statuses: list[str] = []
    europe_pmc_statuses: list[str] = []
    pubmed_statuses: list[str] = []
    total_steps = max(1, len(candidate_items) * 4)
    population_circuit = ProviderCircuitState()
    for index, (candidate, evidence, readiness_audit) in enumerate(
        zip(
            candidate_items,
            evidence_items,
            readiness_items,
            strict=True,
        )
    ):
        if not isinstance(candidate, dict) or not isinstance(
            evidence,
            Mapping,
        ):
            raise ConditionalEnrichmentError(
                "Conditional enrichment items must be mappings."
            )
        item = deepcopy(candidate)
        triggers = determine_enrichment_triggers(
            evidence,
            item,
            readiness_audit,
        )
        within_limit = (
            triggered_count
            < settings.CONDITIONAL_ENRICHMENT_MAX_VARIANTS
        )
        population_triggered = bool(
            set(triggers)
            & {
                "vus",
                "population_evidence_ambiguity",
                "readiness_population_deficit",
            }
        )
        literature_triggered = bool(
            set(triggers)
            & {
                "literature_evidence_need",
                "readiness_context_deficit",
            }
        )
        population_needed = (
            population_triggered
            and settings.ENABLE_GNOMAD_DEEP_LOOKUP
        )
        literature_needed = (
            literature_triggered
            and settings.ENABLE_LITERATURE_ENRICHMENT
        )
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
            if progress_callback is not None:
                progress_callback(
                    index * 4 + 1,
                    total_steps,
                    "population evidence",
                    "not_triggered",
                )
                progress_callback(
                    index * 4 + 2,
                    total_steps,
                    "LitVar",
                    "not_triggered",
                )
                progress_callback(
                    index * 4 + 3,
                    total_steps,
                    "Europe PMC",
                    "not_triggered",
                )
                progress_callback(
                    index * 4 + 4,
                    total_steps,
                    "PubMed",
                    "not_triggered",
                )
            continue

        if population_needed:
            if progress_callback is not None:
                progress_callback(
                    index * 4 + 1,
                    total_steps,
                    "population evidence",
                    "running",
                )
            population = fetch_population_evidence_with_fallback(
                item,
                session=population_session,
                circuit_state=population_circuit,
            )
        else:
            population = _empty_population_evidence(
                status="not_triggered",
                candidate=item,
            )
            if population_triggered:
                population["failure_reason"] = (
                    "disabled_by_configuration"
                )
                population["warnings"] = [
                    "gnomAD deep lookup is disabled by configuration."
                ]
        if progress_callback is not None:
            progress_callback(
                index * 4 + 1,
                total_steps,
                "population evidence",
                str(population["status"]),
            )
        if literature_needed:
            def notify_literature_provider(
                provider: str,
                status: str,
            ) -> None:
                if progress_callback is None:
                    return
                offset = {
                    "litvar": 2,
                    "europe_pmc": 3,
                    "pubmed": 4,
                }[provider]
                progress_callback(
                    index * 4 + offset,
                    total_steps,
                    provider,
                    status,
                )

            literature = fetch_literature_evidence(
                item,
                session=literature_session,
                progress_callback=notify_literature_provider,
            )
        else:
            literature = _empty_literature("not_triggered")
            if literature_triggered:
                literature["failure_reason"] = (
                    "disabled_by_configuration"
                )
                literature["warnings"] = [
                    "Literature enrichment is disabled by configuration."
                ]
            if progress_callback is not None:
                for offset, provider in enumerate(
                    ("litvar", "europe_pmc", "pubmed"),
                    start=2,
                ):
                    progress_callback(
                        index * 4 + offset,
                        total_steps,
                        provider,
                        "not_triggered",
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
    "fetch_population_evidence_with_fallback",
    "fetch_literature_evidence",
]
