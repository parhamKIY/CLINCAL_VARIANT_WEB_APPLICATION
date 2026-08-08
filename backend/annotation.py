"""External variant annotation and evidence standardization."""

import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import requests

from backend.logging_config import get_logger
from backend.vcf_processing import VariantData
from config import settings


LOGGER = get_logger("annotation")

# Ensembl documents a maximum of 200 variants per POST request.
MAX_VEP_BATCH_SIZE = 200
MAX_GENEBE_BATCH_SIZE = 1_000
MAX_STORED_TRANSCRIPTS = 10
MAX_GENEBE_CONSEQUENCES = 10
MAX_GENEBE_ACMG_CRITERIA = 50
MAX_CLINVAR_SEARCH_RESULTS = 20
MAX_CLINVAR_CONDITIONS = 10
MAX_CLINVAR_ACCESSIONS = 20
MAX_CLINGEN_CURATIONS = 25
MAX_CLINGEN_PMIDS = 50
MAX_CSPEC_SPECIFICATIONS = 10
MAX_CSPEC_DISEASE_QUERIES = 10
CLINVAR_REQUEST_INTERVAL = 0.34
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
REQUIRED_VARIANT_FIELDS = {"chrom", "pos", "ref", "alt"}
VEP_PROVIDER_NAME = "Ensembl VEP"
GENEBE_PROVIDER_NAME = "GeneBe"
MYVARIANT_PROVIDER_NAME = "MyVariant.info"
MYVARIANT_API_VERSION = "v1"
GENEBE_GENOMES = {
    "GRCh37": "hg19",
    "GRCh38": "hg38",
}
MYVARIANT_ASSEMBLIES = {
    "GRCh37": "hg19",
    "GRCh38": "hg38",
}
MYVARIANT_FIELDS = ",".join(
    (
        "_id",
        "dbsnp.rsid",
        "dbsnp.gene.symbol",
        "dbsnp.alleles",
        "dbnsfp.genename",
        "cadd.gene.genename",
        "gnomad_exome.af",
        "gnomad_genome.af",
        "exac.af",
    )
)
CLINVAR_REFSEQ_BASES = {
    **{
        str(chromosome): f"NC_{chromosome:06d}"
        for chromosome in range(1, 23)
    },
    "X": "NC_000023",
    "Y": "NC_000024",
    "MT": "NC_012920",
}
CLINVAR_REFSEQ_VERSIONS = {
    "GRCh37": {
        "1": 10,
        "2": 11,
        "3": 11,
        "4": 11,
        "5": 9,
        "6": 11,
        "7": 13,
        "8": 10,
        "9": 11,
        "10": 10,
        "11": 9,
        "12": 11,
        "13": 10,
        "14": 8,
        "15": 9,
        "16": 9,
        "17": 10,
        "18": 9,
        "19": 9,
        "20": 10,
        "21": 8,
        "22": 10,
        "X": 10,
        "Y": 9,
        "MT": 1,
    },
    "GRCh38": {
        "1": 11,
        "2": 12,
        "3": 12,
        "4": 12,
        "5": 10,
        "6": 12,
        "7": 14,
        "8": 11,
        "9": 12,
        "10": 11,
        "11": 10,
        "12": 12,
        "13": 11,
        "14": 9,
        "15": 10,
        "16": 10,
        "17": 11,
        "18": 10,
        "19": 10,
        "20": 11,
        "21": 9,
        "22": 11,
        "X": 11,
        "Y": 10,
        "MT": 1,
    },
}
CLINGEN_GENE_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CSPEC_IDENTIFIER_PATTERN = re.compile(r"^GN[0-9]+$")
MONDO_IDENTIFIER_PATTERN = re.compile(r"^MONDO:[0-9]{7}$")
CLINGEN_UCSC_ASSEMBLIES = {
    "GRCh37": "hg19",
    "GRCh38": "hg38",
}
IMPACT_PRIORITY = {
    "HIGH": 4,
    "MODERATE": 3,
    "LOW": 2,
    "MODIFIER": 1,
}

AnnotationData = dict[str, Any]
AnnotationProgressStatus = Literal[
    "running",
    "success",
    "warning",
    "error",
]
AnnotationProgressCallback = Callable[
    [str, AnnotationProgressStatus, str],
    None,
]
FAILED_SOURCE_STATUSES = frozenset(
    {"error", "unavailable", "invalid_response"}
)
ANNOTATION_SERVICE_LOG_NAMES = {
    "vep": "ensembl_vep",
    "genebe": "genebe",
    "myvariant": "myvariant",
    "clinvar": "ncbi_clinvar",
    "clingen": "ucsc_gencc",
    "cspec": "clingen_cspec",
}
ANNOTATION_SOURCE_LABELS = {
    "vep": "Ensembl VEP",
    "genebe": "GeneBe",
    "myvariant": "MyVariant.info",
    "clinvar": "NCBI ClinVar",
    "clingen": "ClinGen/GenCC",
    "cspec": "ClinGen CSpec Registry",
}
_CLINVAR_RATE_LOCK = Lock()
_LAST_CLINVAR_REQUEST_AT = 0.0
_ANNOTATION_CACHE_LOCK = Lock()
_ANNOTATION_CACHE: OrderedDict[
    tuple[object, ...], tuple[float, list[AnnotationData]]
] = OrderedDict()


class AnnotationError(ValueError):
    """Raised when annotation input or configuration is invalid."""


class AnnotationServiceError(RuntimeError):
    """Raised when an external annotation service cannot be used."""


class GeneBeResponseError(AnnotationServiceError):
    """Raised when GeneBe returns an invalid successful response."""


class ClinVarResponseError(AnnotationServiceError):
    """Raised when ClinVar returns malformed or non-matching evidence."""


class CSpecResponseError(AnnotationServiceError):
    """Raised when CSpec returns malformed or non-matching metadata."""


# ---------------------------------------------------------------------------
# Input validation and batching
# ---------------------------------------------------------------------------

def _validate_variant(
    variant: VariantData,
    index: int,
) -> None:
    """Validate the minimum coordinate and allele fields for VEP."""
    if not isinstance(variant, dict):
        raise AnnotationError(
            f"Variant at index {index} must be a dictionary."
        )

    missing_fields = REQUIRED_VARIANT_FIELDS.difference(variant)
    if missing_fields:
        missing_text = ", ".join(sorted(missing_fields))
        raise AnnotationError(
            f"Variant at index {index} is missing: {missing_text}."
        )

    if not isinstance(variant["pos"], int) or variant["pos"] <= 0:
        raise AnnotationError(
            f"Variant at index {index} has an invalid position."
        )

    for allele_name in ("ref", "alt"):
        allele = variant[allele_name]
        if not isinstance(allele, str) or not allele.strip():
            raise AnnotationError(
                f"Variant at index {index} has an invalid "
                f"{allele_name.upper()} allele."
            )


def _resolve_batch_size(batch_size: int | None) -> int:
    """Resolve the configured VEP batch size within Ensembl's limit."""
    resolved_size = (
        settings.VEP_BATCH_SIZE
        if batch_size is None
        else batch_size
    )

    if (
        isinstance(resolved_size, bool)
        or not isinstance(resolved_size, int)
        or resolved_size <= 0
        or resolved_size > MAX_VEP_BATCH_SIZE
    ):
        raise AnnotationError(
            "batch_size must be an integer between 1 and 200."
        )

    return resolved_size


def _resolve_retries(max_retries: int | None) -> int:
    """Resolve the number of retry attempts for transient failures."""
    resolved_retries = (
        settings.ANNOTATION_MAX_RETRIES
        if max_retries is None
        else max_retries
    )

    if (
        isinstance(resolved_retries, bool)
        or not isinstance(resolved_retries, int)
        or resolved_retries < 0
    ):
        raise AnnotationError(
            "max_retries must be a non-negative integer."
        )

    return resolved_retries


def _iter_batches(
    variants: Iterable[VariantData],
    batch_size: int,
) -> Iterator[list[tuple[str, VariantData]]]:
    """Yield bounded batches while preserving streaming input."""
    batch: list[tuple[str, VariantData]] = []

    for index, variant in enumerate(variants):
        _validate_variant(variant, index)

        # The temporary identifier lets responses map back to their exact
        # input variant even when coordinates or alleles are duplicated.
        token = f"cv_{index}"
        batch.append((token, variant))

        if len(batch) == batch_size:
            yield batch
            batch = []

    if batch:
        yield batch


def _to_vep_input(token: str, variant: VariantData) -> str:
    """Convert a standardized variant to Ensembl's VCF-like input format."""
    return (
        f"{variant['chrom']} {variant['pos']} {token} "
        f"{variant['ref']} {variant['alt']} . . ."
    )


# ---------------------------------------------------------------------------
# Ensembl VEP HTTP client
# ---------------------------------------------------------------------------

def _retry_delay(
    attempt: int,
    response: requests.Response | None = None,
) -> float:
    """Calculate a short retry delay and respect numeric Retry-After."""
    if response is not None:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            return min(float(retry_after), 10.0)

    return min(float(2**attempt), 5.0)


def _annotation_timeout(service: str) -> int:
    """Return the independently configurable deadline for one provider."""

    setting_name = {
        "ensembl_vep": "VEP_TIMEOUT",
        "genebe": "GENEBE_TIMEOUT",
        "myvariant": "MYVARIANT_TIMEOUT",
        "ncbi_clinvar": "CLINVAR_TIMEOUT",
        "ucsc_gencc": "CLINGEN_TIMEOUT",
        "clingen_cspec": "CSPEC_TIMEOUT",
    }.get(service)
    return (
        getattr(settings, setting_name)
        if setting_name is not None
        else settings.REQUEST_TIMEOUT
    )


def _log_api_call(
    *,
    service: str,
    operation: str,
    attempt: int,
    started_at: float,
    response: requests.Response | None = None,
    exception: requests.RequestException | None = None,
) -> None:
    """Log bounded external-call telemetry without request data."""

    duration_ms = max(
        0,
        round((time.perf_counter() - started_at) * 1000),
    )
    status_code = (
        response.status_code
        if response is not None
        else None
    )
    if exception is not None:
        outcome = (
            "timeout"
            if isinstance(exception, requests.Timeout)
            else "network_error"
        )
    elif status_code is not None and 200 <= status_code < 300:
        outcome = "success"
    elif status_code == 404:
        outcome = "not_found"
    else:
        outcome = "http_error"
    log_method = (
        LOGGER.info
        if outcome in {"success", "not_found"}
        else LOGGER.warning
    )
    log_method(
        "event=api_call service=%s operation=%s attempt=%d "
        "outcome=%s duration_ms=%d http_status=%s "
        "timeout_seconds=%d",
        service,
        operation,
        attempt + 1,
        outcome,
        duration_ms,
        status_code,
        _annotation_timeout(service),
    )


def _log_api_retry(
    *,
    service: str,
    operation: str,
    attempt: int,
    reason: str,
    delay_seconds: float,
) -> None:
    """Log one bounded retry decision."""

    LOGGER.warning(
        "event=api_retry_scheduled service=%s operation=%s "
        "next_attempt=%d reason=%s delay_ms=%d",
        service,
        operation,
        attempt + 2,
        reason,
        round(delay_seconds * 1000),
    )


def _post_vep_batch(
    session: requests.Session,
    vep_inputs: list[str],
    max_retries: int,
) -> list[dict[str, Any]]:
    """Send one VEP batch with timeout, retry, and response validation."""
    endpoint = f"{settings.VEP_BASE_URL}/vep/homo_sapiens/region"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params = {
        "canonical": 1,
        "hgvs": 1,
        "pick_allele_gene": 1,
        "protein": 1,
    }

    # MANE annotations are only available for GRCh38.
    if settings.GENOME_ASSEMBLY == "GRCh38":
        params["mane"] = 1

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None
        started_at = time.perf_counter()

        try:
            response = session.post(
                endpoint,
                headers=headers,
                params=params,
                json={"variants": vep_inputs},
                timeout=settings.VEP_TIMEOUT,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="ensembl_vep",
                operation="annotate_batch",
                attempt=attempt,
                started_at=started_at,
                exception=exc,
            )
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "Ensembl VEP request failed because the service "
                    "was unavailable."
                ) from exc

            delay = _retry_delay(attempt)
            _log_api_retry(
                service="ensembl_vep",
                operation="annotate_batch",
                attempt=attempt,
                reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "network_error"
                ),
                delay_seconds=delay,
            )
            time.sleep(delay)
            continue

        _log_api_call(
            service="ensembl_vep",
            operation="annotate_batch",
            attempt=attempt,
            started_at=started_at,
            response=response,
        )
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                delay = _retry_delay(attempt, response)
                _log_api_retry(
                    service="ensembl_vep",
                    operation="annotate_batch",
                    attempt=attempt,
                    reason=f"http_{response.status_code}",
                    delay_seconds=delay,
                )
                time.sleep(delay)
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "Ensembl VEP returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnotationServiceError(
                "Ensembl VEP returned invalid JSON."
            ) from exc

        if not isinstance(payload, list) or any(
            not isinstance(item, dict)
            for item in payload
        ):
            raise AnnotationServiceError(
                "Ensembl VEP returned an unexpected response structure."
            )

        return payload

    raise AnnotationServiceError(
        "Ensembl VEP retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# GeneBe HTTP client
# ---------------------------------------------------------------------------

def _to_genebe_variant(variant: VariantData) -> dict[str, Any]:
    """Build one documented GeneBe CPRA request object."""

    chromosome = str(variant["chrom"]).strip()
    if chromosome.upper() == "MT":
        chromosome = "M"
    return {
        "chr": chromosome,
        "pos": int(variant["pos"]),
        "ref": str(variant["ref"]).strip().upper(),
        "alt": str(variant["alt"]).strip().upper(),
    }


def _post_genebe_batch(
    session: requests.Session,
    variants: list[dict[str, Any]],
    max_retries: int,
) -> list[dict[str, Any]]:
    """Send one GeneBe batch with bounded reliability controls."""

    endpoint = (
        f"{settings.GENEBE_BASE_URL}/api-public/v1/variants"
    )
    params = {
        "genome": GENEBE_GENOMES[settings.GENOME_ASSEMBLY],
        "useRefseq": "true",
        "useEnsembl": "true",
        "omitAcmg": "false",
        "omitCsq": "false",
        "omitBasic": "false",
        "omitAdvanced": "false",
        "omitNormalization": "false",
        "allGenes": "false",
    }
    auth = (
        (
            settings.GENEBE_EMAIL,
            settings.GENEBE_API_KEY,
        )
        if (
            settings.GENEBE_EMAIL is not None
            and settings.GENEBE_API_KEY is not None
        )
        else None
    )

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None
        started_at = time.perf_counter()
        try:
            response = session.post(
                endpoint,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params=params,
                json=variants,
                auth=auth,
                timeout=settings.GENEBE_TIMEOUT,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="genebe",
                operation="annotate_batch",
                attempt=attempt,
                started_at=started_at,
                exception=exc,
            )
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "GeneBe request failed because the service was "
                    "unavailable."
                ) from exc

            delay = _retry_delay(attempt)
            _log_api_retry(
                service="genebe",
                operation="annotate_batch",
                attempt=attempt,
                reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "network_error"
                ),
                delay_seconds=delay,
            )
            time.sleep(delay)
            continue

        _log_api_call(
            service="genebe",
            operation="annotate_batch",
            attempt=attempt,
            started_at=started_at,
            response=response,
        )
        if (
            response.status_code in TRANSIENT_HTTP_STATUSES
            and attempt < max_retries
        ):
            delay = _retry_delay(attempt, response)
            _log_api_retry(
                service="genebe",
                operation="annotate_batch",
                attempt=attempt,
                reason=f"http_{response.status_code}",
                delay_seconds=delay,
            )
            time.sleep(delay)
            continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                f"GeneBe returned HTTP {response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise GeneBeResponseError(
                "GeneBe returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise GeneBeResponseError(
                "GeneBe returned an unexpected response structure."
            )
        response_variants = payload.get("variants")
        if (
            not isinstance(response_variants, list)
            or any(
                not isinstance(item, dict)
                for item in response_variants
            )
            or len(response_variants) != len(variants)
        ):
            raise GeneBeResponseError(
                "GeneBe returned an unexpected variant count or "
                "response structure."
            )
        return response_variants

    raise AnnotationServiceError(
        "GeneBe retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# MyVariant.info HTTP client
# ---------------------------------------------------------------------------

def _normalize_chromosome(chromosome: Any) -> str | None:
    """Convert a VCF chromosome to a standard human chromosome name."""
    normalized = str(chromosome).strip()
    if normalized.lower().startswith("chr"):
        normalized = normalized[3:]

    normalized = normalized.upper()
    if normalized == "M":
        normalized = "MT"

    if normalized in {"X", "Y", "MT"}:
        return normalized

    if normalized.isdigit() and 1 <= int(normalized) <= 22:
        return str(int(normalized))

    return None


def _normalize_variant_edit(
    variant: VariantData,
) -> tuple[str, int, str, str] | None:
    """Remove shared VCF padding from one exact small-variant edit."""
    chromosome = _normalize_chromosome(variant["chrom"])
    reference = str(variant["ref"]).strip().upper()
    alternate = str(variant["alt"]).strip().upper()

    if (
        chromosome is None
        or not reference
        or not alternate
        or not set(reference).issubset({"A", "C", "G", "T"})
        or not set(alternate).issubset({"A", "C", "G", "T"})
        or reference == alternate
    ):
        return None

    start = int(variant["pos"])

    # Remove shared VCF padding so indels use HGVS coordinates and alleles.
    while reference and alternate and reference[0] == alternate[0]:
        reference = reference[1:]
        alternate = alternate[1:]
        start += 1

    while reference and alternate and reference[-1] == alternate[-1]:
        reference = reference[:-1]
        alternate = alternate[:-1]

    return chromosome, start, reference, alternate


def _format_genomic_hgvs(
    sequence: str,
    start: int,
    reference: str,
    alternate: str,
) -> str:
    """Format one normalized small-variant edit as genomic HGVS."""
    prefix = f"{sequence}:g."

    if not reference and alternate:
        return f"{prefix}{start - 1}_{start}ins{alternate}"

    if reference and not alternate:
        end = start + len(reference) - 1
        location = str(start) if start == end else f"{start}_{end}"
        return f"{prefix}{location}del"

    if len(reference) == 1 and len(alternate) == 1:
        return f"{prefix}{start}{reference}>{alternate}"

    end = start + len(reference) - 1
    location = str(start) if start == end else f"{start}_{end}"
    return f"{prefix}{location}delins{alternate}"


def _to_myvariant_hgvs(variant: VariantData) -> str | None:
    """Convert normalized VCF alleles to a MyVariant genomic HGVS ID."""
    edit = _normalize_variant_edit(variant)
    if edit is None:
        return None

    chromosome, start, reference, alternate = edit
    return _format_genomic_hgvs(
        f"chr{chromosome}",
        start,
        reference,
        alternate,
    )


def _get_myvariant(
    session: requests.Session,
    variant: VariantData,
    max_retries: int,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Retrieve one exact MyVariant.info record with bounded retries."""
    variant_id = _to_myvariant_hgvs(variant)
    if variant_id is None:
        return None, None, (
            "MyVariant.info does not support this chromosome or allele "
            "representation."
        )

    encoded_id = quote(variant_id, safe="")
    endpoint = f"{settings.MYVARIANT_BASE_URL}/variant/{encoded_id}"
    params = {
        "assembly": MYVARIANT_ASSEMBLIES[settings.GENOME_ASSEMBLY],
        "fields": MYVARIANT_FIELDS,
    }

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None
        started_at = time.perf_counter()

        try:
            response = session.get(
                endpoint,
                params=params,
                headers={"Accept": "application/json"},
                timeout=settings.MYVARIANT_TIMEOUT,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="myvariant",
                operation="lookup_variant",
                attempt=attempt,
                started_at=started_at,
                exception=exc,
            )
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "MyVariant.info request failed because the service "
                    "was unavailable."
                ) from exc

            delay = _retry_delay(attempt)
            _log_api_retry(
                service="myvariant",
                operation="lookup_variant",
                attempt=attempt,
                reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "network_error"
                ),
                delay_seconds=delay,
            )
            time.sleep(delay)
            continue

        _log_api_call(
            service="myvariant",
            operation="lookup_variant",
            attempt=attempt,
            started_at=started_at,
            response=response,
        )
        if response.status_code == 404:
            return None, variant_id, None

        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                delay = _retry_delay(attempt, response)
                _log_api_retry(
                    service="myvariant",
                    operation="lookup_variant",
                    attempt=attempt,
                    reason=f"http_{response.status_code}",
                    delay_seconds=delay,
                )
                time.sleep(delay)
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "MyVariant.info returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnotationServiceError(
                "MyVariant.info returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise AnnotationServiceError(
                "MyVariant.info returned an unexpected response structure."
            )

        response_id = payload.get("_id")
        if (
            not isinstance(response_id, str)
            or response_id.upper() != variant_id.upper()
        ):
            raise AnnotationServiceError(
                "MyVariant.info returned a record that does not exactly "
                "match the requested assembly, chromosome, position, "
                "REF, and ALT."
            )

        return payload, variant_id, None

    raise AnnotationServiceError(
        "MyVariant.info retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# NCBI ClinVar HTTP client
# ---------------------------------------------------------------------------

def _to_clinvar_identifiers(
    variant: VariantData,
) -> tuple[str, str, str, int] | None:
    """Build exact assembly-specific HGVS and SPDI identifiers."""
    edit = _normalize_variant_edit(variant)
    if edit is None:
        return None

    chromosome, start, reference, alternate = edit
    accession_base = CLINVAR_REFSEQ_BASES[chromosome]
    accession_version = CLINVAR_REFSEQ_VERSIONS[
        settings.GENOME_ASSEMBLY
    ][chromosome]
    accession = f"{accession_base}.{accession_version}"
    hgvs = _format_genomic_hgvs(
        accession,
        start,
        reference,
        alternate,
    )
    spdi = f"{accession}:{start - 1}:{reference}:{alternate}"
    return hgvs, spdi, chromosome, start


def _wait_for_clinvar_request_slot(
    session: requests.Session,
) -> None:
    """Keep real NCBI E-utility traffic below three requests per second."""
    if not isinstance(session, requests.Session):
        return

    global _LAST_CLINVAR_REQUEST_AT

    with _CLINVAR_RATE_LOCK:
        elapsed = time.monotonic() - _LAST_CLINVAR_REQUEST_AT
        remaining = CLINVAR_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)
        _LAST_CLINVAR_REQUEST_AT = time.monotonic()


def _get_clinvar_json(
    session: requests.Session,
    endpoint_name: str,
    params: dict[str, Any],
    max_retries: int,
) -> dict[str, Any]:
    """Call one ClinVar E-utility endpoint with bounded retries."""
    endpoint = f"{settings.CLINVAR_BASE_URL}/{endpoint_name}"
    request_params = {
        "tool": "clinical_variant_app",
        **params,
    }
    operation = {
        "esearch.fcgi": "search_variant",
        "esummary.fcgi": "summarize_variant",
    }.get(endpoint_name, "request")

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None
        _wait_for_clinvar_request_slot(session)
        started_at = time.perf_counter()

        try:
            response = session.get(
                endpoint,
                params=request_params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ClinicalVariantInterpretation/0.1",
                },
                timeout=settings.CLINVAR_TIMEOUT,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="ncbi_clinvar",
                operation=operation,
                attempt=attempt,
                started_at=started_at,
                exception=exc,
            )
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "NCBI ClinVar request failed because the service "
                    "was unavailable."
                ) from exc

            delay = _retry_delay(attempt)
            _log_api_retry(
                service="ncbi_clinvar",
                operation=operation,
                attempt=attempt,
                reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "network_error"
                ),
                delay_seconds=delay,
            )
            time.sleep(delay)
            continue

        _log_api_call(
            service="ncbi_clinvar",
            operation=operation,
            attempt=attempt,
            started_at=started_at,
            response=response,
        )
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                delay = _retry_delay(attempt, response)
                _log_api_retry(
                    service="ncbi_clinvar",
                    operation=operation,
                    attempt=attempt,
                    reason=f"http_{response.status_code}",
                    delay_seconds=delay,
                )
                time.sleep(delay)
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "NCBI ClinVar returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ClinVarResponseError(
                "NCBI ClinVar returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise ClinVarResponseError(
                "NCBI ClinVar returned an unexpected response structure."
            )

        if payload.get("error"):
            if attempt < max_retries:
                delay = _retry_delay(attempt, response)
                _log_api_retry(
                    service="ncbi_clinvar",
                    operation=operation,
                    attempt=attempt,
                    reason="api_error",
                    delay_seconds=delay,
                )
                time.sleep(delay)
                continue

            raise AnnotationServiceError(
                "NCBI ClinVar returned an API error."
            )

        return payload

    raise AnnotationServiceError(
        "NCBI ClinVar retry loop ended unexpectedly."
    )


def _search_clinvar_ids(
    session: requests.Session,
    hgvs: str,
    max_retries: int,
) -> list[str]:
    """Find ClinVar Variation IDs using one exact HGVS variant name."""
    payload = _get_clinvar_json(
        session,
        "esearch.fcgi",
        {
            "db": "clinvar",
            "term": f'"{hgvs}"[varnam]',
            "retmode": "json",
            "retmax": MAX_CLINVAR_SEARCH_RESULTS,
        },
        max_retries,
    )
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise ClinVarResponseError(
            "NCBI ClinVar search returned an unexpected response structure."
        )

    raw_count = result.get("count")
    identifiers = result.get("idlist")
    try:
        count = int(raw_count)
    except (TypeError, ValueError) as exc:
        raise ClinVarResponseError(
            "NCBI ClinVar search returned an invalid result count."
        ) from exc

    if count == 0:
        return []

    if count > MAX_CLINVAR_SEARCH_RESULTS:
        raise ClinVarResponseError(
            "NCBI ClinVar returned too many records for one exact variant."
        )

    if (
        not isinstance(identifiers, list)
        or len(identifiers) != count
        or any(
            not isinstance(identifier, str) or not identifier
            for identifier in identifiers
        )
    ):
        raise ClinVarResponseError(
            "NCBI ClinVar search returned invalid Variation IDs."
        )

    return identifiers


def _get_clinvar_summaries(
    session: requests.Session,
    identifiers: list[str],
    max_retries: int,
) -> list[dict[str, Any]]:
    """Retrieve bounded ClinVar document summaries for exact search hits."""
    payload = _get_clinvar_json(
        session,
        "esummary.fcgi",
        {
            "db": "clinvar",
            "id": ",".join(identifiers),
            "retmode": "json",
            "version": "2.0",
        },
        max_retries,
    )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ClinVarResponseError(
            "NCBI ClinVar summary returned an unexpected response structure."
        )

    summaries: list[dict[str, Any]] = []
    for identifier in identifiers:
        summary = result.get(identifier)
        if (
            not isinstance(summary, dict)
            or str(summary.get("uid", "")) != identifier
        ):
            raise ClinVarResponseError(
                "NCBI ClinVar summary omitted a requested Variation ID."
            )
        summaries.append(summary)

    return summaries


def _clinvar_location_matches(
    measure: dict[str, Any],
    chromosome: str,
    start: int,
) -> bool:
    """Validate the configured assembly, chromosome, and start coordinate."""
    locations = measure.get("variation_loc")
    if not isinstance(locations, list):
        return False

    return any(
        isinstance(location, dict)
        and location.get("assembly_name") == settings.GENOME_ASSEMBLY
        and str(location.get("chr", "")).upper() == chromosome
        and str(location.get("start", "")) == str(start)
        for location in locations
    )


def _select_exact_clinvar_record(
    summaries: list[dict[str, Any]],
    expected_spdi: str,
    chromosome: str,
    start: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select exactly one summary measure matching the requested variant."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for summary in summaries:
        measures = summary.get("variation_set")
        if not isinstance(measures, list):
            continue

        for measure in measures:
            if (
                not isinstance(measure, dict)
                or not _clinvar_location_matches(
                    measure,
                    chromosome,
                    start,
                )
            ):
                continue

            # ClinVar reports canonical SPDI on GRCh38. GRCh37 allele
            # exactness is guaranteed by the exact assembly-specific HGVS
            # ESearch term and coordinate validation above.
            if (
                settings.GENOME_ASSEMBLY == "GRCh38"
                and measure.get("canonical_spdi") != expected_spdi
            ):
                continue

            matches.append((summary, measure))

    if len(matches) != 1:
        raise ClinVarResponseError(
            "NCBI ClinVar did not return exactly one record matching the "
            "requested assembly, chromosome, position, REF, and ALT."
        )

    return matches[0]


def _get_clinvar(
    session: requests.Session,
    variant: VariantData,
    max_retries: int,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    str | None,
    str | None,
]:
    """Retrieve one exact direct ClinVar record and its matching measure."""
    identifiers = _to_clinvar_identifiers(variant)
    if identifiers is None:
        return None, None, None, (
            "NCBI ClinVar does not support this chromosome or allele "
            "representation."
        )

    hgvs, spdi, chromosome, start = identifiers
    variation_ids = _search_clinvar_ids(
        session,
        hgvs,
        max_retries,
    )
    if not variation_ids:
        return None, None, hgvs, None

    summaries = _get_clinvar_summaries(
        session,
        variation_ids,
        max_retries,
    )
    summary, measure = _select_exact_clinvar_record(
        summaries,
        spdi,
        chromosome,
        start,
    )
    return summary, measure, hgvs, None


# ---------------------------------------------------------------------------
# ClinGen evidence through the UCSC GenCC track
# ---------------------------------------------------------------------------

def _select_clingen_gene(annotation: AnnotationData) -> str | None:
    """Select one unambiguous gene symbol from standardized source fields."""
    candidates = (
        annotation.get("gene"),
        annotation.get("sources", {}).get("myvariant", {}).get("gene"),
        annotation.get("sources", {}).get("clinvar", {}).get("gene"),
    )

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue

        symbol = candidate.strip()
        if CLINGEN_GENE_SYMBOL_PATTERN.fullmatch(symbol):
            return symbol

    return None


def _to_clingen_ucsc_region(
    variant: VariantData,
) -> tuple[str, int, int] | None:
    """Convert a 1-based VCF variant into a bounded UCSC query interval."""
    chromosome = _normalize_chromosome(variant.get("chrom"))
    position = variant.get("pos")
    reference = variant.get("ref")

    if (
        chromosome is None
        or not isinstance(position, int)
        or isinstance(position, bool)
        or position < 1
        or not isinstance(reference, str)
        or not reference.strip()
    ):
        return None

    ucsc_chromosome = "chrM" if chromosome == "MT" else f"chr{chromosome}"
    start = position - 1
    end = start + max(len(reference.strip()), 1)
    return ucsc_chromosome, start, end


def _get_clingen_gene_validity(
    session: requests.Session,
    variant: VariantData,
    gene: str,
    max_retries: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Retrieve exact ClinGen-submitted claims from UCSC's GenCC track."""
    region = _to_clingen_ucsc_region(variant)
    if region is None:
        return None, (
            "ClinGen evidence does not support this chromosome or "
            "coordinate representation."
        )

    chromosome, start, end = region
    assembly = CLINGEN_UCSC_ASSEMBLIES[settings.GENOME_ASSEMBLY]
    endpoint = f"{settings.CLINGEN_BASE_URL}/getData/track"
    params = {
        "genome": assembly,
        "track": "genCC",
        "chrom": chromosome,
        "start": start,
        "end": end,
    }

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None
        started_at = time.perf_counter()

        try:
            response = session.get(
                endpoint,
                params=params,
                headers={"Accept": "application/json"},
                timeout=settings.CLINGEN_TIMEOUT,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="ucsc_gencc",
                operation="lookup_gene_validity",
                attempt=attempt,
                started_at=started_at,
                exception=exc,
            )
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "UCSC GenCC request failed because the service "
                    "was unavailable."
                ) from exc

            delay = _retry_delay(attempt)
            _log_api_retry(
                service="ucsc_gencc",
                operation="lookup_gene_validity",
                attempt=attempt,
                reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "network_error"
                ),
                delay_seconds=delay,
            )
            time.sleep(delay)
            continue

        _log_api_call(
            service="ucsc_gencc",
            operation="lookup_gene_validity",
            attempt=attempt,
            started_at=started_at,
            response=response,
        )
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                delay = _retry_delay(attempt, response)
                _log_api_retry(
                    service="ucsc_gencc",
                    operation="lookup_gene_validity",
                    attempt=attempt,
                    reason=f"http_{response.status_code}",
                    delay_seconds=delay,
                )
                time.sleep(delay)
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "UCSC GenCC returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnotationServiceError(
                "UCSC GenCC returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise AnnotationServiceError(
                "UCSC GenCC returned an unexpected response structure."
            )

        if payload.get("error"):
            raise AnnotationServiceError(
                "UCSC GenCC returned an API error."
            )

        records = payload.get("genCC")
        if (
            payload.get("genome") != assembly
            or payload.get("track") != "genCC"
            or payload.get("chrom") != chromosome
            or not isinstance(records, list)
            or any(
                not isinstance(item, dict)
                for item in records
            )
        ):
            raise AnnotationServiceError(
                "UCSC GenCC returned an unexpected response structure."
            )

        exact_matches = [
            item
            for item in records
            if isinstance(item.get("gene_symbol"), str)
            and item["gene_symbol"].strip().casefold() == gene.casefold()
            and isinstance(item.get("submitter_title"), str)
            and item["submitter_title"].strip().casefold() == "clingen"
        ]
        return exact_matches, None

    raise AnnotationServiceError(
        "UCSC GenCC retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# ClinGen Criteria Specification Registry client
# ---------------------------------------------------------------------------

def _get_cspec_entity(
    session: requests.Session,
    entity_type: str,
    identifier: str,
    max_retries: int,
) -> dict[str, Any] | None:
    """Retrieve one exact public CSpec entity with bounded retries."""
    endpoint = (
        f"{settings.CSPEC_BASE_URL}/{entity_type}/id/"
        f"{quote(identifier, safe='')}"
    )

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None
        started_at = time.perf_counter()

        try:
            response = session.get(
                endpoint,
                headers={"Accept": "application/json"},
                timeout=settings.CSPEC_TIMEOUT,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="clingen_cspec",
                operation=f"lookup_{entity_type.casefold()}",
                attempt=attempt,
                started_at=started_at,
                exception=exc,
            )
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "ClinGen CSpec request failed because the service "
                    "was unavailable."
                ) from exc

            delay = _retry_delay(attempt)
            _log_api_retry(
                service="clingen_cspec",
                operation=f"lookup_{entity_type.casefold()}",
                attempt=attempt,
                reason=(
                    "timeout"
                    if isinstance(exc, requests.Timeout)
                    else "network_error"
                ),
                delay_seconds=delay,
            )
            time.sleep(delay)
            continue

        _log_api_call(
            service="clingen_cspec",
            operation=f"lookup_{entity_type.casefold()}",
            attempt=attempt,
            started_at=started_at,
            response=response,
        )
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                delay = _retry_delay(attempt, response)
                _log_api_retry(
                    service="clingen_cspec",
                    operation=f"lookup_{entity_type.casefold()}",
                    attempt=attempt,
                    reason=f"http_{response.status_code}",
                    delay_seconds=delay,
                )
                time.sleep(delay)
                continue

        if response.status_code == 404:
            return None

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "ClinGen CSpec returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CSpecResponseError(
                "ClinGen CSpec returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise CSpecResponseError(
                "ClinGen CSpec returned an unexpected response structure."
            )

        data = payload.get("data")
        status = payload.get("status")
        if (
            not isinstance(data, dict)
            or not isinstance(status, dict)
            or status.get("code") != 200
            or data.get("entType") != entity_type
            or str(data.get("entId", "")).casefold()
            != identifier.casefold()
        ):
            raise CSpecResponseError(
                "ClinGen CSpec returned a non-matching entity."
            )

        return data

    raise AnnotationServiceError(
        "ClinGen CSpec retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# Response cleaning and unified evidence output
# ---------------------------------------------------------------------------

def _extract_response_token(response: dict[str, Any]) -> str | None:
    """Read the temporary input identifier returned by VEP."""
    raw_input = response.get("input")
    if not isinstance(raw_input, str):
        return None

    fields = raw_input.split()
    if len(fields) < 3 or not fields[2].startswith("cv_"):
        return None

    return fields[2]


def _retrieval_timestamp() -> str:
    """Return one UTC timestamp for provider provenance."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _optional_text(value: Any) -> str | None:
    """Normalize one optional provider string."""

    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _optional_score(value: Any) -> float | None:
    """Normalize one optional finite predictor score."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _clean_predictors(
    transcript: dict[str, Any],
) -> dict[str, dict[str, str | float | None]]:
    """Keep only enabled VEP predictor results with explicit missingness."""

    predictors: dict[str, dict[str, str | float | None]] = {}
    for predictor in ("sift", "polyphen"):
        prediction = _optional_text(
            transcript.get(f"{predictor}_prediction")
        )
        score = _optional_score(
            transcript.get(f"{predictor}_score")
        )
        if prediction is not None or score is not None:
            predictors[predictor] = {
                "prediction": prediction,
                "score": score,
            }
    return predictors


def _clean_transcript(
    transcript: dict[str, Any],
) -> dict[str, Any]:
    """Keep only transcript evidence used by later pipeline stages."""
    raw_consequence_terms = transcript.get("consequence_terms")
    consequence_terms = (
        [
            term.strip()
            for term in raw_consequence_terms
            if isinstance(term, str) and term.strip()
        ]
        if isinstance(raw_consequence_terms, list)
        else []
    )

    return {
        "gene_symbol": _optional_text(transcript.get("gene_symbol")),
        "gene_id": _optional_text(transcript.get("gene_id")),
        "transcript_id": _optional_text(
            transcript.get("transcript_id")
        ),
        "biotype": _optional_text(transcript.get("biotype")),
        "consequence_terms": consequence_terms,
        "impact": _optional_text(transcript.get("impact")),
        "hgvsc": _optional_text(transcript.get("hgvsc")),
        "hgvsp": _optional_text(transcript.get("hgvsp")),
        "canonical": transcript.get("canonical") == 1,
        "mane_select": _optional_text(transcript.get("mane_select")),
        "mane_plus_clinical": _optional_text(
            transcript.get("mane_plus_clinical")
        ),
        "predictors": _clean_predictors(transcript),
    }


def _select_representative_transcript(
    transcripts: list[dict[str, Any]],
    most_severe_consequence: str | None,
) -> dict[str, Any] | None:
    """Choose one useful transcript while retaining all cleaned evidence."""
    if not transcripts:
        return None

    severe_matches = [
        transcript
        for transcript in transcripts
        if most_severe_consequence
        in transcript.get("consequence_terms", [])
    ]
    candidates = severe_matches or transcripts

    # Prefer MANE Select and canonical protein-coding transcripts. Impact is
    # only a final tie-breaker; the complete transcript list is still retained.
    return max(
        candidates,
        key=_transcript_priority,
    )


def _transcript_priority(
    transcript: dict[str, Any],
) -> tuple[bool, bool, bool, int]:
    """Rank cleaned transcripts using stable clinical metadata."""
    return (
        bool(transcript.get("mane_select")),
        bool(transcript.get("canonical")),
        transcript.get("biotype") == "protein_coding",
        IMPACT_PRIORITY.get(
            str(transcript.get("impact")),
            0,
        ),
    )


def _limit_transcripts(
    transcripts: list[dict[str, Any]],
    most_severe_consequence: str | None,
) -> list[dict[str, Any]]:
    """Keep a bounded set of the most relevant cleaned transcripts."""
    severe_matches = [
        transcript
        for transcript in transcripts
        if most_severe_consequence
        in transcript.get("consequence_terms", [])
    ]
    candidates = severe_matches or transcripts

    return sorted(
        candidates,
        key=_transcript_priority,
        reverse=True,
    )[:MAX_STORED_TRANSCRIPTS]


def _base_annotation(
    variant: VariantData,
    *,
    status: str,
    warning: str | None = None,
    retrieved_at: str | None = None,
) -> AnnotationData:
    """Build a stable annotation object for success or failure states."""
    warnings_list = [warning] if warning else []

    return {
        "variant": dict(variant),
        "assembly": settings.GENOME_ASSEMBLY,
        "gene": None,
        "gene_id": None,
        "transcript": None,
        "consequence": None,
        "impact": None,
        "hgvsc": None,
        "hgvsp": None,
        "protein_change": None,
        "is_canonical": None,
        "mane_select": None,
        "mane_plus_clinical": None,
        "predictors": {},
        "population_frequency": None,
        "sources": {
            "vep": {
                "status": status,
                "provider": VEP_PROVIDER_NAME,
                "provider_version": None,
                "retrieved_at": retrieved_at or _retrieval_timestamp(),
                "assembly": settings.GENOME_ASSEMBLY,
                "most_severe_consequence": None,
                "transcript_consequences": [],
                "total_transcript_consequences": 0,
                "transcripts_truncated": False,
            },
            "genebe": {
                "status": "pending",
                "provider": GENEBE_PROVIDER_NAME,
                "provider_version": None,
                "retrieved_at": None,
                "request_assembly": settings.GENOME_ASSEMBLY,
                "returned_variant": None,
                "representation_mismatch": False,
                "transcript_mismatch": False,
                "gene": None,
                "gene_hgnc_id": None,
                "transcript": None,
                "effect": None,
                "consequences": [],
                "total_consequences": 0,
                "consequences_truncated": False,
                "automated_acmg_classification": None,
                "automated_acmg_criteria": [],
                "automated_acmg_score": None,
                "population_annotations": {},
                "predictor_annotations": {},
                "clinvar_derived": {
                    "upstream_source": "ClinVar",
                    "classification": None,
                    "review_status": None,
                    "disease": None,
                },
            },
            "myvariant": {
                "status": "pending",
                "provider": MYVARIANT_PROVIDER_NAME,
                "provider_version": MYVARIANT_API_VERSION,
                "retrieved_at": None,
                "upstream_sources": [],
                "variant_id": None,
                "rsid": None,
                "gene": None,
                "population_frequencies": {},
                "max_population_frequency": None,
            },
            "clinvar": {
                "status": "pending",
                "direct_verification_status": "pending",
                "provider": "NCBI ClinVar",
                "provider_version": None,
                "api": "NCBI E-utilities",
                "api_version": "ESummary 2.0",
                "source_type": "direct",
                "retrieved_at": None,
                "assembly": settings.GENOME_ASSEMBLY,
                "query_hgvs": None,
                "variation_id": None,
                "accession": None,
                "accession_version": None,
                "gene": None,
                "clinical_significance": None,
                "review_status": None,
                "last_evaluated": None,
                "conditions": [],
                "condition_count": 0,
                "conditions_truncated": False,
                "scv_accessions": [],
                "scv_accession_count": 0,
                "scv_accessions_truncated": False,
                "rcv_accessions": [],
                "rcv_accession_count": 0,
                "rcv_accessions_truncated": False,
                "conflicting_submissions": {
                    "status": "unknown",
                    "detected": None,
                    "basis": "aggregate_review_status",
                    "details": None,
                },
            },
            "clingen": {
                "status": "pending",
                "provider": "ClinGen",
                "provider_version": None,
                "data_provider": "UCSC GenCC",
                "retrieved_at": None,
                "assembly": settings.GENOME_ASSEMBLY,
                "query_region": None,
                "query_gene": None,
                "gene": None,
                "gene_id": None,
                "context_type": "gene_disease_validity",
                "classification_effect": "context_only",
                "curations": [],
                "curation_count": 0,
                "curations_truncated": False,
            },
            "cspec": {
                "status": "pending",
                "provider": "ClinGen CSpec Registry",
                "provider_version": None,
                "retrieved_at": None,
                "query_gene": None,
                "query_disease_ids": [],
                "disease_queries_truncated": False,
                "specification_available": False,
                "specifications": [],
                "specification_count": 0,
                "specifications_truncated": False,
                "context_type": "gene_disease_acmg_specification",
                "classification_effect": "context_only",
                "rule_logic_applied": False,
            },
        },
        "references": [
            {
                "source": "Ensembl VEP",
                "url": (
                    f"{settings.VEP_BASE_URL}"
                    "/vep/homo_sapiens/region"
                ),
            }
        ],
        "warnings": warnings_list,
    }


def _standardize_vep_response(
    variant: VariantData,
    response: dict[str, Any],
) -> AnnotationData:
    """Convert raw VEP JSON into the project's unified evidence structure."""
    annotation = _base_annotation(variant, status="success")
    response_assembly = response.get("assembly_name")

    if response_assembly != settings.GENOME_ASSEMBLY:
        message = (
            "Ensembl VEP assembly mismatch: expected "
            f"{settings.GENOME_ASSEMBLY}, received "
            f"{response_assembly or 'unknown'}."
        )
        annotation["sources"]["vep"]["status"] = "error"
        annotation["warnings"].append(message)
        return annotation

    raw_transcripts = response.get("transcript_consequences", [])
    if not isinstance(raw_transcripts, list):
        raw_transcripts = []

    cleaned_transcripts = [
        _clean_transcript(transcript)
        for transcript in raw_transcripts
        if isinstance(transcript, dict)
    ]
    most_severe = response.get("most_severe_consequence")
    if not isinstance(most_severe, str):
        most_severe = None

    representative = _select_representative_transcript(
        cleaned_transcripts,
        most_severe,
    )
    stored_transcripts = _limit_transcripts(
        cleaned_transcripts,
        most_severe,
    )

    annotation["consequence"] = most_severe
    annotation["sources"]["vep"].update(
        {
            "provider_version": _optional_text(
                response.get("version")
            ),
            "most_severe_consequence": most_severe,
            "transcript_consequences": stored_transcripts,
            "total_transcript_consequences": len(
                cleaned_transcripts
            ),
            "transcripts_truncated": (
                len(stored_transcripts)
                < len(cleaned_transcripts)
            ),
        }
    )

    if representative is None:
        annotation["warnings"].append(
            "Ensembl VEP returned no transcript consequences."
        )
        return annotation

    annotation.update(
        {
            "gene": (
                representative.get("gene_symbol")
                or representative.get("gene_id")
            ),
            "gene_id": representative.get("gene_id"),
            "transcript": representative.get("transcript_id"),
            "impact": representative.get("impact"),
            "hgvsc": representative.get("hgvsc"),
            "hgvsp": representative.get("hgvsp"),
            "protein_change": representative.get("hgvsp"),
            "is_canonical": representative.get("canonical"),
            "mane_select": representative.get("mane_select"),
            "mane_plus_clinical": representative.get(
                "mane_plus_clinical"
            ),
            "predictors": dict(
                representative.get("predictors", {})
            ),
        }
    )
    return annotation


def _optional_integer(value: Any) -> int | None:
    """Normalize one optional non-negative provider count."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        return None
    return value


def _clean_genebe_consequence(
    consequence: dict[str, Any],
) -> dict[str, Any]:
    """Keep one bounded GeneBe transcript consequence."""

    raw_terms = consequence.get("consequences")
    terms = (
        [
            term.strip()
            for term in raw_terms
            if isinstance(term, str) and term.strip()
        ][:20]
        if isinstance(raw_terms, list)
        else []
    )
    canonical = consequence.get("canonical")
    protein_coding = consequence.get("protein_coding")
    return {
        "gene": _optional_text(consequence.get("gene_symbol")),
        "gene_hgnc_id": _optional_integer(
            consequence.get("gene_hgnc_id")
        ),
        "transcript": _optional_text(
            consequence.get("transcript")
        ),
        "protein_id": _optional_text(
            consequence.get("protein_id")
        ),
        "biotype": _optional_text(consequence.get("biotype")),
        "consequence_terms": terms,
        "hgvs_c": _optional_text(consequence.get("hgvs_c")),
        "hgvs_p": _optional_text(consequence.get("hgvs_p")),
        "canonical": (
            canonical if isinstance(canonical, bool) else None
        ),
        "protein_coding": (
            protein_coding
            if isinstance(protein_coding, bool)
            else None
        ),
        "mane_select": _optional_text(
            consequence.get("mane_select")
        ),
        "mane_plus_clinical": _optional_text(
            consequence.get("mane_plus")
        ),
    }


def _clean_genebe_criteria(value: Any) -> list[str]:
    """Normalize GeneBe ACMG criteria without interpreting them."""

    if isinstance(value, str):
        raw_criteria: Iterable[Any] = value.split(",")
    elif isinstance(value, list):
        raw_criteria = value
    else:
        return []

    criteria: list[str] = []
    for raw_criterion in raw_criteria:
        criterion = _optional_text(raw_criterion)
        if criterion is None or criterion in criteria:
            continue
        criteria.append(criterion)
        if len(criteria) >= MAX_GENEBE_ACMG_CRITERIA:
            break
    return criteria


def _clean_genebe_population(
    response: dict[str, Any],
) -> dict[str, float | int]:
    """Select documented population fields from GeneBe."""

    population: dict[str, float | int] = {}
    for field in (
        "frequency_reference_population",
        "gnomad_exomes_af",
        "gnomad_genomes_af",
    ):
        value = _optional_score(response.get(field))
        if value is not None and 0.0 <= value <= 1.0:
            population[field] = value

    for field in (
        "hom_count_reference_population",
        "allele_count_reference_population",
        "gnomad_exomes_ac",
        "gnomad_genomes_ac",
        "gnomad_exomes_homalt",
        "gnomad_genomes_homalt",
        "gnomad_mito_homoplasmic",
        "gnomad_mito_heteroplasmic",
    ):
        value = _optional_integer(response.get(field))
        if value is not None:
            population[field] = value
    return population


def _clean_genebe_predictors(
    response: dict[str, Any],
) -> dict[str, dict[str, str | float | None]]:
    """Select documented GeneBe predictor results."""

    predictors: dict[str, dict[str, str | float | None]] = {}
    predictor_fields = {
        "computational_selected": (
            "computational_score_selected",
            "computational_prediction_selected",
            "computational_source_selected",
        ),
        "splice_selected": (
            "splice_score_selected",
            "splice_prediction_selected",
            "splice_source_selected",
        ),
        "revel": (
            "revel_score",
            "revel_prediction",
            None,
        ),
        "alphamissense": (
            "alphamissense_score",
            "alphamissense_prediction",
            None,
        ),
        "bayesdel_noaf": (
            "bayesdelnoaf_score",
            "bayesdelnoaf_prediction",
            None,
        ),
        "phylop_100way": (
            "phylop100way_score",
            "phylop100way_prediction",
            None,
        ),
        "spliceai_max": (
            "spliceai_max_score",
            "spliceai_max_prediction",
            None,
        ),
        "dbscsnv_ada": (
            "dbscsnv_ada_score",
            "dbscsnv_ada_prediction",
            None,
        ),
    }
    for name, (score_field, prediction_field, source_field) in (
        predictor_fields.items()
    ):
        score = _optional_score(response.get(score_field))
        prediction = _optional_text(
            response.get(prediction_field)
        )
        source = (
            _optional_text(response.get(source_field))
            if source_field is not None
            else None
        )
        if score is None and prediction is None and source is None:
            continue
        predictors[name] = {
            "score": score,
            "prediction": prediction,
            "source": source,
        }
    return predictors


def _standardize_genebe_response(
    annotation: AnnotationData,
    response: dict[str, Any],
) -> None:
    """Attach independent, cleaned GeneBe evidence to one annotation."""

    chromosome = _normalize_chromosome(response.get("chr"))
    position = response.get("pos")
    reference = _optional_text(response.get("ref"))
    alternate = _optional_text(response.get("alt"))
    if (
        chromosome is None
        or isinstance(position, bool)
        or not isinstance(position, int)
        or position <= 0
        or reference is None
        or alternate is None
    ):
        raise GeneBeResponseError(
            "GeneBe returned an invalid variant representation."
        )

    returned_variant = {
        "chrom": chromosome,
        "pos": position,
        "ref": reference.upper(),
        "alt": alternate.upper(),
    }
    input_variant = {
        "chrom": _normalize_chromosome(
            annotation["variant"]["chrom"]
        ),
        "pos": int(annotation["variant"]["pos"]),
        "ref": str(annotation["variant"]["ref"]).upper(),
        "alt": str(annotation["variant"]["alt"]).upper(),
    }

    raw_consequences = response.get("consequences")
    cleaned_consequences = (
        [
            _clean_genebe_consequence(item)
            for item in raw_consequences
            if isinstance(item, dict)
        ]
        if isinstance(raw_consequences, list)
        else []
    )
    stored_consequences = cleaned_consequences[
        :MAX_GENEBE_CONSEQUENCES
    ]
    gene = _optional_text(response.get("gene_symbol"))
    transcript = _optional_text(response.get("transcript"))
    vep_transcript = _optional_text(annotation.get("transcript"))

    annotation["sources"]["genebe"].update(
        {
            "status": "success",
            "provider_version": _optional_text(
                response.get("version")
            ),
            "retrieved_at": _retrieval_timestamp(),
            "returned_variant": returned_variant,
            "representation_mismatch": (
                returned_variant != input_variant
            ),
            "transcript_mismatch": (
                transcript is not None
                and vep_transcript is not None
                and transcript != vep_transcript
            ),
            "gene": gene,
            "gene_hgnc_id": _optional_integer(
                response.get("gene_hgnc_id")
            ),
            "transcript": transcript,
            "effect": _optional_text(response.get("effect")),
            "consequences": stored_consequences,
            "total_consequences": len(cleaned_consequences),
            "consequences_truncated": (
                len(stored_consequences)
                < len(cleaned_consequences)
            ),
            "automated_acmg_classification": _optional_text(
                response.get("acmg_classification")
            ),
            "automated_acmg_criteria": _clean_genebe_criteria(
                response.get("acmg_criteria")
            ),
            "automated_acmg_score": _optional_score(
                response.get("acmg_score")
            ),
            "population_annotations": _clean_genebe_population(
                response
            ),
            "predictor_annotations": _clean_genebe_predictors(
                response
            ),
            "clinvar_derived": {
                "upstream_source": "ClinVar",
                "classification": _optional_text(
                    response.get("clinvar_classification")
                ),
                "review_status": _optional_text(
                    response.get("clinvar_review_status")
                ),
                "disease": _optional_text(
                    response.get("clinvar_disease")
                ),
            },
        }
    )
    annotation["references"].append(
        {
            "source": "GeneBe automated annotation",
            "url": (
                f"{settings.GENEBE_BASE_URL}"
                "/api-public/v1/variants"
            ),
        }
    )


def _annotate_with_genebe(
    annotations: list[AnnotationData],
    session: requests.Session,
    max_retries: int,
) -> None:
    """Batch GeneBe evidence without overwriting VEP annotations."""

    for offset in range(0, len(annotations), MAX_GENEBE_BATCH_SIZE):
        batch = annotations[offset:offset + MAX_GENEBE_BATCH_SIZE]
        request_variants = [
            _to_genebe_variant(annotation["variant"])
            for annotation in batch
        ]
        try:
            responses = _post_genebe_batch(
                session,
                request_variants,
                max_retries,
            )
        except AnnotationServiceError as exc:
            LOGGER.error(
                "event=annotation_source_failed service=genebe "
                "error_type=%s",
                type(exc).__name__,
            )
            for annotation in batch:
                annotation["sources"]["genebe"].update(
                    {
                        "status": (
                            "invalid_response"
                            if isinstance(exc, GeneBeResponseError)
                            else "unavailable"
                        ),
                        "retrieved_at": _retrieval_timestamp(),
                    }
                )
                annotation["warnings"].append(str(exc))
            continue

        for annotation, response in zip(batch, responses, strict=True):
            try:
                _standardize_genebe_response(annotation, response)
            except AnnotationServiceError as exc:
                annotation["sources"]["genebe"].update(
                    {
                        "status": "invalid_response",
                        "retrieved_at": _retrieval_timestamp(),
                    }
                )
                annotation["warnings"].append(str(exc))


def _iter_source_records(value: Any) -> Iterator[dict[str, Any]]:
    """Yield dictionary records from a single or repeated source field."""
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _first_nested_string(
    records: Any,
    *path: str,
) -> str | None:
    """Read the first non-empty string at a nested source path."""
    for record in _iter_source_records(records):
        value: Any = record
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _valid_frequency(value: Any) -> float | None:
    """Return a finite allele frequency in the inclusive range zero to one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    frequency = float(value)
    if not math.isfinite(frequency) or not 0.0 <= frequency <= 1.0:
        return None

    return frequency


def _extract_global_frequency(source: Any) -> float | None:
    """Extract the largest global AF from one MyVariant population source."""
    frequencies: list[float] = []

    for record in _iter_source_records(source):
        raw_frequency = record.get("af")
        if isinstance(raw_frequency, dict):
            raw_frequency = raw_frequency.get("af")

        frequency = _valid_frequency(raw_frequency)
        if frequency is not None:
            frequencies.append(frequency)

    return max(frequencies) if frequencies else None


def _extract_dbsnp_frequencies(
    dbsnp: Any,
    alternate: str,
) -> dict[str, float]:
    """Extract exact-ALT population frequencies from dbSNP aggregation."""
    frequencies: dict[str, float] = {}

    for record in _iter_source_records(dbsnp):
        alleles = record.get("alleles")
        if not isinstance(alleles, list):
            continue

        for allele in alleles:
            if (
                not isinstance(allele, dict)
                or str(allele.get("allele", "")).upper() != alternate
                or not isinstance(allele.get("freq"), dict)
            ):
                continue

            for source_name, raw_frequency in allele["freq"].items():
                frequency = _valid_frequency(raw_frequency)
                if frequency is not None:
                    frequencies[f"dbsnp_{source_name}"] = frequency

    return frequencies


def _standardize_myvariant_response(
    annotation: AnnotationData,
    payload: dict[str, Any],
    variant_id: str,
) -> None:
    """Add bounded MyVariant evidence without retaining its raw payload."""
    dbsnp = payload.get("dbsnp")
    population_frequencies: dict[str, float] = {}

    for source_name in ("gnomad_exome", "gnomad_genome", "exac"):
        frequency = _extract_global_frequency(payload.get(source_name))
        if frequency is not None:
            population_frequencies[source_name] = frequency

    alternate = str(annotation["variant"]["alt"]).strip().upper()
    population_frequencies.update(
        _extract_dbsnp_frequencies(dbsnp, alternate)
    )

    max_frequency = (
        max(population_frequencies.values())
        if population_frequencies
        else None
    )
    gene = (
        _first_nested_string(dbsnp, "gene", "symbol")
        or _first_nested_string(payload.get("dbnsfp"), "genename")
        or _first_nested_string(
            payload.get("cadd"),
            "gene",
            "genename",
        )
    )
    upstream_sources: list[str] = []
    for payload_field, source_name in (
        ("dbsnp", "dbSNP"),
        ("dbnsfp", "dbNSFP"),
        ("cadd", "CADD"),
        ("gnomad_exome", "gnomAD"),
        ("gnomad_genome", "gnomAD"),
        ("exac", "ExAC"),
    ):
        if payload.get(payload_field) is not None:
            upstream_sources.append(source_name)

    annotation["population_frequency"] = max_frequency
    annotation["sources"]["myvariant"].update(
        {
            "status": "success",
            "upstream_sources": sorted(set(upstream_sources)),
            "variant_id": variant_id,
            "rsid": _first_nested_string(dbsnp, "rsid"),
            "gene": gene,
            "population_frequencies": population_frequencies,
            "max_population_frequency": max_frequency,
        }
    )
    annotation["references"].append(
        {
            "source": "MyVariant.info",
            "url": (
                f"{settings.MYVARIANT_BASE_URL}/variant/"
                f"{quote(variant_id, safe='')}"
                f"?assembly={MYVARIANT_ASSEMBLIES[settings.GENOME_ASSEMBLY]}"
            ),
        }
    )


def _annotate_with_myvariant(
    annotation: AnnotationData,
    session: requests.Session,
    max_retries: int,
) -> None:
    """Add isolated MyVariant evidence to one existing VEP annotation."""
    annotation["sources"]["myvariant"][
        "retrieved_at"
    ] = _retrieval_timestamp()
    try:
        payload, variant_id, unsupported_warning = _get_myvariant(
            session,
            annotation["variant"],
            max_retries,
        )
    except AnnotationServiceError as exc:
        LOGGER.error(
            "event=annotation_source_failed service=myvariant "
            "error_type=%s",
            type(exc).__name__,
        )
        annotation["sources"]["myvariant"]["status"] = "error"
        annotation["warnings"].append(str(exc))
        return

    if unsupported_warning is not None:
        annotation["sources"]["myvariant"]["status"] = "unsupported"
        annotation["warnings"].append(unsupported_warning)
        return

    if payload is None or variant_id is None:
        annotation["sources"]["myvariant"]["status"] = "not_found"
        annotation["warnings"].append(
            "MyVariant.info returned no exact result for this variant."
        )
        return

    _standardize_myvariant_response(
        annotation,
        payload,
        variant_id,
    )


def _bounded_string_list(
    value: Any,
    maximum: int,
) -> list[str]:
    """Keep a bounded, ordered list of unique non-empty strings."""
    return _bounded_string_list_with_count(value, maximum)[0]


def _bounded_string_list_with_count(
    value: Any,
    maximum: int,
) -> tuple[list[str], int]:
    """Return a bounded unique string list and its untruncated count."""
    if not isinstance(value, list):
        return [], 0

    cleaned: list[str] = []
    seen: set[str] = set()
    total = 0
    for item in value:
        if isinstance(item, str) and item.strip():
            normalized = item.strip()
            if normalized in seen:
                continue
            seen.add(normalized)
            total += 1
            if len(cleaned) < maximum:
                cleaned.append(normalized)

    return cleaned, total


def _clinvar_conflict_evidence(
    review_status: str | None,
    significance: str | None,
) -> dict[str, Any]:
    """Map only explicit aggregate ClinVar conflict language."""
    normalized_status = (
        review_status.casefold()
        if isinstance(review_status, str)
        else ""
    )

    if "no conflict" in normalized_status:
        status = "no_conflict"
        detected: bool | None = False
    elif "conflict" in normalized_status:
        status = "conflicting"
        detected = True
    else:
        status = "unknown"
        detected = None

    return {
        "status": status,
        "detected": detected,
        "basis": "aggregate_review_status",
        "details": significance if detected else None,
    }


def _clean_clinvar_conditions(
    classification: Any,
) -> tuple[list[dict[str, Any]], int]:
    """Standardize a bounded set of ClinVar germline conditions."""
    if not isinstance(classification, dict):
        return [], 0

    raw_traits = classification.get("trait_set")
    if not isinstance(raw_traits, list):
        return [], 0

    conditions: list[dict[str, Any]] = []
    total_conditions = 0

    for trait in raw_traits:
        if not isinstance(trait, dict):
            continue

        name = trait.get("trait_name")
        if not isinstance(name, str) or not name.strip():
            continue

        total_conditions += 1
        if len(conditions) >= MAX_CLINVAR_CONDITIONS:
            continue

        identifiers: list[dict[str, str]] = []
        raw_xrefs = trait.get("trait_xrefs")
        if isinstance(raw_xrefs, list):
            for xref in raw_xrefs:
                if not isinstance(xref, dict):
                    continue

                source = xref.get("db_source")
                identifier = xref.get("db_id")
                if (
                    isinstance(source, str)
                    and source.strip()
                    and isinstance(identifier, str)
                    and identifier.strip()
                ):
                    identifiers.append(
                        {
                            "source": source.strip(),
                            "id": identifier.strip(),
                        }
                    )

        conditions.append(
            {
                "name": name.strip(),
                "identifiers": identifiers,
            }
        )

    return conditions, total_conditions


def _standardize_clinvar_response(
    annotation: AnnotationData,
    summary: dict[str, Any],
    hgvs: str,
) -> None:
    """Add bounded direct ClinVar evidence without retaining raw JSON."""
    classification = summary.get("germline_classification")
    if not isinstance(classification, dict):
        classification = {}

    conditions, condition_count = _clean_clinvar_conditions(
        classification
    )
    supporting = summary.get("supporting_submissions")
    if not isinstance(supporting, dict):
        supporting = {}

    gene = summary.get("gene_sort")
    if not isinstance(gene, str) or not gene.strip():
        gene = _first_nested_string(summary.get("genes"), "symbol")

    significance = classification.get("description")
    if not isinstance(significance, str) or not significance.strip():
        significance = None

    review_status = classification.get("review_status")
    if not isinstance(review_status, str) or not review_status.strip():
        review_status = None

    last_evaluated = classification.get("last_evaluated")
    if not isinstance(last_evaluated, str) or not last_evaluated.strip():
        last_evaluated = None

    variation_id = str(summary.get("uid", "")).strip() or None
    accession = summary.get("accession")
    if not isinstance(accession, str) or not accession.strip():
        accession = None

    accession_version = summary.get("accession_version")
    if (
        not isinstance(accession_version, str)
        or not accession_version.strip()
    ):
        accession_version = None

    scv_accessions, scv_accession_count = (
        _bounded_string_list_with_count(
            supporting.get("scv"),
            MAX_CLINVAR_ACCESSIONS,
        )
    )
    rcv_accessions, rcv_accession_count = (
        _bounded_string_list_with_count(
            supporting.get("rcv"),
            MAX_CLINVAR_ACCESSIONS,
        )
    )

    annotation["sources"]["clinvar"].update(
        {
            "status": "success",
            "direct_verification_status": "verified",
            "query_hgvs": hgvs,
            "variation_id": variation_id,
            "accession": accession,
            "accession_version": accession_version,
            "gene": gene.strip() if isinstance(gene, str) else None,
            "clinical_significance": significance,
            "review_status": review_status,
            "last_evaluated": last_evaluated,
            "conditions": conditions,
            "condition_count": condition_count,
            "conditions_truncated": condition_count > len(conditions),
            "scv_accessions": scv_accessions,
            "scv_accession_count": scv_accession_count,
            "scv_accessions_truncated": (
                scv_accession_count > len(scv_accessions)
            ),
            "rcv_accessions": rcv_accessions,
            "rcv_accession_count": rcv_accession_count,
            "rcv_accessions_truncated": (
                rcv_accession_count > len(rcv_accessions)
            ),
            "conflicting_submissions": _clinvar_conflict_evidence(
                review_status,
                significance,
            ),
        }
    )

    if variation_id is not None:
        annotation["references"].append(
            {
                "source": "NCBI ClinVar",
                "url": (
                    "https://www.ncbi.nlm.nih.gov/clinvar/variation/"
                    f"{variation_id}/"
                ),
            }
        )


def _annotate_with_clinvar(
    annotation: AnnotationData,
    session: requests.Session,
    max_retries: int,
) -> None:
    """Add isolated direct ClinVar evidence to one annotation."""
    source = annotation["sources"]["clinvar"]
    source["retrieved_at"] = _retrieval_timestamp()
    identifiers = _to_clinvar_identifiers(annotation["variant"])
    if identifiers is not None:
        source["query_hgvs"] = identifiers[0]

    try:
        summary, _, hgvs, unsupported_warning = _get_clinvar(
            session,
            annotation["variant"],
            max_retries,
        )
    except AnnotationServiceError as exc:
        LOGGER.error(
            "event=annotation_source_failed service=ncbi_clinvar "
            "error_type=%s",
            type(exc).__name__,
        )
        source_status = (
            "invalid_response"
            if isinstance(exc, ClinVarResponseError)
            else "unavailable"
        )
        source["status"] = source_status
        source["direct_verification_status"] = source_status
        annotation["warnings"].append(str(exc))
        return

    if unsupported_warning is not None:
        source["status"] = "unsupported"
        source["direct_verification_status"] = "unsupported"
        annotation["warnings"].append(unsupported_warning)
        return

    if summary is None or hgvs is None:
        source["status"] = "not_found"
        source["direct_verification_status"] = "no_record"
        annotation["warnings"].append(
            "NCBI ClinVar returned no exact result for this variant."
        )
        return

    _standardize_clinvar_response(
        annotation,
        summary,
        hgvs,
    )


def _optional_clingen_string(value: Any) -> str | None:
    """Return a stripped ClinGen scalar or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _standardize_clingen_response(
    annotation: AnnotationData,
    records: list[dict[str, Any]],
    query_gene: str,
) -> None:
    """Add bounded ClinGen-submitted GenCC validity evidence."""
    curations: list[dict[str, Any]] = []
    reference_urls: list[str] = []

    for record in records[:MAX_CLINGEN_CURATIONS]:
        gene = _optional_clingen_string(record.get("gene_symbol"))
        submitter = _optional_clingen_string(record.get("submitter_title"))
        classification = _optional_clingen_string(
            record.get("classification_title")
        )
        disease = _optional_clingen_string(record.get("disease_title"))
        if (
            gene is None
            or gene.casefold() != query_gene.casefold()
            or submitter is None
            or submitter.casefold() != "clingen"
            or classification is None
            or disease is None
        ):
            raise AnnotationServiceError(
                "UCSC GenCC returned an incomplete validity claim."
            )

        curation_id = (
            _optional_clingen_string(record.get("sgc_id"))
            or _optional_clingen_string(record.get("sub_submission_id"))
        )
        report_url = _optional_clingen_string(
            record.get("sub_public_report_url")
        )
        raw_pmids = _optional_clingen_string(record.get("sub_pmids"))
        pmids = list(
            dict.fromkeys(
                value.strip()
                for value in (raw_pmids or "").split(",")
                if value.strip().isdigit()
            )
        )[:MAX_CLINGEN_PMIDS]
        curations.append(
            {
                "curation_id": curation_id,
                "disease": disease,
                "disease_id": _optional_clingen_string(
                    record.get("disease_curie")
                ),
                "classification": classification,
                "classification_id": _optional_clingen_string(
                    record.get("classification_curie")
                ),
                "mode_of_inheritance": _optional_clingen_string(
                    record.get("moi_title")
                ),
                "mode_of_inheritance_id": _optional_clingen_string(
                    record.get("moi_curie")
                ),
                "classification_date": _optional_clingen_string(
                    record.get("sub_date")
                ),
                "submitter": submitter,
                "criteria_url": _optional_clingen_string(
                    record.get("sub_assertion_criteria_url")
                ),
                "submission_id": _optional_clingen_string(
                    record.get("sub_submission_id")
                ),
                "pmids": pmids,
                "report_url": report_url,
            }
        )
        if report_url is not None:
            reference_urls.append(report_url)

    clingen = annotation["sources"]["clingen"]
    clingen.update(
        {
            "status": "success",
            "query_gene": query_gene,
            "gene": _optional_clingen_string(records[0].get("gene_symbol")),
            "gene_id": _optional_clingen_string(
                records[0].get("gene_curie")
            ),
            "curations": curations,
            "curation_count": len(records),
            "curations_truncated": len(records) > len(curations),
        }
    )

    for report_url in reference_urls:
        annotation["references"].append(
            {
                "source": (
                    "ClinGen Gene-Disease Validity via UCSC GenCC"
                ),
                "url": report_url,
            }
        )


def _annotate_with_clingen(
    annotation: AnnotationData,
    session: requests.Session,
    max_retries: int,
) -> None:
    """Add isolated ClinGen gene-disease validity evidence."""
    source = annotation["sources"]["clingen"]
    source["retrieved_at"] = _retrieval_timestamp()
    region = _to_clingen_ucsc_region(annotation["variant"])
    if region is not None:
        chromosome, start, end = region
        source["query_region"] = {
            "assembly": CLINGEN_UCSC_ASSEMBLIES[
                settings.GENOME_ASSEMBLY
            ],
            "chromosome": chromosome,
            "start": start,
            "end": end,
            "coordinate_system": "0-based half-open",
        }

    gene = _select_clingen_gene(annotation)
    if gene is None:
        source["status"] = "not_applicable"
        annotation["warnings"].append(
            "ClinGen was not queried because no unambiguous gene symbol "
            "was available."
        )
        return

    source["query_gene"] = gene

    try:
        records, unsupported_warning = _get_clingen_gene_validity(
            session,
            annotation["variant"],
            gene,
            max_retries,
        )
        if unsupported_warning is not None:
            source["status"] = "unsupported"
            annotation["warnings"].append(unsupported_warning)
            return

        if records is None or not records:
            source["status"] = "not_found"
            annotation["warnings"].append(
                "UCSC GenCC returned no exact ClinGen validity claims "
                f"for {gene} at this locus."
            )
            return

        _standardize_clingen_response(
            annotation,
            records,
            gene,
        )
    except AnnotationServiceError as exc:
        LOGGER.error(
            "event=annotation_source_failed service=ucsc_gencc "
            "error_type=%s",
            type(exc).__name__,
        )
        source["status"] = "error"
        annotation["warnings"].append(str(exc))


def _collect_cspec_disease_ids(
    annotation: AnnotationData,
) -> tuple[list[str], bool]:
    """Collect bounded MONDO context from direct clinical sources."""
    disease_ids: list[str] = []

    clingen_curations = (
        annotation.get("sources", {})
        .get("clingen", {})
        .get("curations", [])
    )
    if isinstance(clingen_curations, list):
        for curation in clingen_curations:
            if not isinstance(curation, dict):
                continue
            disease_id = curation.get("disease_id")
            if (
                isinstance(disease_id, str)
                and MONDO_IDENTIFIER_PATTERN.fullmatch(disease_id.strip())
                and disease_id.strip() not in disease_ids
            ):
                disease_ids.append(disease_id.strip())

    clinvar_conditions = (
        annotation.get("sources", {})
        .get("clinvar", {})
        .get("conditions", [])
    )
    if isinstance(clinvar_conditions, list):
        for condition in clinvar_conditions:
            if not isinstance(condition, dict):
                continue
            identifiers = condition.get("identifiers")
            if not isinstance(identifiers, list):
                continue
            for identifier in identifiers:
                if not isinstance(identifier, dict):
                    continue
                source = identifier.get("source")
                value = identifier.get("id")
                if (
                    isinstance(source, str)
                    and source.strip().casefold() == "mondo"
                    and isinstance(value, str)
                    and MONDO_IDENTIFIER_PATTERN.fullmatch(value.strip())
                    and value.strip() not in disease_ids
                ):
                    disease_ids.append(value.strip())

    return (
        disease_ids[:MAX_CSPEC_DISEASE_QUERIES],
        len(disease_ids) > MAX_CSPEC_DISEASE_QUERIES,
    )


def _current_cspec_state(content: dict[str, Any]) -> str | None:
    """Return the explicitly current CSpec workflow state."""
    states = content.get("states")
    if not isinstance(states, list):
        return None

    for state in states:
        if (
            isinstance(state, dict)
            and state.get("current") is True
        ):
            return _optional_clingen_string(state.get("name"))
    return None


def _cspec_vcep_name(content: dict[str, Any]) -> str | None:
    """Extract the VCEP research-group name from DOI metadata."""
    doi = content.get("doi")
    if not isinstance(doi, dict):
        return None
    authors = doi.get("authors")
    if not isinstance(authors, list):
        return None

    for author in authors:
        if not isinstance(author, dict):
            continue
        role = author.get("role")
        person_or_org = author.get("person_or_org")
        if (
            isinstance(role, dict)
            and role.get("id") == "researchgroup"
            and isinstance(person_or_org, dict)
        ):
            return _optional_clingen_string(
                person_or_org.get("name")
            )
    return None


def _cspec_doi(
    content: dict[str, Any],
    field: str,
) -> str | None:
    """Read one DOI identifier without retaining full DOI metadata."""
    doi = content.get("doi")
    if not isinstance(doi, dict):
        return None
    return _optional_clingen_string(doi.get(field))


def _cspec_source_url(value: Any) -> str | None:
    """Keep only an HTTPS CSpec source-document URL."""
    url = _optional_clingen_string(value)
    if url is None:
        return None
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not (
            hostname == "clinicalgenome.org"
            or hostname.endswith(".clinicalgenome.org")
            or hostname == "genome.network"
            or hostname.endswith(".genome.network")
        )
    ):
        return None
    return url


def _linked_cspec_records(
    entity: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate linked CSpec specification summaries."""
    linked = entity.get("ldFor")
    if linked is None:
        return []
    if not isinstance(linked, dict):
        raise CSpecResponseError(
            "ClinGen CSpec returned invalid linked specification data."
        )

    records = linked.get("SequenceVariantInterpretation", [])
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        for record in records
    ):
        raise CSpecResponseError(
            "ClinGen CSpec returned invalid linked specification data."
        )
    return records


def _linked_cspec_ids(
    disease_entity: dict[str, Any],
) -> set[str]:
    """Return exact CSpec identifiers linked to one MONDO disease."""
    identifiers: set[str] = set()
    for record in _linked_cspec_records(disease_entity):
        identifier = record.get("entId")
        if (
            isinstance(identifier, str)
            and CSPEC_IDENTIFIER_PATTERN.fullmatch(identifier)
        ):
            identifiers.add(identifier)
    return identifiers


def _standardize_cspec_record(
    record: dict[str, Any],
    *,
    disease_matches: list[str],
    has_disease_context: bool,
) -> dict[str, Any] | None:
    """Reduce one released specification to auditable context metadata."""
    identifier = record.get("entId")
    content = record.get("entContent")
    if (
        not isinstance(identifier, str)
        or not CSPEC_IDENTIFIER_PATTERN.fullmatch(identifier)
        or record.get("entType") != "SequenceVariantInterpretation"
        or not isinstance(content, dict)
    ):
        raise CSpecResponseError(
            "ClinGen CSpec returned an invalid specification summary."
        )

    current_state = _current_cspec_state(content)
    if current_state != "Released":
        return None

    title = _optional_clingen_string(content.get("title"))
    version = _optional_clingen_string(content.get("version"))
    if title is None or version is None:
        raise CSpecResponseError(
            "ClinGen CSpec returned incomplete released specification "
            "metadata."
        )

    specification_url = (
        f"{settings.CSPEC_BASE_URL}/"
        f"SequenceVariantInterpretation/id/{identifier}"
    )
    return {
        "specification_id": identifier,
        "title": title,
        "short_title": _optional_clingen_string(
            content.get("shortTitle")
        ),
        "version": version,
        "status": current_state,
        "vcep": _cspec_vcep_name(content),
        "approved_at": _optional_clingen_string(
            content.get("approvedOn")
        ),
        "modified_at": _optional_clingen_string(record.get("modified")),
        "source_document_url": _cspec_source_url(
            content.get("specificationSource")
        ),
        "specification_url": specification_url,
        "concept_doi": _cspec_doi(content, "conceptDoi"),
        "document_doi": _cspec_doi(content, "docDoi"),
        "matched_disease_ids": disease_matches,
        "scope_match": (
            "gene_and_disease" if disease_matches else "gene_only"
        ),
        "applicable_to_disease_context": (
            bool(disease_matches)
            if has_disease_context
            else None
        ),
    }


def _annotate_with_cspec(
    annotation: AnnotationData,
    session: requests.Session,
    max_retries: int,
) -> None:
    """Add CSpec availability metadata without applying ACMG rules."""
    source = annotation["sources"]["cspec"]
    source["retrieved_at"] = _retrieval_timestamp()
    gene = _select_clingen_gene(annotation)
    if gene is None:
        source["status"] = "not_applicable"
        annotation["warnings"].append(
            "ClinGen CSpec was not queried because no unambiguous gene "
            "symbol was available."
        )
        return

    disease_ids, disease_queries_truncated = _collect_cspec_disease_ids(
        annotation
    )
    source["query_gene"] = gene
    source["query_disease_ids"] = disease_ids
    source["disease_queries_truncated"] = disease_queries_truncated

    try:
        gene_entity = _get_cspec_entity(
            session,
            "Gene",
            gene,
            max_retries,
        )
        if gene_entity is None:
            source["status"] = "not_found"
            return

        disease_links: dict[str, set[str]] = {}
        for disease_id in disease_ids:
            disease_entity = _get_cspec_entity(
                session,
                "Disease",
                disease_id,
                max_retries,
            )
            disease_links[disease_id] = (
                _linked_cspec_ids(disease_entity)
                if disease_entity is not None
                else set()
            )

        raw_records = _linked_cspec_records(gene_entity)
        specifications: list[dict[str, Any]] = []
        for record in raw_records:
            identifier = record.get("entId")
            disease_matches = [
                disease_id
                for disease_id, linked_ids in disease_links.items()
                if isinstance(identifier, str)
                and identifier in linked_ids
            ]
            standardized = _standardize_cspec_record(
                record,
                disease_matches=disease_matches,
                has_disease_context=bool(disease_ids),
            )
            if standardized is not None:
                specifications.append(standardized)

        if not specifications:
            source["status"] = "not_found"
            return

        retained = specifications[:MAX_CSPEC_SPECIFICATIONS]
        source.update(
            {
                "status": "success",
                "specification_available": True,
                "specifications": retained,
                "specification_count": len(specifications),
                "specifications_truncated": (
                    len(specifications) > len(retained)
                ),
            }
        )

        for specification in retained:
            annotation["references"].append(
                {
                    "source": "ClinGen CSpec Registry",
                    "url": specification["specification_url"],
                }
            )

    except AnnotationServiceError as exc:
        LOGGER.error(
            "event=annotation_source_failed service=clingen_cspec "
            "error_type=%s",
            type(exc).__name__,
        )
        source_status = (
            "invalid_response"
            if isinstance(exc, CSpecResponseError)
            else "unavailable"
        )
        source["status"] = source_status
        annotation["warnings"].append(str(exc))


# ---------------------------------------------------------------------------
# Public annotation entry point
# ---------------------------------------------------------------------------

def _notify_annotation_progress(
    callback: AnnotationProgressCallback | None,
    source: str,
    status: AnnotationProgressStatus,
    message: str,
) -> None:
    """Publish one source-level progress update without blocking annotation."""

    if callback is None:
        return
    try:
        callback(source, status, message)
    except Exception:
        return


def _source_progress_summary(
    annotations: list[AnnotationData],
    source: str,
    *,
    retry_rounds: int = 0,
) -> tuple[AnnotationProgressStatus, str]:
    """Summarize provider availability separately from evidence presence."""

    total = len(annotations)
    statuses = [
        str(annotation["sources"][source]["status"])
        for annotation in annotations
    ]
    failed = sum(
        status in FAILED_SOURCE_STATUSES
        for status in statuses
    )
    unsupported = statuses.count("unsupported")
    not_found = statuses.count("not_found")

    retry_suffix = (
        f" after {retry_rounds} automatic "
        f"{'retry' if retry_rounds == 1 else 'retries'}"
        if retry_rounds
        else ""
    )
    if failed == total and total:
        return (
            "error",
            f"Failed for all {total} variants{retry_suffix}.",
        )

    details: list[str] = []
    if failed:
        failed_variants = [
            _variant_progress_label(annotation["variant"])
            for annotation, status in zip(
                annotations,
                statuses,
                strict=True,
            )
            if status in FAILED_SOURCE_STATUSES
        ]
        details.append(
            f"{failed} failed"
            + (
                f" ({', '.join(failed_variants)})"
                if failed_variants
                else ""
            )
        )
    if unsupported:
        details.append(f"{unsupported} unsupported")
    if not_found:
        details.append(f"{not_found} with no exact record")

    if details:
        return (
            "warning" if failed or unsupported else "success",
            f"Completed {total} variants{retry_suffix}; "
            f"{', '.join(details)}.",
        )
    return (
        "success",
        f"Completed all {total} variants{retry_suffix}.",
    )


def _variant_progress_label(variant: dict[str, Any]) -> str:
    """Return one compact non-identifying genomic variant label."""

    return (
        f"{variant.get('chrom')}:{variant.get('pos')} "
        f"{variant.get('ref')}>{variant.get('alt')}"
    )


def _source_failed(
    annotation: AnnotationData,
    source: str,
) -> bool:
    """Return whether one provider attempt ended in an actual failure."""

    return (
        annotation["sources"][source]["status"]
        in FAILED_SOURCE_STATUSES
    )


def _retry_failed_source_annotations(
    annotations: list[AnnotationData],
    *,
    source: str,
    max_retries: int,
    retry_variant: Callable[[int], AnnotationData],
    progress_callback: AnnotationProgressCallback | None,
) -> int:
    """Automatically retry only failed variant/provider pairs."""

    failed_indices = [
        index
        for index, annotation in enumerate(annotations)
        if _source_failed(annotation, source)
    ]
    retry_rounds = 0

    for retry_index in range(max_retries):
        if not failed_indices:
            break
        retry_rounds = retry_index + 1
        failed_count = len(failed_indices)
        label = ANNOTATION_SOURCE_LABELS[source]
        _notify_annotation_progress(
            progress_callback,
            source,
            "running",
            (
                f"{label} is automatically retrying {failed_count} "
                f"failed {'variant' if failed_count == 1 else 'variants'} "
                f"(retry {retry_rounds} of {max_retries})."
            ),
        )
        delay = _retry_delay(retry_index)
        _log_api_retry(
            service=ANNOTATION_SERVICE_LOG_NAMES[source],
            operation="retry_failed_variants",
            attempt=retry_index,
            reason="variant_failure",
            delay_seconds=delay,
        )
        time.sleep(delay)

        for index in failed_indices:
            annotations[index] = retry_variant(index)

        failed_indices = [
            index
            for index in failed_indices
            if _source_failed(annotations[index], source)
        ]

    return retry_rounds


def _annotate_vep_variant_once(
    variant: VariantData,
    session: requests.Session,
) -> AnnotationData:
    """Retry one failed VEP variant without restarting successful variants."""

    token = "cv_retry_0"
    try:
        responses = _post_vep_batch(
            session,
            [_to_vep_input(token, variant)],
            0,
        )
    except AnnotationServiceError as exc:
        LOGGER.error(
            "event=annotation_source_failed "
            "service=ensembl_vep error_type=%s",
            type(exc).__name__,
        )
        return _base_annotation(
            variant,
            status="error",
            warning=str(exc),
        )

    response = next(
        (
            candidate
            for candidate in responses
            if _extract_response_token(candidate) == token
        ),
        None,
    )
    if response is None:
        return _base_annotation(
            variant,
            status="not_found",
            warning="Ensembl VEP returned no result for this variant.",
        )
    return _standardize_vep_response(variant, response)


def _retry_annotation_copy(
    baseline: AnnotationData,
    annotate: Callable[[AnnotationData], None],
) -> AnnotationData:
    """Run one isolated retry without retaining warnings from older attempts."""

    candidate = deepcopy(baseline)
    annotate(candidate)
    return candidate


def _annotation_cache_key(
    variants: list[VariantData],
) -> tuple[object, ...]:
    """Key normalized alleles with assembly and provider API context."""

    provider_context = (
        settings.VEP_BASE_URL,
        settings.GENEBE_BASE_URL,
        settings.MYVARIANT_BASE_URL,
        MYVARIANT_API_VERSION,
        settings.CLINVAR_BASE_URL,
        settings.CLINGEN_BASE_URL,
        settings.CSPEC_BASE_URL,
    )
    normalized = tuple(
        (
            str(variant["chrom"]).removeprefix("chr").upper(),
            variant["pos"],
            variant["ref"].strip().upper(),
            variant["alt"].strip().upper(),
        )
        for variant in variants
    )
    return (settings.GENOME_ASSEMBLY, provider_context, normalized)


def _annotation_cache_get(
    key: tuple[object, ...],
    variants: list[VariantData],
) -> list[AnnotationData] | None:
    """Return an isolated unexpired entry without retaining caller fields."""

    now = time.monotonic()
    with _ANNOTATION_CACHE_LOCK:
        entry = _ANNOTATION_CACHE.get(key)
        if entry is None:
            return None
        cached_at, cached_annotations = entry
        if now - cached_at > settings.ANNOTATION_CACHE_TTL_SECONDS:
            _ANNOTATION_CACHE.pop(key, None)
            return None
        _ANNOTATION_CACHE.move_to_end(key)
        result = deepcopy(cached_annotations)
    for annotation, variant in zip(result, variants, strict=True):
        annotation["variant"] = dict(variant)
    return result


def _annotation_cache_put(
    key: tuple[object, ...],
    annotations: list[AnnotationData],
) -> None:
    """Cache only complete non-failure provider results in bounded memory."""

    if any(
        source.get("status") not in {"success", "not_found"}
        for annotation in annotations
        for source in annotation.get("sources", {}).values()
        if isinstance(source, dict)
    ):
        return
    with _ANNOTATION_CACHE_LOCK:
        _ANNOTATION_CACHE[key] = (time.monotonic(), deepcopy(annotations))
        _ANNOTATION_CACHE.move_to_end(key)
        while len(_ANNOTATION_CACHE) > settings.ANNOTATION_CACHE_SIZE:
            _ANNOTATION_CACHE.popitem(last=False)


def clear_annotation_cache() -> None:
    """Clear the bounded process-local annotation cache."""

    with _ANNOTATION_CACHE_LOCK:
        _ANNOTATION_CACHE.clear()


def annotate_variants(
    variants: Iterable[VariantData],
    *,
    batch_size: int | None = None,
    max_retries: int | None = None,
    session: requests.Session | None = None,
    progress_callback: AnnotationProgressCallback | None = None,
    use_cache: bool | None = None,
) -> list[AnnotationData]:
    """Annotate with VEP, GeneBe, MyVariant, ClinVar, ClinGen, and CSpec evidence.

    Each external source fails independently and raw source payloads are not
    retained in the returned standardized evidence.
    """
    if isinstance(variants, (str, bytes, dict)):
        raise AnnotationError("variants must be an iterable of dictionaries.")
    variant_items = list(variants)
    for index, variant in enumerate(variant_items):
        _validate_variant(variant, index)
    resolved_use_cache = session is None if use_cache is None else use_cache
    if not isinstance(resolved_use_cache, bool):
        raise AnnotationError("use_cache must be a boolean.")

    resolved_batch_size = _resolve_batch_size(batch_size)
    resolved_retries = _resolve_retries(max_retries)
    cache_key = _annotation_cache_key(variant_items)
    if resolved_use_cache:
        cached = _annotation_cache_get(cache_key, variant_items)
        if cached is not None:
            for source in ANNOTATION_SOURCE_LABELS:
                _notify_annotation_progress(
                    progress_callback,
                    source,
                    "success",
                    f"{ANNOTATION_SOURCE_LABELS[source]} loaded from cache.",
                )
            return cached
    active_session = session or requests.Session()
    owns_session = session is None
    annotations: list[AnnotationData] = []

    try:
        _notify_annotation_progress(
            progress_callback,
            "vep",
            "running",
            "Sending variants to Ensembl VEP.",
        )
        for batch in _iter_batches(variant_items, resolved_batch_size):
            vep_inputs = [
                _to_vep_input(token, variant)
                for token, variant in batch
            ]

            try:
                responses = _post_vep_batch(
                    active_session,
                    vep_inputs,
                    resolved_retries,
                )
            except AnnotationServiceError as exc:
                LOGGER.error(
                    "event=annotation_source_failed "
                    "service=ensembl_vep error_type=%s",
                    type(exc).__name__,
                )
                annotations.extend(
                    _base_annotation(
                        variant,
                        status="error",
                        warning=str(exc),
                    )
                    for _, variant in batch
                )
                continue

            responses_by_token = {
                token: response
                for response in responses
                if (token := _extract_response_token(response))
            }

            for token, variant in batch:
                response = responses_by_token.get(token)

                if response is None:
                    annotations.append(
                        _base_annotation(
                            variant,
                            status="not_found",
                            warning=(
                                "Ensembl VEP returned no result for "
                                "this variant."
                            ),
                        )
                    )
                    continue

                annotations.append(
                    _standardize_vep_response(
                        variant,
                        response,
                    )
                )

        vep_retry_rounds = _retry_failed_source_annotations(
            annotations,
            source="vep",
            max_retries=resolved_retries,
            retry_variant=lambda index: _annotate_vep_variant_once(
                annotations[index]["variant"],
                active_session,
            ),
            progress_callback=progress_callback,
        )
        vep_status, vep_message = _source_progress_summary(
            annotations,
            "vep",
            retry_rounds=vep_retry_rounds,
        )
        _notify_annotation_progress(
            progress_callback,
            "vep",
            vep_status,
            vep_message,
        )
        _notify_annotation_progress(
            progress_callback,
            "genebe",
            "running",
            "Sending variants to GeneBe.",
        )
        genebe_baselines = deepcopy(annotations)
        _annotate_with_genebe(
            annotations,
            active_session,
            resolved_retries,
        )
        genebe_retry_rounds = _retry_failed_source_annotations(
            annotations,
            source="genebe",
            max_retries=resolved_retries,
            retry_variant=lambda index: _retry_annotation_copy(
                genebe_baselines[index],
                lambda candidate: _annotate_with_genebe(
                    [candidate],
                    active_session,
                    0,
                ),
            ),
            progress_callback=progress_callback,
        )
        genebe_status, genebe_message = _source_progress_summary(
            annotations,
            "genebe",
            retry_rounds=genebe_retry_rounds,
        )
        _notify_annotation_progress(
            progress_callback,
            "genebe",
            genebe_status,
            genebe_message,
        )

        _notify_annotation_progress(
            progress_callback,
            "myvariant",
            "running",
            "Querying MyVariant.info.",
        )
        myvariant_baselines = deepcopy(annotations)
        for annotation in annotations:
            _annotate_with_myvariant(
                annotation,
                active_session,
                resolved_retries,
            )
        myvariant_retry_rounds = _retry_failed_source_annotations(
            annotations,
            source="myvariant",
            max_retries=resolved_retries,
            retry_variant=lambda index: _retry_annotation_copy(
                myvariant_baselines[index],
                lambda candidate: _annotate_with_myvariant(
                    candidate,
                    active_session,
                    0,
                ),
            ),
            progress_callback=progress_callback,
        )
        myvariant_status, myvariant_message = _source_progress_summary(
            annotations,
            "myvariant",
            retry_rounds=myvariant_retry_rounds,
        )
        _notify_annotation_progress(
            progress_callback,
            "myvariant",
            myvariant_status,
            myvariant_message,
        )

        _notify_annotation_progress(
            progress_callback,
            "clinvar",
            "running",
            "Querying NCBI ClinVar.",
        )
        clinvar_baselines = deepcopy(annotations)
        for annotation in annotations:
            _annotate_with_clinvar(
                annotation,
                active_session,
                resolved_retries,
            )
        clinvar_retry_rounds = _retry_failed_source_annotations(
            annotations,
            source="clinvar",
            max_retries=resolved_retries,
            retry_variant=lambda index: _retry_annotation_copy(
                clinvar_baselines[index],
                lambda candidate: _annotate_with_clinvar(
                    candidate,
                    active_session,
                    0,
                ),
            ),
            progress_callback=progress_callback,
        )
        clinvar_status, clinvar_message = _source_progress_summary(
            annotations,
            "clinvar",
            retry_rounds=clinvar_retry_rounds,
        )
        _notify_annotation_progress(
            progress_callback,
            "clinvar",
            clinvar_status,
            clinvar_message,
        )

        _notify_annotation_progress(
            progress_callback,
            "clingen",
            "running",
            "Querying ClinGen/GenCC through UCSC.",
        )
        clingen_baselines = deepcopy(annotations)
        for annotation in annotations:
            _annotate_with_clingen(
                annotation,
                active_session,
                resolved_retries,
            )
        clingen_retry_rounds = _retry_failed_source_annotations(
            annotations,
            source="clingen",
            max_retries=resolved_retries,
            retry_variant=lambda index: _retry_annotation_copy(
                clingen_baselines[index],
                lambda candidate: _annotate_with_clingen(
                    candidate,
                    active_session,
                    0,
                ),
            ),
            progress_callback=progress_callback,
        )
        clingen_status, clingen_message = _source_progress_summary(
            annotations,
            "clingen",
            retry_rounds=clingen_retry_rounds,
        )
        _notify_annotation_progress(
            progress_callback,
            "clingen",
            clingen_status,
            clingen_message,
        )

        _notify_annotation_progress(
            progress_callback,
            "cspec",
            "running",
            "Checking ClinGen CSpec guideline availability.",
        )
        cspec_baselines = deepcopy(annotations)
        for annotation in annotations:
            _annotate_with_cspec(
                annotation,
                active_session,
                resolved_retries,
            )
        cspec_retry_rounds = _retry_failed_source_annotations(
            annotations,
            source="cspec",
            max_retries=resolved_retries,
            retry_variant=lambda index: _retry_annotation_copy(
                cspec_baselines[index],
                lambda candidate: _annotate_with_cspec(
                    candidate,
                    active_session,
                    0,
                ),
            ),
            progress_callback=progress_callback,
        )
        cspec_status, cspec_message = _source_progress_summary(
            annotations,
            "cspec",
            retry_rounds=cspec_retry_rounds,
        )
        _notify_annotation_progress(
            progress_callback,
            "cspec",
            cspec_status,
            cspec_message,
        )
    finally:
        if owns_session:
            active_session.close()

    if resolved_use_cache:
        _annotation_cache_put(cache_key, annotations)
    return annotations
