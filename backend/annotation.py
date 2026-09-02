"""External variant annotation and evidence standardization."""

import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import requests

from backend.cspec_cache import (
    CSpecCacheError,
    load_cspec_lkg,
    store_cspec_lkg,
)
from backend.conditional_enrichment import fetch_ucsc_gnomad_evidence
from backend.evidence_rescue import (
    EvidenceRescueAttempt,
    EvidenceRescueIdentifier,
    build_evidence_rescue_trace,
)
from backend.erepo import retrieve_expert_curated_context
from backend.gene_identity import (
    GENE_IDENTITY_UNRESOLVED,
    VEP_GENE_CONTEXT_UNRESOLVED,
    resolve_gene_identity,
    vep_gene_context_is_complete,
)
from backend.logging_config import get_logger
from backend.provider_readiness import (
    ProviderReadinessSnapshot,
    resolve_provider_readiness_hint,
)
from backend.provider_resilience import (
    ProviderCircuitState,
    ProviderInvalidResponseError,
    ProviderRetryPolicy,
    ProviderStatus,
    ProviderTimeouts,
    build_provider_provenance,
    call_provider_with_policy,
    classify_http_status,
    classify_request_exception,
    should_trigger_fallback,
)
from backend.retrieval_intelligence import (
    IdentifierReference,
    VariantIdentifierBundle,
    build_retrieval_assessment,
    build_variant_identifier_bundle,
)
from backend.variant_identity import (
    format_genomic_hgvs as _shared_format_genomic_hgvs,
    normalize_chromosome as _shared_normalize_chromosome,
    normalize_variant_edit as _shared_normalize_variant_edit,
)
from backend.vcf_processing import VariantData
from config import settings


LOGGER = get_logger("annotation")

# Ensembl documents a maximum of 200 variants per POST request.
MAX_VEP_BATCH_SIZE = 200
MAX_GENEBE_BATCH_SIZE = 1_000
FAST_CONFIRMATION_CONNECT_TIMEOUT_SECONDS = 1.0
FAST_CONFIRMATION_READ_TIMEOUT_SECONDS = 3.0
MAX_STORED_TRANSCRIPTS = 10
MAX_GENEBE_CONSEQUENCES = 10
MAX_GENEBE_ACMG_CRITERIA = 50
MAX_CLINVAR_SEARCH_RESULTS = 20
MAX_CLINVAR_TOTAL_CANDIDATES = 100
MAX_CLINVAR_QUERY_STRATEGIES = 20
MAX_CLINVAR_CANDIDATE_REJECTIONS = 50
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
VARIANTVALIDATOR_PROVIDER_NAME = "VariantValidator"
VARIANTVALIDATOR_SELECT_TRANSCRIPTS = "mane"
GENEBE_PROVIDER_NAME = "GeneBe"
MYVARIANT_PROVIDER_NAME = "MyVariant.info"
UCSC_GNOMAD_PROVIDER_NAME = "UCSC gnomAD"
MYVARIANT_API_VERSION = "v1"
ENSEMBL_VARIATION_PROVIDER_NAME = "Ensembl REST Variation"
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
        "clinvar.allele_id",
        "clinvar.gene.symbol",
        "clinvar.rcv.accession",
        "clinvar.rcv.clinical_significance",
        "clinvar.rcv.conditions",
        "clinvar.rcv.last_evaluated",
        "clinvar.rcv.origin",
        "clinvar.rcv.review_status",
        "clinvar.variant_id",
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
    "erepo": "clingen_erepo",
}
ANNOTATION_SOURCE_LABELS = {
    "vep": "Ensembl VEP",
    "genebe": "GeneBe",
    "myvariant": "MyVariant.info",
    "clinvar": "NCBI ClinVar",
    "clingen": "ClinGen/GenCC",
    "cspec": "ClinGen CSpec Registry",
    "erepo": "ClinGen ERepo",
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


class GeneBeOperationalError(AnnotationServiceError):
    """Carry one normalized provider-wide GeneBe operational failure."""

    def __init__(
        self,
        message: str,
        provider_status: ProviderStatus,
    ) -> None:
        super().__init__(message)
        self.provider_status = provider_status


class GeneBeVariantLevelError(AnnotationServiceError):
    """Carry one GeneBe rejection requiring per-variant isolation."""


class VepOperationalError(AnnotationServiceError):
    """Carry one normalized VEP operational failure."""

    def __init__(
        self,
        message: str,
        provider_status: ProviderStatus,
    ) -> None:
        super().__init__(message)
        self.provider_status = provider_status


class VepVariantLevelError(AnnotationServiceError):
    """Carry one VEP rejection requiring per-variant isolation."""


class MyVariantOperationalError(AnnotationServiceError):
    """Carry one normalized MyVariant.info operational failure."""

    def __init__(
        self,
        message: str,
        provider_status: ProviderStatus,
    ) -> None:
        super().__init__(message)
        self.provider_status = provider_status


class ClinVarResponseError(AnnotationServiceError):
    """Raised when ClinVar returns malformed or non-matching evidence."""


class ClinVarOperationalError(AnnotationServiceError):
    """Carry one normalized direct ClinVar operational failure."""

    def __init__(
        self,
        message: str,
        provider_status: ProviderStatus,
    ) -> None:
        super().__init__(message)
        self.provider_status = provider_status


class CSpecResponseError(AnnotationServiceError):
    """Raised when CSpec returns malformed or non-matching metadata."""


class CSpecOperationalError(AnnotationServiceError):
    """Carry one normalized live CSpec operational failure."""

    def __init__(
        self,
        message: str,
        provider_status: ProviderStatus,
    ) -> None:
        super().__init__(message)
        self.provider_status = provider_status


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


@dataclass
class _ReadinessConfirmationState:
    """Consume one fresh-unreachable hint after a successful confirmation."""

    succeeded: bool = False


def _batch_provider_policy(
    *,
    provider: str,
    read_timeout: float,
    max_retries: int,
    readiness_snapshot: ProviderReadinessSnapshot | None,
    confirmation_state: _ReadinessConfirmationState | None,
) -> tuple[ProviderTimeouts, ProviderRetryPolicy, bool]:
    """Select one bounded policy from optional readiness context."""

    hint = resolve_provider_readiness_hint(readiness_snapshot, provider)
    fast_confirmation = (
        hint.state == "fresh_unreachable"
        and not (
            confirmation_state is not None
            and confirmation_state.succeeded
        )
    )
    if fast_confirmation:
        return (
            ProviderTimeouts(
                connect=FAST_CONFIRMATION_CONNECT_TIMEOUT_SECONDS,
                read=FAST_CONFIRMATION_READ_TIMEOUT_SECONDS,
            ),
            ProviderRetryPolicy(max_attempts=1),
            True,
        )
    normalized_read_timeout = float(read_timeout)
    return (
        ProviderTimeouts(
            connect=min(5.0, normalized_read_timeout),
            read=normalized_read_timeout,
        ),
        ProviderRetryPolicy(max_attempts=min(max_retries + 1, 3)),
        False,
    )


def _is_variant_level_http_rejection(status_code: int) -> bool:
    """Return whether an HTTP response requires independent isolation."""

    return status_code in {400, 409, 413, 422}


def _consume_reachable_confirmation(
    *,
    status_code: int,
    fast_confirmation: bool,
    confirmation_state: _ReadinessConfirmationState | None,
) -> None:
    """Consume a hint after an HTTP success or variant-level rejection."""

    if (
        fast_confirmation
        and confirmation_state is not None
        and (
            200 <= status_code < 300
            or _is_variant_level_http_rejection(status_code)
        )
    ):
        confirmation_state.succeeded = True


def _post_vep_batch(
    session: requests.Session,
    vep_inputs: list[str],
    max_retries: int,
    circuit_state: ProviderCircuitState | None = None,
    readiness_snapshot: ProviderReadinessSnapshot | None = None,
    confirmation_state: _ReadinessConfirmationState | None = None,
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

    timeouts, retry_policy, fast_confirmation = _batch_provider_policy(
        provider="vep",
        read_timeout=settings.VEP_TIMEOUT,
        max_retries=max_retries,
        readiness_snapshot=readiness_snapshot,
        confirmation_state=confirmation_state,
    )
    invalid_message: str | None = None

    def operation(
        timeout: tuple[float, float],
        attempt: int,
    ) -> object:
        nonlocal invalid_message
        started_at = time.perf_counter()
        try:
            response = session.post(
                endpoint,
                headers=headers,
                params=params,
                json={"variants": vep_inputs},
                timeout=timeout,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="ensembl_vep",
                operation="annotate_batch",
                attempt=attempt - 1,
                started_at=started_at,
                exception=exc,
            )
            raise

        _log_api_call(
            service="ensembl_vep",
            operation="annotate_batch",
            attempt=attempt - 1,
            started_at=started_at,
            response=response,
        )
        _consume_reachable_confirmation(
            status_code=response.status_code,
            fast_confirmation=fast_confirmation,
            confirmation_state=confirmation_state,
        )
        if not 200 <= response.status_code < 300:
            if _is_variant_level_http_rejection(response.status_code):
                raise VepVariantLevelError(
                    "Ensembl VEP rejected the variant request with "
                    f"HTTP {response.status_code}."
                )
            return response

        try:
            payload = response.json()
        except ValueError as exc:
            invalid_message = "Ensembl VEP returned invalid JSON."
            raise ProviderInvalidResponseError(
                invalid_message,
                http_status=response.status_code,
            ) from exc

        if not isinstance(payload, list) or any(
            not isinstance(item, dict)
            for item in payload
        ):
            invalid_message = (
                "Ensembl VEP returned an unexpected response structure."
            )
            raise ProviderInvalidResponseError(
                invalid_message,
                http_status=response.status_code,
            )
        return payload

    result = call_provider_with_policy(
        provider="vep",
        operation_name="annotate_batch",
        operation=operation,
        timeouts=timeouts,
        retry_policy=retry_policy,
        circuit_state=circuit_state,
        sleep=time.sleep,
    )
    if result.status == "success" and isinstance(result.value, list):
        return result.value
    if result.status == "invalid_response" and invalid_message is not None:
        message = invalid_message
    elif result.http_status is not None:
        message = f"Ensembl VEP returned HTTP {result.http_status}."
    else:
        message = (
            "Ensembl VEP request failed because the service was unavailable."
        )
    raise VepOperationalError(message, result.status)


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
    circuit_state: ProviderCircuitState | None = None,
    readiness_snapshot: ProviderReadinessSnapshot | None = None,
    confirmation_state: _ReadinessConfirmationState | None = None,
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

    timeouts, retry_policy, fast_confirmation = _batch_provider_policy(
        provider="genebe",
        read_timeout=settings.GENEBE_TIMEOUT,
        max_retries=max_retries,
        readiness_snapshot=readiness_snapshot,
        confirmation_state=confirmation_state,
    )
    invalid_message: str | None = None

    def operation(
        timeout: tuple[float, float],
        attempt: int,
    ) -> object:
        nonlocal invalid_message
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
                timeout=timeout,
                verify=True,
            )
        except requests.RequestException as exc:
            _log_api_call(
                service="genebe",
                operation="annotate_batch",
                attempt=attempt - 1,
                started_at=started_at,
                exception=exc,
            )
            raise

        _log_api_call(
            service="genebe",
            operation="annotate_batch",
            attempt=attempt - 1,
            started_at=started_at,
            response=response,
        )
        _consume_reachable_confirmation(
            status_code=response.status_code,
            fast_confirmation=fast_confirmation,
            confirmation_state=confirmation_state,
        )
        if not 200 <= response.status_code < 300:
            if _is_variant_level_http_rejection(response.status_code):
                raise GeneBeVariantLevelError(
                    "GeneBe rejected the variant request with "
                    f"HTTP {response.status_code}."
                )
            return response

        try:
            payload = response.json()
        except ValueError as exc:
            invalid_message = "GeneBe returned invalid JSON."
            raise ProviderInvalidResponseError(
                invalid_message,
                http_status=response.status_code,
            ) from exc

        if not isinstance(payload, dict):
            invalid_message = (
                "GeneBe returned an unexpected response structure."
            )
            raise ProviderInvalidResponseError(
                invalid_message,
                http_status=response.status_code,
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
            invalid_message = (
                "GeneBe returned an unexpected variant count or "
                "response structure."
            )
            raise ProviderInvalidResponseError(
                invalid_message,
                http_status=response.status_code,
            )
        return response_variants

    result = call_provider_with_policy(
        provider="genebe",
        operation_name="annotate_batch",
        operation=operation,
        timeouts=timeouts,
        retry_policy=retry_policy,
        circuit_state=circuit_state,
        sleep=time.sleep,
    )
    if result.status == "success" and isinstance(result.value, list):
        return result.value
    if result.status == "invalid_response" and invalid_message is not None:
        message = invalid_message
    elif result.http_status is not None:
        message = f"GeneBe returned HTTP {result.http_status}."
    else:
        message = "GeneBe request failed because the service was unavailable."
    raise GeneBeOperationalError(message, result.status)


# ---------------------------------------------------------------------------
# MyVariant.info HTTP client
# ---------------------------------------------------------------------------

def _normalize_chromosome(chromosome: Any) -> str | None:
    """Compatibility wrapper for the shared deterministic normalizer."""
    return _shared_normalize_chromosome(chromosome)


def _normalize_variant_edit(
    variant: VariantData,
) -> tuple[str, int, str, str] | None:
    """Compatibility wrapper for the shared deterministic normalizer."""
    return _shared_normalize_variant_edit(variant)


def _format_genomic_hgvs(
    sequence: str,
    start: int,
    reference: str,
    alternate: str,
) -> str:
    """Compatibility wrapper for shared deterministic HGVS formatting."""
    return _shared_format_genomic_hgvs(sequence, start, reference, alternate)


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
                raise MyVariantOperationalError(
                    "MyVariant.info request failed because the service "
                    "was unavailable.",
                    classify_request_exception(exc),
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
            raise MyVariantOperationalError(
                "MyVariant.info returned HTTP "
                f"{response.status_code}.",
                classify_http_status(response.status_code),
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MyVariantOperationalError(
                "MyVariant.info returned invalid JSON.",
                "invalid_response",
            ) from exc

        if not isinstance(payload, dict):
            raise MyVariantOperationalError(
                "MyVariant.info returned an unexpected response structure.",
                "invalid_response",
            )

        response_id = payload.get("_id")
        if (
            not isinstance(response_id, str)
            or response_id.upper() != variant_id.upper()
        ):
            raise MyVariantOperationalError(
                "MyVariant.info returned a record that does not exactly "
                "match the requested assembly, chromosome, position, "
                "REF, and ALT.",
                "invalid_response",
            )

        return payload, variant_id, None

    raise MyVariantOperationalError(
        "MyVariant.info retry loop ended unexpectedly.",
        "unavailable",
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


def _refresh_identifier_bundle(
    annotation: AnnotationData,
    *,
    input_index: int | None = None,
) -> None:
    """Rebuild bounded cross-provider identity after a provider stage."""

    existing = annotation.get("identifier_bundle")
    if input_index is None and isinstance(existing, dict):
        stored_index = existing.get("input_index")
        if isinstance(stored_index, int) and not isinstance(
            stored_index,
            bool,
        ):
            input_index = stored_index
    if input_index is None:
        raise AnnotationError("Identifier bundle input index is missing.")
    clinvar_identifiers = _to_clinvar_identifiers(annotation["variant"])
    annotation["identifier_bundle"] = build_variant_identifier_bundle(
        annotation,
        input_index=input_index,
        normalized_genomic_hgvs=(
            clinvar_identifiers[0]
            if clinvar_identifiers is not None
            else None
        ),
    )


def _refresh_identifier_bundles(
    annotations: list[AnnotationData],
    *,
    initialize: bool = False,
) -> None:
    for input_index, annotation in enumerate(annotations):
        _refresh_identifier_bundle(
            annotation,
            input_index=input_index if initialize else None,
        )


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
                raise ClinVarOperationalError(
                    "NCBI ClinVar request failed because the service "
                    "was unavailable.",
                    classify_request_exception(exc),
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
            raise ClinVarOperationalError(
                "NCBI ClinVar returned HTTP "
                f"{response.status_code}.",
                classify_http_status(response.status_code),
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

            raise ClinVarOperationalError(
                "NCBI ClinVar returned an API error.",
                "invalid_response",
            )

        return payload

    raise ClinVarOperationalError(
        "NCBI ClinVar retry loop ended unexpectedly.",
        "unavailable",
    )


def _search_clinvar_ids(
    session: requests.Session,
    identifier: str,
    search_field: str | None,
    max_retries: int,
) -> tuple[list[str], int, bool]:
    """Find bounded paginated ClinVar Variation IDs for one exact term."""
    term = f'"{identifier}"'
    if search_field is not None:
        term = f"{term}[{search_field}]"

    def fetch_page(retstart: int, retmax: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "db": "clinvar",
            "term": term,
            "retmode": "json",
            "retmax": retmax,
        }
        if retstart:
            params["retstart"] = retstart
        return _get_clinvar_json(
            session,
            "esearch.fcgi",
            params,
            max_retries,
        )

    def parse_page(
        payload: dict[str, Any],
        *,
        retstart: int,
        retmax: int,
        expected_count: int | None = None,
    ) -> tuple[int, list[str]]:
        result = payload.get("esearchresult")
        if not isinstance(result, dict):
            raise ClinVarResponseError(
                "NCBI ClinVar search returned an unexpected response "
                "structure."
            )
        try:
            count = int(result.get("count"))
        except (TypeError, ValueError) as exc:
            raise ClinVarResponseError(
                "NCBI ClinVar search returned an invalid result count."
            ) from exc
        if count < 0 or (
            expected_count is not None and count != expected_count
        ):
            raise ClinVarResponseError(
                "NCBI ClinVar search returned an inconsistent result count."
            )
        page_ids = result.get("idlist")
        expected_length = min(max(count - retstart, 0), retmax)
        if (
            not isinstance(page_ids, list)
            or len(page_ids) != expected_length
            or any(
                not isinstance(value, str) or not value
                for value in page_ids
            )
        ):
            raise ClinVarResponseError(
                "NCBI ClinVar search returned invalid Variation IDs."
            )
        return count, page_ids

    first_payload = fetch_page(0, MAX_CLINVAR_SEARCH_RESULTS)
    count, identifiers = parse_page(
        first_payload,
        retstart=0,
        retmax=MAX_CLINVAR_SEARCH_RESULTS,
    )
    if count == 0:
        return [], 0, False

    target_count = min(count, MAX_CLINVAR_TOTAL_CANDIDATES)
    while len(identifiers) < target_count:
        retstart = len(identifiers)
        retmax = min(
            MAX_CLINVAR_SEARCH_RESULTS,
            target_count - retstart,
        )
        payload = fetch_page(retstart, retmax)
        _, page_ids = parse_page(
            payload,
            retstart=retstart,
            retmax=retmax,
            expected_count=count,
        )
        identifiers.extend(page_ids)

    return identifiers, count, count > len(identifiers)


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


def _clinvar_identifier_is_allele_validated(
    bundle: VariantIdentifierBundle,
    identifier_type: str,
    identifier: str,
) -> bool:
    """Return whether upstream provenance binds an identifier to this allele."""
    return any(
        item.get("identifier_type") == identifier_type
        and item.get("value", "").casefold() == identifier.casefold()
        and item.get("scope") == "allele"
        and item.get("validation") == "provider_exact_allele"
        for item in bundle["provenance"]
    )


def _clinvar_query_strategies(
    annotation: AnnotationData,
    hgvs: str,
    spdi: str,
) -> list[dict[str, Any]]:
    """Build a deterministic, bounded hierarchy from validated identifiers."""
    raw_bundle = annotation.get("identifier_bundle")
    bundle: VariantIdentifierBundle = (
        raw_bundle  # type: ignore[assignment]
        if isinstance(raw_bundle, dict)
        else {}
    )
    strategies: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str]] = set()

    def add(
        strategy: str,
        identifier_type: str,
        identifier: object,
        search_field: str | None,
        *,
        allele_validated: bool = False,
    ) -> None:
        if not isinstance(identifier, str) or not identifier.strip():
            return
        value = identifier.strip()
        key = (search_field, value.casefold())
        if key in seen or len(strategies) >= MAX_CLINVAR_QUERY_STRATEGIES:
            return
        seen.add(key)
        strategies.append(
            {
                "strategy": strategy,
                "identifier_type": identifier_type,
                "identifier": value,
                "search_field": search_field,
                "allele_validated": allele_validated,
            }
        )

    # ClinVar canonical SPDI is on the current GRCh38 reference assembly.
    if settings.GENOME_ASSEMBLY == "GRCh38":
        add("canonical_spdi", "canonical_spdi", spdi, "cspdi")
    add("genomic_hgvs", "genomic_hgvs", hgvs, "varnam")

    if isinstance(raw_bundle, dict):
        for value in bundle["transcript_hgvs"]:
            add(
                "transcript_hgvs",
                "transcript_hgvs",
                value,
                "varnam",
                allele_validated=True,
            )
        for value in bundle["rsids"]:
            add(
                "rsid",
                "rsid",
                value,
                None,
                allele_validated=_clinvar_identifier_is_allele_validated(
                    bundle, "rsids", value
                ),
            )
        for value in bundle["clinvar_variation_ids"]:
            add(
                "variation_id",
                "clinvar_variation_id",
                value,
                "uid",
                allele_validated=_clinvar_identifier_is_allele_validated(
                    bundle, "clinvar_variation_ids", value
                ),
            )
        for value in bundle["vcv_accessions"]:
            add(
                "vcv_accession",
                "vcv_accession",
                value,
                "varacc",
                allele_validated=_clinvar_identifier_is_allele_validated(
                    bundle, "vcv_accessions", value
                ),
            )
        for value in bundle["rcv_accessions"]:
            add(
                "rcv_accession",
                "rcv_accession",
                value,
                "clv_acc",
            )
    return strategies


def _clinvar_spdi_rejection_reasons(
    expected_spdi: str,
    actual_spdi: object,
) -> list[str]:
    """Explain allele-level differences in one returned canonical SPDI."""
    if not isinstance(actual_spdi, str):
        return ["candidate_identity_unresolved"]
    expected = expected_spdi.split(":", 3)
    actual = actual_spdi.split(":", 3)
    if len(expected) != 4 or len(actual) != 4:
        return ["representation_unresolved"]
    reasons: list[str] = []
    if actual[0] != expected[0] or actual[1] != expected[1]:
        reasons.append("coordinate_mismatch")
    if actual[2].upper() != expected[2].upper():
        reasons.append("ref_mismatch")
    if actual[3].upper() != expected[3].upper():
        reasons.append("alt_mismatch")
    return reasons


def _clinvar_measure_rejection_reasons(
    measure: dict[str, Any],
    *,
    expected_spdi: str,
    chromosome: str,
    start: int,
    reference: str,
    alternate: str,
    strategy: dict[str, Any],
) -> list[str]:
    """Return deterministic reason codes when one measure is not exact."""
    raw_locations = measure.get("variation_loc")
    if not isinstance(raw_locations, list):
        return ["representation_unresolved"]
    locations = [
        item for item in raw_locations if isinstance(item, dict)
    ]
    if not locations:
        return ["representation_unresolved"]
    assembly_locations = [
        item
        for item in locations
        if item.get("assembly_name") == settings.GENOME_ASSEMBLY
    ]
    if not assembly_locations:
        return ["assembly_mismatch"]
    coordinate_locations = [
        item
        for item in assembly_locations
        if _normalize_chromosome(item.get("chr")) == chromosome
        and str(item.get("start", "")) == str(start)
    ]
    if not coordinate_locations:
        return ["coordinate_mismatch"]

    location = coordinate_locations[0]
    reasons: list[str] = []
    returned_ref = location.get("ref")
    returned_alt = location.get("alt")
    if isinstance(returned_ref, str) and returned_ref:
        if returned_ref.upper() != reference.upper():
            reasons.append("ref_mismatch")
    if isinstance(returned_alt, str) and returned_alt:
        if returned_alt.upper() != alternate.upper():
            reasons.append("alt_mismatch")
    if reasons:
        return reasons

    if settings.GENOME_ASSEMBLY == "GRCh38":
        return _clinvar_spdi_rejection_reasons(
            expected_spdi,
            measure.get("canonical_spdi"),
        )

    # GRCh37 ESummary does not expose an assembly-specific SPDI. Exact HGVS
    # terms, or exact-allele identifiers retained from an upstream provider,
    # remain allele-bound after assembly and coordinate validation.
    if strategy["strategy"] in {"genomic_hgvs", "transcript_hgvs"}:
        return []
    if strategy.get("allele_validated") is True:
        return []
    return ["candidate_identity_unresolved"]


def _select_exact_clinvar_record(
    summaries: list[dict[str, Any]],
    expected_spdi: str,
    chromosome: str,
    start: int,
    reference: str,
    alternate: str,
    strategy: dict[str, Any],
) -> tuple[
    tuple[dict[str, Any], dict[str, Any]] | None,
    list[dict[str, Any]],
]:
    """Select one exact record or return bounded rejection diagnostics."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejections: list[dict[str, Any]] = []

    for summary in summaries:
        measures = summary.get("variation_set")
        candidate_reasons: list[str] = []
        candidate_match: tuple[dict[str, Any], dict[str, Any]] | None = None
        if not isinstance(measures, list) or not measures:
            candidate_reasons.append("representation_unresolved")
            measures = []

        for measure in measures:
            if not isinstance(measure, dict):
                if "representation_unresolved" not in candidate_reasons:
                    candidate_reasons.append("representation_unresolved")
                continue
            reasons = _clinvar_measure_rejection_reasons(
                measure,
                expected_spdi=expected_spdi,
                chromosome=chromosome,
                start=start,
                reference=reference,
                alternate=alternate,
                strategy=strategy,
            )
            if not reasons:
                candidate_match = (summary, measure)
                break
            for reason in reasons:
                if reason not in candidate_reasons:
                    candidate_reasons.append(reason)

        if candidate_match is not None:
            matches.append(candidate_match)
        else:
            rejections.append(
                {
                    "variation_id": str(summary.get("uid", "")) or None,
                    "strategy": strategy["strategy"],
                    "reason_codes": candidate_reasons
                    or ["candidate_identity_unresolved"],
                }
            )

    if len(matches) == 1:
        return matches[0], rejections
    if len(matches) > 1:
        rejections.extend(
            {
                "variation_id": str(summary.get("uid", "")) or None,
                "strategy": strategy["strategy"],
                "reason_codes": ["candidate_identity_unresolved"],
            }
            for summary, _measure in matches
        )
    return None, rejections


def _get_clinvar(
    session: requests.Session,
    annotation: AnnotationData,
    max_retries: int,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    str | None,
    str | None,
]:
    """Retrieve one exact direct record through a bounded lookup hierarchy."""
    variant = annotation["variant"]
    source = annotation["sources"]["clinvar"]
    identifiers = _to_clinvar_identifiers(variant)
    if identifiers is None:
        return None, None, None, (
            "NCBI ClinVar does not support this chromosome or allele "
            "representation."
        )

    hgvs, spdi, chromosome, start = identifiers
    edit = _normalize_variant_edit(variant)
    if edit is None:
        return None, None, hgvs, None
    _, _, reference, alternate = edit
    query_trace: list[dict[str, Any]] = []
    candidate_rejections: list[dict[str, Any]] = []
    identifiers_used: list[IdentifierReference] = []
    candidate_ids: set[str] = set()
    rejected_candidate_ids: set[str | None] = set()

    for sequence, strategy in enumerate(
        _clinvar_query_strategies(annotation, hgvs, spdi),
        start=1,
    ):
        reference_record: IdentifierReference = {
            "type": strategy["identifier_type"],
            "value": strategy["identifier"],
        }
        if reference_record not in identifiers_used:
            identifiers_used.append(reference_record)
        variation_ids, result_count, truncated = _search_clinvar_ids(
            session,
            strategy["identifier"],
            strategy["search_field"],
            max_retries,
        )
        attempt = {
            "sequence": sequence,
            "strategy": strategy["strategy"],
            "identifier_type": strategy["identifier_type"],
            "identifier": strategy["identifier"],
            "search_field": strategy["search_field"] or "all",
            "result_count": result_count,
            "candidate_ids": list(variation_ids),
            "results_truncated": truncated,
            "candidates_rejected": 0,
            "outcome": "no_candidates",
        }
        query_trace.append(attempt)
        if not variation_ids:
            continue
        candidate_ids.update(variation_ids)
        summaries = _get_clinvar_summaries(
            session,
            variation_ids,
            max_retries,
        )
        selected, rejections = _select_exact_clinvar_record(
            summaries,
            spdi,
            chromosome,
            start,
            reference,
            alternate,
            strategy,
        )
        attempt["candidates_rejected"] = len(rejections)
        rejected_candidate_ids.update(
            item.get("variation_id") for item in rejections
        )
        candidate_rejections.extend(
            rejections[: max(
                0,
                MAX_CLINVAR_CANDIDATE_REJECTIONS
                - len(candidate_rejections),
            )]
        )
        if selected is None:
            attempt["outcome"] = "candidates_rejected"
            continue
        attempt["outcome"] = "verified"
        source.update(
            {
                "selected_query_strategy": strategy["strategy"],
                "query_strategies": query_trace,
                "identifiers_used": identifiers_used,
                "candidate_count": len(candidate_ids),
                "candidates_rejected": len(rejected_candidate_ids),
                "candidate_rejections": candidate_rejections,
            }
        )
        return selected[0], selected[1], hgvs, None

    source.update(
        {
            "selected_query_strategy": None,
            "query_strategies": query_trace,
            "identifiers_used": identifiers_used,
            "candidate_count": len(candidate_ids),
            "candidates_rejected": len(rejected_candidate_ids),
            "candidate_rejections": candidate_rejections,
        }
    )
    return None, None, hgvs, None


# ---------------------------------------------------------------------------
# ClinGen evidence through the UCSC GenCC track
# ---------------------------------------------------------------------------

def _select_clingen_gene(annotation: AnnotationData) -> str | None:
    """Select one unambiguous gene symbol from standardized source fields."""
    resolution = annotation.get("gene_identity_resolution")
    if not isinstance(resolution, dict) or resolution.get("status") != "resolved":
        return None
    gene = resolution.get("gene")
    if not isinstance(gene, str):
        return None
    symbol = gene.strip()
    return symbol if CLINGEN_GENE_SYMBOL_PATTERN.fullmatch(symbol) else None


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
                raise CSpecOperationalError(
                    "ClinGen CSpec request failed because the service "
                    "was unavailable.",
                    classify_request_exception(exc),
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
            raise CSpecOperationalError(
                "ClinGen CSpec returned HTTP "
                f"{response.status_code}.",
                classify_http_status(response.status_code),
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

    raise CSpecOperationalError(
        "ClinGen CSpec retry loop ended unexpectedly.",
        "unavailable",
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
    vep_failure: ProviderStatus | None = None,
    vep_failure_scope: (
        Literal["provider_wide", "variant_level"] | None
    ) = None,
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
        "population_frequency_provenance": {
            "status": "pending",
            "provider": MYVARIANT_PROVIDER_NAME,
            "operational_provider": "myvariant",
            "underlying_dataset": None,
            "assembly": settings.GENOME_ASSEMBLY,
            "retrieved_at": None,
            "selected_frequency": None,
            "selection_method": "maximum_exact_alt_global_af",
            "fallback_used": False,
            "primary_provider": "myvariant",
            "primary_failure": None,
            "continuation_reason": None,
        },
        "sources": {
            "vep": {
                "status": status,
                "provider": VEP_PROVIDER_NAME,
                "provider_role": "primary",
                "fallback_used": False,
                "primary_provider": VEP_PROVIDER_NAME,
                "primary_failure": vep_failure,
                "fallback_provider": VARIANTVALIDATOR_PROVIDER_NAME,
                "fallback_status": "not_triggered",
                "fallback_failure": None,
                "failure_scope": (
                    vep_failure_scope
                    or (
                        "provider_wide"
                        if vep_failure is not None
                        else None
                    )
                ),
                "source_type": "direct",
                "provider_version": None,
                "retrieved_at": retrieved_at or _retrieval_timestamp(),
                "assembly": settings.GENOME_ASSEMBLY,
                "most_severe_consequence": None,
                "transcript_consequences": [],
                "total_transcript_consequences": 0,
                "transcripts_truncated": False,
                "normalized_variant": {
                    "assembly": settings.GENOME_ASSEMBLY,
                    "chrom": _normalize_chromosome(variant["chrom"]),
                    "pos": int(variant["pos"]),
                    "ref": str(variant["ref"]).upper(),
                    "alt": str(variant["alt"]).upper(),
                },
                "validated_genomic_hgvs": None,
                "validated_transcript_hgvs": None,
                "validated_protein_hgvs": None,
                "consequence_available": False,
            },
            "genebe": {
                "status": "pending",
                "provider": GENEBE_PROVIDER_NAME,
                "primary_failure": None,
                "failure_scope": None,
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
                "provider_role": "primary",
                "fallback_used": False,
                "primary_provider": MYVARIANT_PROVIDER_NAME,
                "primary_failure": None,
                "fallback_provider": ENSEMBL_VARIATION_PROVIDER_NAME,
                "fallback_status": "not_triggered",
                "fallback_failure": None,
                "source_type": "direct",
                "provider_version": MYVARIANT_API_VERSION,
                "retrieved_at": None,
                "upstream_sources": [],
                "variant_id": None,
                "rsid": None,
                "gene": None,
                "population_frequencies": {},
                "population_frequency_details": {},
                "max_population_frequency": None,
                "ensembl_variation": None,
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
                "selected_query_strategy": None,
                "query_strategies": [],
                "identifiers_used": [],
                "candidate_count": 0,
                "candidates_rejected": 0,
                "candidate_rejections": [],
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
                "provider_role": "primary",
                "fallback_used": False,
                "primary_provider": "ClinGen CSpec Registry",
                "primary_failure": None,
                "fallback_provider": "Local CSpec last-known-good cache",
                "fallback_status": "not_triggered",
                "fallback_failure": None,
                "source": "live_cspec",
                "source_type": "direct",
                "provider_version": None,
                "retrieved_at": None,
                "source_retrieved_at": None,
                "cache_stored_at": None,
                "fallback_used_at": None,
                "freshness_status": "live",
                "query_gene": None,
                "query_disease_ids": [],
                "disease_queries_truncated": False,
                "query_scope": {
                    "gene_symbol": None,
                    "hgnc_id": None,
                    "mondo_ids": [],
                    "omim_ids": [],
                    "medgen_ids": [],
                    "disease_query_policy": "mondo_exact_only",
                },
                "scope_audit": {
                    "gene_entity_found": None,
                    "gene_linked_specification_count": 0,
                    "released_specification_count": 0,
                    "unreleased_specification_count": 0,
                    "disease_entities_queried": 0,
                    "disease_entities_found": 0,
                    "disease_linked_specification_count": 0,
                    "cache_policy": "operational_failure_only",
                },
                "applicability_status": "pending",
                "applicability_message": None,
                "no_match_reason": None,
                "specification_available": False,
                "specifications": [],
                "specification_count": 0,
                "specifications_truncated": False,
                "context_type": "gene_disease_acmg_specification",
                "classification_effect": "context_only",
                "rule_logic_applied": False,
            },
            "erepo": {
                "status": "pending",
                "provider": "ClinGen ERepo",
                "provider_id": "clingen_erepo",
                "retrieved_at": None,
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
        annotation["sources"]["vep"][
            "primary_failure"
        ] = "invalid_response"
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
            "consequence_available": most_severe is not None,
        }
    )

    if representative is None:
        annotation["warnings"].append(
            "Ensembl VEP returned no transcript consequences."
        )
        return annotation

    annotation.update(
        {
            "gene": representative.get("gene_symbol"),
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
    circuit_state: ProviderCircuitState,
    readiness_snapshot: ProviderReadinessSnapshot | None,
    confirmation_state: _ReadinessConfirmationState,
) -> bool:
    """Batch GeneBe evidence without overwriting VEP annotations."""

    provider_wide_failure = False
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
                circuit_state,
                readiness_snapshot,
                confirmation_state,
            )
        except AnnotationServiceError as exc:
            variant_level = isinstance(exc, GeneBeVariantLevelError)
            provider_wide_failure = (
                provider_wide_failure or not variant_level
            )
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
                            if variant_level
                            or (
                                isinstance(exc, GeneBeOperationalError)
                                and exc.provider_status == "invalid_response"
                            )
                            else "unavailable"
                        ),
                        "primary_failure": (
                            None
                            if variant_level
                            else (
                                exc.provider_status
                                if isinstance(exc, GeneBeOperationalError)
                                else "unavailable"
                            )
                        ),
                        "failure_scope": (
                            "variant_level"
                            if variant_level
                            else "provider_wide"
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
                        "failure_scope": "variant_level",
                        "retrieved_at": _retrieval_timestamp(),
                    }
                )
                annotation["warnings"].append(str(exc))

    return provider_wide_failure


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


def _extract_gnomad_frequency_records(
    source: Any,
) -> list[dict[str, Any]]:
    """Retain per-record gnomAD AF values from an exact MyVariant result."""

    records: list[dict[str, Any]] = []
    for record in _iter_source_records(source):
        raw_frequency = record.get("af")
        global_frequency: float | None = None
        population_frequencies: dict[str, float] = {}

        if isinstance(raw_frequency, dict):
            global_frequency = _valid_frequency(raw_frequency.get("af"))
            for raw_label, raw_value in raw_frequency.items():
                if (
                    not isinstance(raw_label, str)
                    or not raw_label.startswith("af_")
                ):
                    continue
                frequency = _valid_frequency(raw_value)
                if frequency is not None:
                    population_frequencies[
                        raw_label.removeprefix("af_").upper()
                    ] = frequency
        else:
            global_frequency = _valid_frequency(raw_frequency)

        if global_frequency is not None or population_frequencies:
            records.append(
                {
                    "global_af": global_frequency,
                    "population_allele_frequencies": (
                        population_frequencies
                    ),
                }
            )

    return records


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


def _myvariant_clinvar_conditions(value: Any) -> list[dict[str, Any]]:
    """Normalize bounded MyVariant ClinVar-derived condition records."""

    source_labels = {
        "medgen": "MedGen",
        "mondo": "MONDO",
        "omim": "OMIM",
        "orphanet": "Orphanet",
    }
    conditions: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for record in _iter_source_records(value):
        name = _optional_text(record.get("name"))
        if name is None:
            continue
        identifiers: list[dict[str, str]] = []
        raw_identifiers = record.get("identifiers")
        if isinstance(raw_identifiers, dict):
            for source, raw_identifier in raw_identifiers.items():
                values = (
                    raw_identifier
                    if isinstance(raw_identifier, list)
                    else [raw_identifier]
                )
                for value in values:
                    identifier = _optional_text(value)
                    if identifier is None:
                        continue
                    normalized_source = source_labels.get(
                        str(source).casefold(),
                        str(source),
                    )
                    item = {
                        "source": normalized_source,
                        "id": identifier,
                    }
                    if item not in identifiers:
                        identifiers.append(item)
        key = (
            name,
            tuple(
                (item["source"], item["id"])
                for item in identifiers
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        conditions.append(
            {"name": name, "identifiers": identifiers}
        )
        if len(conditions) >= MAX_CLINVAR_CONDITIONS:
            break
    return conditions


def _myvariant_clinvar_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    """Retain bounded ClinVar-derived fields for possible fallback use."""

    clinvar_records = list(_iter_source_records(payload.get("clinvar")))
    if not clinvar_records:
        return {"status": "not_found"}

    rcv_records: list[dict[str, Any]] = []
    for clinvar_record in clinvar_records:
        for record in _iter_source_records(clinvar_record.get("rcv")):
            raw_origin = record.get("origin")
            origins = [
                origin
                for value in (
                    raw_origin
                    if isinstance(raw_origin, list)
                    else [raw_origin]
                )
                if (origin := _optional_text(value))
            ]
            if origins and not any(
                "germline" in origin.casefold()
                for origin in origins
            ):
                continue
            rcv_records.append(record)

    significances = _bounded_string_list(
        [
            significance
            for record in rcv_records
            if (
                significance := _optional_text(
                    record.get("clinical_significance")
                )
            )
        ],
        MAX_CLINVAR_ACCESSIONS,
    )
    review_statuses = _bounded_string_list(
        [
            review_status
            for record in rcv_records
            if (
                review_status := _optional_text(
                    record.get("review_status")
                )
            )
        ],
        MAX_CLINVAR_ACCESSIONS,
    )
    rcv_accessions = _bounded_string_list(
        [
            accession
            for record in rcv_records
            if (
                accession := _optional_text(record.get("accession"))
            )
        ],
        MAX_CLINVAR_ACCESSIONS,
    )
    conditions = _myvariant_clinvar_conditions(
        [
            condition
            for record in rcv_records
            for condition in _iter_source_records(
                record.get("conditions")
            )
        ]
    )
    last_evaluated_values = sorted(
        {
            value
            for record in rcv_records
            if (
                value := _optional_text(record.get("last_evaluated"))
            )
        },
        reverse=True,
    )
    first_clinvar = clinvar_records[0]
    variation_value = first_clinvar.get("variant_id")
    variation_id = (
        str(variation_value)
        if isinstance(variation_value, (int, str))
        and not isinstance(variation_value, bool)
        and str(variation_value).strip()
        else None
    )
    allele_value = first_clinvar.get("allele_id")
    allele_id = (
        str(allele_value)
        if isinstance(allele_value, (int, str))
        and not isinstance(allele_value, bool)
        and str(allele_value).strip()
        else None
    )
    usable = bool(
        significances
        or review_statuses
        or rcv_accessions
        or conditions
    )
    return {
        "status": "available" if usable else "not_found",
        "provider": MYVARIANT_PROVIDER_NAME,
        "provider_version": MYVARIANT_API_VERSION,
        "upstream_source": "ClinVar",
        "derivation": "derived",
        "independent_evidence": False,
        "variation_id": variation_id,
        "allele_id": allele_id,
        "gene": _first_nested_string(
            clinvar_records,
            "gene",
            "symbol",
        ),
        "clinical_significance": (
            significances[0] if len(significances) == 1 else None
        ),
        "review_status": (
            review_statuses[0] if len(review_statuses) == 1 else None
        ),
        "last_evaluated": (
            last_evaluated_values[0]
            if last_evaluated_values
            else None
        ),
        "conditions": conditions,
        "condition_count": len(conditions),
        "conditions_truncated": False,
        "scv_accessions": [],
        "scv_accession_count": 0,
        "scv_accessions_truncated": False,
        "rcv_accessions": rcv_accessions,
        "rcv_accession_count": len(rcv_accessions),
        "rcv_accessions_truncated": False,
        "ambiguous_classifications": (
            significances if len(significances) > 1 else []
        ),
    }


def _standardize_myvariant_response(
    annotation: AnnotationData,
    payload: dict[str, Any],
    variant_id: str,
) -> None:
    """Add bounded MyVariant evidence without retaining its raw payload."""
    dbsnp = payload.get("dbsnp")
    population_frequencies: dict[str, float] = {}
    population_frequency_details = {
        "source": "gnomAD via MyVariant.info",
        "assembly": settings.GENOME_ASSEMBLY,
        "variant_id": variant_id,
        "gnomad_exome": _extract_gnomad_frequency_records(
            payload.get("gnomad_exome")
        ),
        "gnomad_genome": _extract_gnomad_frequency_records(
            payload.get("gnomad_genome")
        ),
    }

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
    annotation["population_frequency_provenance"] = {
        "status": (
            "available" if max_frequency is not None else "no_usable_frequency"
        ),
        "provider": MYVARIANT_PROVIDER_NAME,
        "operational_provider": "myvariant",
        "underlying_dataset": "MyVariant.info aggregated sources",
        "assembly": settings.GENOME_ASSEMBLY,
        "retrieved_at": annotation["sources"]["myvariant"]["retrieved_at"],
        "variant_id": variant_id,
        "selected_frequency": max_frequency,
        "selection_method": "maximum_exact_alt_global_af",
        "available_global_af": dict(population_frequencies),
        "fallback_used": False,
        "primary_provider": "myvariant",
        "primary_failure": None,
        "continuation_reason": None,
    }
    annotation["sources"]["myvariant"].update(
        {
            "status": "success",
            "upstream_sources": sorted(set(upstream_sources)),
            "variant_id": variant_id,
            "rsid": _first_nested_string(dbsnp, "rsid"),
            "gene": gene,
            "population_frequencies": population_frequencies,
            "population_frequency_details": population_frequency_details,
            "max_population_frequency": max_frequency,
        }
    )
    clinvar_candidate = _myvariant_clinvar_candidate(payload)
    if clinvar_candidate["status"] != "not_found":
        annotation["sources"]["myvariant"][
            "clinvar_derived"
        ] = clinvar_candidate
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
        annotation["sources"]["myvariant"]["primary_failure"] = (
            exc.provider_status
            if isinstance(exc, MyVariantOperationalError)
            else "unavailable"
        )
        annotation["warnings"].append(str(exc))
        return

    if unsupported_warning is not None:
        annotation["sources"]["myvariant"]["status"] = "unsupported"
        annotation["warnings"].append(unsupported_warning)
        return

    if payload is None or variant_id is None:
        annotation["sources"]["myvariant"].update(
            {
                "status": "not_found",
                "variant_id": variant_id,
            }
        )
        annotation["warnings"].append(
            "MyVariant.info returned no exact result for this variant."
        )
        return

    _standardize_myvariant_response(
        annotation,
        payload,
        variant_id,
    )


def _has_usable_population_frequency(value: object) -> bool:
    """Return whether the normal-frequency field contains a valid AF."""

    return _valid_frequency(value) is not None


def _apply_ucsc_normal_population_frequency_fallback(
    annotation: AnnotationData,
    session: requests.Session,
) -> None:
    """Use strict UCSC gnomAD AF only when MyVariant has no usable AF."""

    if _has_usable_population_frequency(annotation.get("population_frequency")):
        return

    myvariant = annotation["sources"]["myvariant"]
    primary_failure = myvariant.get("primary_failure")
    ucsc = fetch_ucsc_gnomad_evidence(annotation, session=session)
    global_af = ucsc.get("global_af")
    usable_af = {
        source: frequency
        for source, frequency in (
            global_af.items() if isinstance(global_af, dict) else ()
        )
        if source in {"exome", "genome"}
        and _valid_frequency(frequency) is not None
    }
    fallback_used = (
        isinstance(primary_failure, str)
        and should_trigger_fallback(primary_failure)
    )
    continuation_reason = (
        None
        if fallback_used
        else "MyVariant.info returned no usable exact-allele frequency."
    )
    provenance: dict[str, Any] = {
        "status": ucsc.get("status"),
        "provider": ucsc.get("provider", UCSC_GNOMAD_PROVIDER_NAME),
        "operational_provider": ucsc.get("operational_provider", "ucsc_gnomad"),
        "underlying_dataset": ucsc.get("underlying_dataset", "gnomAD"),
        "assembly": ucsc.get("assembly", annotation.get("assembly")),
        "retrieved_at": ucsc.get("retrieved_at"),
        "variant_id": ucsc.get("variant_id"),
        "release": ucsc.get("release"),
        "source_url": ucsc.get("source_url"),
        "selected_frequency": None,
        "selection_method": "maximum_exact_allele_global_af_across_available_exome_genome_tracks",
        "available_global_af": usable_af,
        "filter_status": ucsc.get("filter_status", {}),
        "track_results": ucsc.get("track_results", []),
        "fallback_used": fallback_used,
        "primary_provider": "myvariant",
        "primary_failure": primary_failure,
        "continuation_reason": continuation_reason,
    }
    if not usable_af or ucsc.get("status") not in {"available", "partial"}:
        annotation["population_frequency_provenance"] = provenance
        return

    selected_frequency = max(usable_af.values())
    annotation["population_frequency"] = selected_frequency
    provenance["status"] = "available"
    provenance["selected_frequency"] = selected_frequency
    annotation["population_frequency_provenance"] = provenance
    source_url = ucsc.get("source_url")
    if isinstance(source_url, str) and source_url:
        annotation["references"].append(
            {"source": UCSC_GNOMAD_PROVIDER_NAME, "url": source_url}
        )
    annotation["warnings"].append(
        "MyVariant.info did not provide a usable exact-allele population "
        "frequency; UCSC gnomAD strict mirror evidence supplied the normal "
        "population-frequency fallback."
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


def _activate_myvariant_clinvar_fallback(
    annotation: AnnotationData,
    primary_failure: ProviderStatus,
) -> bool:
    """Promote exact MyVariant ClinVar-derived fields after primary failure."""

    if not should_trigger_fallback(primary_failure):
        return False
    source = annotation["sources"]["clinvar"]
    myvariant = annotation["sources"]["myvariant"]
    derived = myvariant.get("clinvar_derived")
    if (
        myvariant.get("status") != "success"
        or not isinstance(derived, dict)
        or derived.get("status") != "available"
    ):
        source.update(
            {
                "primary_failure": primary_failure,
                "fallback_attempted": True,
                "fallback_provider": "MyVariant.info",
                "fallback_status": "unavailable",
            }
        )
        return False

    provenance = build_provider_provenance(
        capability="clinvar_evidence",
        provider="myvariant",
        provider_role="fallback",
        primary_provider="ncbi_clinvar",
        primary_failure=primary_failure,
    )
    source.update(
        {
            "status": "success",
            "direct_verification_status": primary_failure,
            "provider": MYVARIANT_PROVIDER_NAME,
            "provider_version": MYVARIANT_API_VERSION,
            "api": "MyVariant.info variant annotation API",
            "api_version": MYVARIANT_API_VERSION,
            "source_type": "derived_fallback",
            "retrieved_at": myvariant.get("retrieved_at"),
            "upstream_sources": ["ClinVar"],
            "operational_provider": provenance["provider"],
            "capability": provenance["capability"],
            "provider_role": provenance["provider_role"],
            "fallback_used": provenance["fallback_used"],
            "primary_provider": provenance["primary_provider"],
            "primary_failure": provenance["primary_failure"],
            "fallback_for": "ncbi_clinvar",
            "fallback_attempted": True,
            "fallback_provider": MYVARIANT_PROVIDER_NAME,
            "fallback_status": "success",
            "independent_evidence": False,
            "variation_id": derived.get("variation_id"),
            "accession": None,
            "accession_version": None,
            "gene": derived.get("gene"),
            "clinical_significance": derived.get(
                "clinical_significance"
            ),
            "review_status": derived.get("review_status"),
            "last_evaluated": derived.get("last_evaluated"),
            "conditions": deepcopy(derived.get("conditions", [])),
            "condition_count": derived.get("condition_count", 0),
            "conditions_truncated": derived.get(
                "conditions_truncated",
                False,
            ),
            "scv_accessions": [],
            "scv_accession_count": 0,
            "scv_accessions_truncated": False,
            "rcv_accessions": deepcopy(
                derived.get("rcv_accessions", [])
            ),
            "rcv_accession_count": derived.get(
                "rcv_accession_count",
                0,
            ),
            "rcv_accessions_truncated": derived.get(
                "rcv_accessions_truncated",
                False,
            ),
            "conflicting_submissions": _clinvar_conflict_evidence(
                derived.get("review_status"),
                derived.get("clinical_significance"),
            ),
        }
    )
    annotation["warnings"].append(
        "Direct NCBI ClinVar was unavailable; ClinVar-derived fields "
        "from MyVariant.info were used as a non-independent fallback."
    )
    return True


def _record_myvariant_clinvar_rescue(
    annotation: AnnotationData,
) -> bool:
    """Trace retained MyVariant ClinVar evidence after a direct no-match."""

    source = annotation["sources"]["clinvar"]
    myvariant = annotation["sources"]["myvariant"]
    derived = myvariant.get("clinvar_derived")
    variant_id = _optional_text(myvariant.get("variant_id"))
    if variant_id is None:
        variant_id = _to_myvariant_hgvs(annotation["variant"])

    alternate_identifiers: list[EvidenceRescueIdentifier] = []
    if variant_id is not None:
        alternate_identifiers.append(
            {
                "type": "myvariant_variant_id",
                "value": variant_id,
            }
        )

    recovered = (
        myvariant.get("status") == "success"
        and isinstance(derived, dict)
        and derived.get("status") == "available"
    )
    attempts: list[EvidenceRescueAttempt] = []
    if variant_id is not None:
        raw_status = myvariant.get("status")
        attempt_status: Literal[
            "success",
            "no_match",
            "unavailable",
            "invalid_response",
            "not_applicable",
        ]
        if recovered:
            attempt_status = "success"
        elif raw_status in {"success", "not_found"}:
            attempt_status = "no_match"
        elif raw_status == "invalid_response":
            attempt_status = "invalid_response"
        elif raw_status == "unsupported":
            attempt_status = "not_applicable"
        else:
            attempt_status = "unavailable"
        attempts.append(
            {
                "sequence": 1,
                "provider": "myvariant",
                "method": "reuse_exact_myvariant_clinvar_derivation",
                "identifier_type": "myvariant_variant_id",
                "identifier_value": variant_id,
                "status": attempt_status,
                "evidence_recovered": recovered,
                "evidence_path": (
                    "annotations.population.clinvar_derived"
                    if recovered
                    else None
                ),
                "independent_evidence": False,
            }
        )

    if recovered:
        stop_reason = "evidence_recovered"
    elif variant_id is None:
        stop_reason = "missing_identifier"
    elif myvariant.get("status") in {"success", "not_found"}:
        stop_reason = "no_secondary_evidence"
    else:
        stop_reason = "secondary_unavailable"

    source["evidence_rescue"] = build_evidence_rescue_trace(
        capability="clinvar_evidence",
        trigger="primary_no_match",
        primary_provider="ncbi_clinvar",
        primary_status="no_match",
        alternate_identifiers=alternate_identifiers,
        attempts=attempts,
        stop_reason=stop_reason,
    )
    if recovered:
        annotation["warnings"].append(
            "Direct NCBI ClinVar returned no exact result; existing "
            "ClinVar-derived evidence from MyVariant.info was retained "
            "as non-independent evidence rescue."
        )
    return recovered


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
            annotation,
            max_retries,
        )
    except AnnotationServiceError as exc:
        LOGGER.error(
            "event=annotation_source_failed service=ncbi_clinvar "
            "error_type=%s",
            type(exc).__name__,
        )
        primary_failure: ProviderStatus = (
            exc.provider_status
            if isinstance(exc, ClinVarOperationalError)
            else (
                "invalid_response"
                if isinstance(exc, ClinVarResponseError)
                else "unavailable"
            )
        )
        source_status = (
            "invalid_response"
            if primary_failure == "invalid_response"
            else "unavailable"
        )
        source["status"] = source_status
        source["direct_verification_status"] = source_status
        annotation["warnings"].append(str(exc))
        _activate_myvariant_clinvar_fallback(
            annotation,
            primary_failure,
        )
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
        _record_myvariant_clinvar_rescue(annotation)
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

    bundle = annotation.get("identifier_bundle")
    bundled_mondo_ids = (
        bundle.get("mondo_ids")
        if isinstance(bundle, dict)
        else None
    )
    if isinstance(bundled_mondo_ids, list):
        for disease_id in bundled_mondo_ids:
            if (
                isinstance(disease_id, str)
                and MONDO_IDENTIFIER_PATTERN.fullmatch(disease_id)
                and disease_id not in disease_ids
            ):
                disease_ids.append(disease_id)

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


def _cspec_query_scope(
    annotation: AnnotationData,
    *,
    gene: str,
    mondo_ids: list[str],
) -> dict[str, Any]:
    """Describe exact queryable and context-only disease identifiers."""
    bundle = annotation.get("identifier_bundle")
    if not isinstance(bundle, dict):
        bundle = {}

    def identifiers(field: str) -> list[str]:
        values = bundle.get(field)
        if not isinstance(values, list):
            return []
        return [
            value
            for value in values
            if isinstance(value, str) and value.strip()
        ]

    hgnc_id = bundle.get("hgnc_id")
    return {
        "gene_symbol": gene,
        "hgnc_id": (
            hgnc_id.strip()
            if isinstance(hgnc_id, str) and hgnc_id.strip()
            else None
        ),
        "mondo_ids": list(mondo_ids),
        # The public CSpec Disease endpoint is MONDO-keyed. OMIM and MedGen
        # identifiers remain useful context but are not sent as if supported.
        "omim_ids": identifiers("omim_ids"),
        "medgen_ids": identifiers("medgen_ids"),
        "disease_query_policy": "mondo_exact_only",
    }


def _set_cspec_applicability(
    source: dict[str, Any],
    *,
    status: str,
    message: str,
    no_match_reason: str | None = None,
) -> None:
    """Record a source-specific CSpec scope outcome."""
    source["applicability_status"] = status
    source["applicability_message"] = message
    source["no_match_reason"] = no_match_reason


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
        role_id = (
            role.get("id")
            if isinstance(role, dict)
            else None
        )
        if isinstance(role, str):
            match = re.search(
                r"(?:^|[;{])\s*id=([^;}]+)",
                role,
                flags=re.IGNORECASE,
            )
            role_id = match.group(1).strip() if match else None
        if role_id != "researchgroup":
            continue
        if isinstance(person_or_org, dict):
            return _optional_clingen_string(
                person_or_org.get("name")
            )
        if isinstance(person_or_org, str):
            match = re.search(
                r"(?:^|[;{])\s*name=([^;}]+)",
                person_or_org,
                flags=re.IGNORECASE,
            )
            if match:
                return _optional_clingen_string(match.group(1))
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


def _cache_cspec_last_known_good(
    source: dict[str, Any],
    *,
    gene: str,
    disease_ids: list[str],
) -> str | None:
    """Persist only validated released metadata; return a safe warning on failure."""

    retrieved_at = source.get("retrieved_at")
    specifications = source.get("specifications")
    if not isinstance(retrieved_at, str) or not isinstance(
        specifications,
        list,
    ):
        return "Released CSpec metadata could not be stored in the local cache."
    try:
        store_cspec_lkg(
            settings.CSPEC_LKG_CACHE_PATH,
            gene=gene,
            disease_ids=disease_ids,
            source_retrieved_at=retrieved_at,
            cache_stored_at=_retrieval_timestamp(),
            specifications=specifications,
        )
    except (CSpecCacheError, OSError):
        LOGGER.error(
            "event=cspec_cache_write_failed error_type=cache_storage_error"
        )
        return "Released CSpec metadata could not be stored in the local cache."
    return None


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
        _set_cspec_applicability(
            source,
            status="gene_scope_unavailable",
            message=(
                "ClinGen CSpec applicability could not be assessed because "
                "no unambiguous gene symbol was available."
            ),
            no_match_reason="missing_unambiguous_gene_symbol",
        )
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
    source["query_scope"] = _cspec_query_scope(
        annotation,
        gene=gene,
        mondo_ids=disease_ids,
    )
    source["scope_audit"]["disease_entities_queried"] = len(
        disease_ids
    )

    try:
        gene_entity = _get_cspec_entity(
            session,
            "Gene",
            gene,
            max_retries,
        )
        if gene_entity is None:
            source["status"] = "not_found"
            source["no_match_stage"] = "gene_entity_absent"
            source["scope_audit"]["gene_entity_found"] = False
            _set_cspec_applicability(
                source,
                status="no_applicable_specification",
                message=(
                    "No applicable ClinGen CSpec specification was "
                    "identified for the current gene/disease scope."
                ),
                no_match_reason="gene_entity_absent",
            )
            return

        disease_links: dict[str, set[str]] = {}
        disease_entities_found = 0
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
            if disease_entity is not None:
                disease_entities_found += 1

        raw_records = _linked_cspec_records(gene_entity)
        source["scope_audit"].update(
            {
                "gene_entity_found": True,
                "gene_linked_specification_count": len(raw_records),
                "disease_entities_found": disease_entities_found,
                "disease_linked_specification_count": len(
                    set().union(*disease_links.values())
                    if disease_links
                    else set()
                ),
            }
        )
        specifications: list[dict[str, Any]] = []
        unreleased_count = 0
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
            else:
                unreleased_count += 1

        source["scope_audit"].update(
            {
                "released_specification_count": len(specifications),
                "unreleased_specification_count": unreleased_count,
            }
        )

        if not specifications:
            source["status"] = "not_found"
            source["no_match_stage"] = "no_released_specifications"
            reason = (
                "gene_present_no_linked_specifications"
                if not raw_records
                else "gene_present_only_unreleased_specifications"
            )
            _set_cspec_applicability(
                source,
                status="no_applicable_specification",
                message=(
                    "No applicable ClinGen CSpec specification was "
                    "identified for the current gene/disease scope."
                ),
                no_match_reason=reason,
            )
            return

        retained = specifications[:MAX_CSPEC_SPECIFICATIONS]
        source.update(
            {
                "status": "success",
                "source_retrieved_at": source["retrieved_at"],
                "specification_available": True,
                "specifications": retained,
                "specification_count": len(specifications),
                "specifications_truncated": (
                    len(specifications) > len(retained)
                ),
            }
        )
        has_disease_match = any(
            bool(item["matched_disease_ids"])
            for item in specifications
        )
        if has_disease_match:
            _set_cspec_applicability(
                source,
                status="released_gene_and_disease_specification",
                message=(
                    "Released ClinGen CSpec context was identified for "
                    "the current gene/disease scope."
                ),
            )
        elif disease_ids:
            _set_cspec_applicability(
                source,
                status="released_gene_specification_disease_unmatched",
                message=(
                    "Released ClinGen CSpec context was identified for "
                    "the gene, but applicability to the current disease "
                    "scope was not established."
                ),
            )
        else:
            _set_cspec_applicability(
                source,
                status="released_gene_specification",
                message=(
                    "Released ClinGen CSpec context was identified for "
                    "the current gene scope; no MONDO disease scope was "
                    "available to assess."
                ),
            )
        cache_warning = _cache_cspec_last_known_good(
            source,
            gene=gene,
            disease_ids=disease_ids,
        )
        if cache_warning is not None:
            annotation["warnings"].append(cache_warning)

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
        source["primary_failure"] = (
            exc.provider_status
            if isinstance(exc, CSpecOperationalError)
            else "invalid_response"
            if isinstance(exc, CSpecResponseError)
            else "unavailable"
        )
        annotation["warnings"].append(str(exc))
        _set_cspec_applicability(
            source,
            status="verification_unavailable",
            message=(
                "ClinGen CSpec applicability could not be verified because "
                "the registry response was unavailable or invalid."
            ),
        )


def _apply_cspec_lkg_fallback(annotation: AnnotationData) -> None:
    """Use an exact local CSpec LKG entry only after live operational failure."""

    source = annotation["sources"]["cspec"]
    primary_failure = source.get("primary_failure")
    if not isinstance(primary_failure, str) or not should_trigger_fallback(
        primary_failure
    ):
        source["fallback_status"] = "not_eligible"
        return
    gene = source.get("query_gene")
    disease_ids = source.get("query_disease_ids")
    if not isinstance(gene, str) or not isinstance(disease_ids, list):
        source["fallback_status"] = "no_match"
        return

    cache_status, entry = load_cspec_lkg(
        settings.CSPEC_LKG_CACHE_PATH,
        gene=gene,
        disease_ids=disease_ids,
    )
    source["fallback_status"] = cache_status
    source["cache_status"] = cache_status
    if cache_status != "success" or entry is None:
        source["fallback_failure"] = (
            "invalid_response" if cache_status == "invalid" else None
        )
        annotation["warnings"].append(
            (
                "The local CSpec last-known-good cache was invalid; live "
                "CSpec context remains unavailable."
                if cache_status == "invalid"
                else "No exact local CSpec last-known-good entry was "
                "available; live CSpec context remains unavailable."
            )
        )
        return

    specifications = entry["specifications"]
    provenance = build_provider_provenance(
        capability="cspec_context",
        provider="cached_cspec",
        provider_role="fallback",
        primary_provider="clingen_cspec",
        primary_failure=primary_failure,
    )
    source.update(
        {
            "status": "partial",
            "provider": "Local CSpec last-known-good cache",
            "provider_role": provenance["provider_role"],
            "fallback_used": provenance["fallback_used"],
            "fallback_for": "clingen_cspec",
            "fallback_failure": None,
            "source": "cached_cspec",
            "source_type": "last_known_good_cache",
            "retrieved_at": entry["source_retrieved_at"],
            "source_retrieved_at": entry["source_retrieved_at"],
            "cache_stored_at": entry["cache_stored_at"],
            "fallback_used_at": _retrieval_timestamp(),
            "freshness_status": "last_known_good_age_unbounded",
            "upstream_sources": ["ClinGen CSpec Registry"],
            "specification_available": True,
            "specifications": specifications,
            "specification_count": len(specifications),
            "specifications_truncated": False,
        }
    )
    _set_cspec_applicability(
        source,
        status="cached_released_specification",
        message=(
            "Released ClinGen CSpec context was recovered from the exact "
            "last-known-good gene/disease cache after a live operational "
            "failure."
        ),
    )
    for specification in specifications:
        annotation["references"].append(
            {
                "source": "ClinGen CSpec Registry",
                "url": specification["specification_url"],
            }
        )
    annotation["warnings"].append(
        "Live ClinGen CSpec was operationally unavailable; cached "
        "last-known-good metadata was used with its original retrieval and "
        "cache dates. Cache age is not treated as current live evidence."
    )


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
    partial = statuses.count("partial")

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
    if partial:
        details.append(f"{partial} using validation/mapping fallback")
    if not_found:
        details.append(
            (
                f"{not_found} with no applicable specification for the "
                "current gene/disease scope"
                if source == "cspec"
                else f"{not_found} with no exact record"
            )
        )

    if details:
        return (
            "warning" if failed or unsupported or partial else "success",
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
    circuit_state: ProviderCircuitState | None = None,
    circuit_provider: str | None = None,
) -> int:
    """Automatically retry only failed variant/provider pairs."""

    failed_indices = [
        index
        for index, annotation in enumerate(annotations)
        if _source_failed(annotation, source)
    ]
    retry_rounds = 0

    for retry_index in range(max_retries):
        if not failed_indices or (
            circuit_state is not None
            and circuit_provider is not None
            and circuit_state.get(circuit_provider) is not None
        ):
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

        circuit_opened = False
        for position, index in enumerate(failed_indices):
            annotations[index] = retry_variant(index)
            if (
                circuit_state is not None
                and circuit_provider is not None
                and circuit_state.get(circuit_provider) is not None
            ):
                for remaining_index in failed_indices[position + 1:]:
                    annotations[remaining_index] = retry_variant(
                        remaining_index
                    )
                circuit_opened = True
                break

        if circuit_opened:
            break

        failed_indices = [
            index
            for index in failed_indices
            if _source_failed(annotations[index], source)
        ]

    return retry_rounds


def _annotate_vep_variant_once(
    variant: VariantData,
    session: requests.Session,
    max_retries: int,
    circuit_state: ProviderCircuitState,
    readiness_snapshot: ProviderReadinessSnapshot | None,
    confirmation_state: _ReadinessConfirmationState,
) -> AnnotationData:
    """Retry one failed VEP variant without restarting successful variants."""

    token = "cv_retry_0"
    try:
        responses = _post_vep_batch(
            session,
            [_to_vep_input(token, variant)],
            max_retries,
            circuit_state,
            readiness_snapshot,
            confirmation_state,
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
            vep_failure=(
                exc.provider_status
                if isinstance(exc, VepOperationalError)
                else None
            ),
            vep_failure_scope=(
                "variant_level"
                if isinstance(exc, VepVariantLevelError)
                else "provider_wide"
            ),
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


def _variantvalidator_match(
    payload: object,
    variant: VariantData,
) -> dict[str, Any] | None:
    """Return one exact assembly/allele VariantValidator mapping."""

    if not isinstance(payload, dict):
        raise ProviderInvalidResponseError()
    metadata = payload.get("metadata")
    flag = payload.get("flag")
    if not isinstance(metadata, dict) or not isinstance(flag, str):
        raise ProviderInvalidResponseError()

    assembly_key = settings.GENOME_ASSEMBLY.casefold()
    expected = {
        "chrom": _normalize_chromosome(variant["chrom"]),
        "pos": int(variant["pos"]),
        "ref": str(variant["ref"]).upper(),
        "alt": str(variant["alt"]).upper(),
    }
    matches: list[
        tuple[str, dict[str, Any], dict[str, Any], dict[str, object]]
    ] = []
    for key, value in payload.items():
        if key in {"flag", "metadata"} or not isinstance(value, dict):
            continue
        loci = value.get("primary_assembly_loci")
        if not isinstance(loci, dict):
            continue
        locus = loci.get(assembly_key)
        if not isinstance(locus, dict):
            continue
        vcf = locus.get("vcf")
        if not isinstance(vcf, dict):
            continue
        try:
            returned = {
                "chrom": _normalize_chromosome(vcf.get("chr")),
                "pos": int(vcf.get("pos")),
                "ref": str(vcf.get("ref")).upper(),
                "alt": str(vcf.get("alt")).upper(),
            }
        except (TypeError, ValueError):
            continue
        if returned == expected:
            matches.append((key, value, locus, returned))

    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            not bool(
                isinstance(item[1].get("annotations"), dict)
                and item[1]["annotations"].get("mane_select") is True
            ),
            item[0],
        )
    )
    key, record, locus, returned_variant = matches[0]
    gene_ids = record.get("gene_ids")
    gene_ids = gene_ids if isinstance(gene_ids, dict) else {}
    predicted = record.get("hgvs_predicted_protein_consequence")
    predicted = predicted if isinstance(predicted, dict) else {}
    transcript_hgvs = _optional_text(
        record.get("hgvs_transcript_variant")
    )
    validation_warnings = _bounded_string_list(
        record.get("validation_warnings"),
        10,
    )
    return {
        "provider_version": _optional_text(
            metadata.get("variantvalidator_version")
        ),
        "assembly": settings.GENOME_ASSEMBLY,
        "returned_variant": returned_variant,
        "selected_record": key,
        "gene": _optional_text(record.get("gene_symbol")),
        "gene_id": (
            _optional_text(gene_ids.get("ensembl_gene_id"))
            or _optional_text(gene_ids.get("hgnc_id"))
        ),
        "transcript": (
            transcript_hgvs.split(":", 1)[0]
            if transcript_hgvs and ":" in transcript_hgvs
            else None
        ),
        "genomic_hgvs": _optional_text(
            locus.get("hgvs_genomic_description")
        ),
        "transcript_hgvs": transcript_hgvs,
        "protein_hgvs": (
            _optional_text(predicted.get("tlr"))
            or _optional_text(predicted.get("slr"))
        ),
        "validation_warnings": validation_warnings,
    }


def _fetch_variantvalidator_fallback(
    session: requests.Session,
    variant: VariantData,
    circuit_state: ProviderCircuitState,
) -> tuple[ProviderStatus, dict[str, Any] | None, str]:
    """Validate one normalized allele with one bounded fallback call."""

    description = (
        f"{_normalize_chromosome(variant['chrom'])}:"
        f"{int(variant['pos'])}:"
        f"{str(variant['ref']).upper()}:"
        f"{str(variant['alt']).upper()}"
    )
    url = (
        f"{settings.VARIANTVALIDATOR_BASE_URL}"
        "/VariantValidator/variantvalidator/"
        f"{settings.GENOME_ASSEMBLY}/"
        f"{quote(description, safe='')}/"
        f"{VARIANTVALIDATOR_SELECT_TRANSCRIPTS}"
    )

    def request(
        timeout: tuple[float, float],
        _attempt: int,
    ) -> object:
        response = session.get(
            url,
            headers={"Accept": "application/json"},
            params={"content-type": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return response
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise ProviderInvalidResponseError(
                http_status=response.status_code
            ) from error
        if not isinstance(payload, dict):
            raise ProviderInvalidResponseError(
                http_status=response.status_code
            )
        return payload

    result = call_provider_with_policy(
        provider="variantvalidator",
        operation_name="validate_variant",
        operation=request,
        timeouts=ProviderTimeouts(
            connect=min(float(settings.VARIANTVALIDATOR_TIMEOUT), 5.0),
            read=float(settings.VARIANTVALIDATOR_TIMEOUT),
        ),
        retry_policy=ProviderRetryPolicy(max_attempts=1),
        circuit_state=circuit_state,
    )
    if result.status != "success":
        return result.status, None, url
    try:
        match = _variantvalidator_match(result.value, variant)
    except ProviderInvalidResponseError:
        circuit_state.open("variantvalidator", "invalid_response")
        return "invalid_response", None, url
    return ("success" if match is not None else "no_match"), match, url


def _ensembl_variation_overlap_match(
    payload: object,
    variant: VariantData,
) -> dict[str, Any] | None:
    """Return one exact assembly, coordinate, and allele overlap record."""

    if not isinstance(payload, list):
        raise ProviderInvalidResponseError()
    expected_chrom = _normalize_chromosome(variant["chrom"])
    expected_pos = int(variant["pos"])
    expected_ref = str(variant["ref"]).upper()
    expected_alt = str(variant["alt"]).upper()
    expected_end = expected_pos + len(expected_ref) - 1
    matches: list[dict[str, Any]] = []
    for record in payload:
        if not isinstance(record, dict):
            raise ProviderInvalidResponseError()
        alleles = record.get("alleles")
        normalized_alleles = (
            [value.upper() for value in alleles if isinstance(value, str)]
            if isinstance(alleles, list)
            else []
        )
        try:
            returned_chrom = _normalize_chromosome(
                record.get("seq_region_name")
            )
        except AnnotationError:
            continue
        if (
            record.get("assembly_name") == settings.GENOME_ASSEMBLY
            and returned_chrom == expected_chrom
            and record.get("start") == expected_pos
            and record.get("end") == expected_end
            and record.get("strand") == 1
            and normalized_alleles
            and normalized_alleles[0] == expected_ref
            and expected_alt in normalized_alleles[1:]
        ):
            matches.append(record)

    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("id", "")))
    selected = matches[0]
    identifier = _optional_text(selected.get("id"))
    if identifier is None:
        raise ProviderInvalidResponseError()
    return {
        "id": identifier,
        "source": _optional_text(selected.get("source")),
        "assembly": settings.GENOME_ASSEMBLY,
        "chrom": expected_chrom,
        "start": expected_pos,
        "end": expected_end,
        "alleles": [expected_ref, expected_alt],
        "consequence_type": _optional_text(
            selected.get("consequence_type")
        ),
        "clinical_significance": _bounded_string_list(
            selected.get("clinical_significance"),
            10,
        ),
    }


def _ensembl_variation_base_url() -> str:
    base_url = settings.ENSEMBL_VARIATION_BASE_URL.rstrip("/")
    if (
        settings.GENOME_ASSEMBLY == "GRCh37"
        and base_url == "https://rest.ensembl.org"
    ):
        return "https://grch37.rest.ensembl.org"
    return base_url


def _fetch_ensembl_variation_fallback(
    session: requests.Session,
    variant: VariantData,
    circuit_state: ProviderCircuitState,
) -> tuple[ProviderStatus, dict[str, Any] | None, str]:
    """Fetch bounded overlapping variant context for one normalized allele."""

    chrom = _normalize_chromosome(variant["chrom"])
    pos = int(variant["pos"])
    url = (
        f"{_ensembl_variation_base_url()}/overlap/region/human/"
        f"{quote(f'{chrom}:{pos}-{pos}', safe=':-')}"
    )

    def request(
        timeout: tuple[float, float],
        _attempt: int,
    ) -> object:
        response = session.get(
            url,
            headers={"Accept": "application/json"},
            params={"feature": "variation"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return response
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise ProviderInvalidResponseError(
                http_status=response.status_code
            ) from error
        if not isinstance(payload, list):
            raise ProviderInvalidResponseError(
                http_status=response.status_code
            )
        return payload

    result = call_provider_with_policy(
        provider="ensembl_variation",
        operation_name="overlap_variant",
        operation=request,
        timeouts=ProviderTimeouts(
            connect=min(float(settings.ENSEMBL_VARIATION_TIMEOUT), 5.0),
            read=float(settings.ENSEMBL_VARIATION_TIMEOUT),
        ),
        retry_policy=ProviderRetryPolicy(max_attempts=1),
        circuit_state=circuit_state,
    )
    if result.status != "success":
        return result.status, None, url
    try:
        match = _ensembl_variation_overlap_match(result.value, variant)
    except ProviderInvalidResponseError:
        circuit_state.open("ensembl_variation", "invalid_response")
        return "invalid_response", None, url
    return ("success" if match is not None else "no_match"), match, url


def _apply_myvariant_fallback(
    annotation: AnnotationData,
    session: requests.Session,
    circuit_state: ProviderCircuitState,
) -> None:
    """Preserve Ensembl overlap context after MyVariant operational failure."""

    source = annotation["sources"]["myvariant"]
    primary_failure = source.get("primary_failure")
    if not isinstance(primary_failure, str) or not should_trigger_fallback(
        primary_failure
    ):
        source["fallback_status"] = "not_eligible"
        return

    status, match, source_url = _fetch_ensembl_variation_fallback(
        session,
        annotation["variant"],
        circuit_state,
    )
    source["fallback_status"] = status
    source["fallback_source_url"] = source_url
    if status != "success" or match is None:
        source["fallback_failure"] = (
            status if should_trigger_fallback(status) else None
        )
        annotation["warnings"].append(
            (
                "Ensembl Variation returned no exact assembly, coordinate, "
                "and allele overlap; MyVariant.info fields remain unavailable."
                if status == "no_match"
                else "Ensembl Variation fallback was unavailable; "
                "MyVariant.info fields remain unavailable."
            )
        )
        return

    provenance = build_provider_provenance(
        capability="variant_context",
        provider="ensembl_variation",
        provider_role="fallback",
        primary_provider="myvariant",
        primary_failure=primary_failure,
    )
    source.update(
        {
            "status": "partial",
            "provider": ENSEMBL_VARIATION_PROVIDER_NAME,
            "provider_role": provenance["provider_role"],
            "fallback_used": provenance["fallback_used"],
            "fallback_for": "myvariant",
            "fallback_failure": None,
            "source_type": "overlapping_variant_context_fallback",
            "provider_version": None,
            "retrieved_at": _retrieval_timestamp(),
            "upstream_sources": [match["source"]] if match["source"] else [],
            "ensembl_variation": match,
        }
    )
    annotation["references"].append(
        {"source": ENSEMBL_VARIATION_PROVIDER_NAME, "url": source_url}
    )
    annotation["warnings"].append(
        "MyVariant.info was operationally unavailable; Ensembl Variation "
        "preserved exact overlapping variant context only. MyVariant.info "
        "aggregated gene, population, and ClinVar-derived fields remain "
        "unavailable."
    )


def _apply_vep_fallback(
    annotation: AnnotationData,
    session: requests.Session,
    circuit_state: ProviderCircuitState,
) -> None:
    """Preserve validation/mapping only after operational VEP failure."""

    source = annotation["sources"]["vep"]
    primary_failure = source.get("primary_failure")
    if not isinstance(primary_failure, str) or not should_trigger_fallback(
        primary_failure
    ):
        source["fallback_status"] = "not_eligible"
        return

    status, match, source_url = _fetch_variantvalidator_fallback(
        session,
        annotation["variant"],
        circuit_state,
    )
    source["fallback_status"] = status
    source["fallback_source_url"] = source_url
    if status != "success" or match is None:
        source["fallback_failure"] = (
            status if should_trigger_fallback(status) else None
        )
        annotation["warnings"].append(
            (
                "VariantValidator returned no exact assembly and allele "
                "mapping; normalized input identity was retained."
                if status == "no_match"
                else "VariantValidator fallback was unavailable; "
                "normalized input identity was retained."
            )
        )
        return

    source.update(
        {
            "status": "partial",
            "provider": VARIANTVALIDATOR_PROVIDER_NAME,
            "provider_role": "fallback",
            "fallback_used": True,
            "fallback_for": "ensembl_vep",
            "fallback_failure": None,
            "source_type": "validation_mapping_fallback",
            "provider_version": match["provider_version"],
            "retrieved_at": _retrieval_timestamp(),
            "validated_genomic_hgvs": match["genomic_hgvs"],
            "validated_variant": {
                "assembly": match["assembly"],
                **match["returned_variant"],
            },
            "validated_gene": match["gene"],
            "validated_gene_id": match["gene_id"],
            "validated_transcript": match["transcript"],
            "validated_transcript_hgvs": match["transcript_hgvs"],
            "validated_protein_hgvs": match["protein_hgvs"],
            "selected_record": match["selected_record"],
            "validation_warnings": match["validation_warnings"],
            "consequence_available": False,
            "upstream_sources": [VARIANTVALIDATOR_PROVIDER_NAME],
        }
    )
    for field in ("gene", "gene_id", "transcript"):
        if annotation.get(field) is None and match.get(field) is not None:
            annotation[field] = match[field]
    if annotation.get("hgvsc") is None:
        annotation["hgvsc"] = match["transcript_hgvs"]
    if annotation.get("hgvsp") is None:
        annotation["hgvsp"] = match["protein_hgvs"]
        annotation["protein_change"] = match["protein_hgvs"]
    annotation["warnings"].append(
        "Ensembl VEP was operationally unavailable; VariantValidator "
        "preserved exact allele validation and HGVS mapping only. "
        "Consequence, impact, transcript-consequence, and VEP plugin "
        "fields remain unavailable."
    )


def _verify_incomplete_vep_gene_context(
    annotation: AnnotationData,
    session: requests.Session,
    circuit_state: ProviderCircuitState,
) -> None:
    """Verify an incomplete but operationally successful VEP annotation."""

    vep = annotation["sources"]["vep"]
    if (
        vep.get("status") != "success"
        or vep.get("provider") != VEP_PROVIDER_NAME
        or vep_gene_context_is_complete(annotation)
    ):
        return

    status, match, source_url = _fetch_variantvalidator_fallback(
        session,
        annotation["variant"],
        circuit_state,
    )
    verification: dict[str, Any] = {
        "status": status,
        "provider": VARIANTVALIDATOR_PROVIDER_NAME,
        "provider_role": "verification",
        "trigger": VEP_GENE_CONTEXT_UNRESOLVED,
        "provider_version": None,
        "retrieved_at": _retrieval_timestamp(),
        "validated_variant": None,
        "validated_genomic_hgvs": None,
        "validated_gene": None,
        "validated_gene_id": None,
        "validated_transcript": None,
        "validated_transcript_hgvs": None,
        "validated_protein_hgvs": None,
        "selected_record": None,
        "validation_warnings": [],
    }
    if status == "success" and match is not None:
        verification.update(
            {
                "provider_version": match["provider_version"],
                "validated_variant": {
                    "assembly": match["assembly"],
                    **match["returned_variant"],
                },
                "validated_genomic_hgvs": match["genomic_hgvs"],
                "validated_gene": match["gene"],
                "validated_gene_id": match["gene_id"],
                "validated_transcript": match["transcript"],
                "validated_transcript_hgvs": match["transcript_hgvs"],
                "validated_protein_hgvs": match["protein_hgvs"],
                "selected_record": match["selected_record"],
                "validation_warnings": match["validation_warnings"],
            }
        )
        annotation["references"].append(
            {
                "source": VARIANTVALIDATOR_PROVIDER_NAME,
                "url": source_url,
            }
        )
    else:
        annotation["warnings"].append(
            (
                "VariantValidator found no exact assembly and allele match "
                "while verifying incomplete VEP gene context."
                if status == "no_match"
                else "VariantValidator could not verify incomplete VEP gene "
                "context."
            )
        )
    vep["gene_identity_verification"] = verification


def _resolve_annotation_gene_identity(annotation: AnnotationData) -> None:
    resolution = resolve_gene_identity(annotation)
    annotation["gene_identity_resolution"] = resolution
    if resolution["status"] == "unresolved":
        annotation["warnings"].append(
            "Gene identity could not be resolved from an exact, "
            "provenance-backed annotation source."
        )
        LOGGER.warning(
            "event=gene_identity_resolution status=unresolved reason=%s",
            GENE_IDENTITY_UNRESOLVED,
        )


def _identifier_reference(
    identifier_type: str,
    value: object,
) -> IdentifierReference | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return {"type": identifier_type, "value": value.strip()}


def _bundle_references(
    bundle: VariantIdentifierBundle,
    field: str,
    identifier_type: str,
) -> list[IdentifierReference]:
    values = bundle.get(field)
    if not isinstance(values, list):
        return []
    return [
        reference
        for value in values
        if (
            reference := _identifier_reference(
                identifier_type,
                value,
            )
        )
    ]


def _record_retrieval_assessments(
    annotation: AnnotationData,
) -> None:
    """Classify sparse provider outcomes without generic no-match loss."""

    raw_bundle = annotation.get("identifier_bundle")
    if not isinstance(raw_bundle, dict):
        raise AnnotationError("Identifier bundle is missing.")
    bundle: VariantIdentifierBundle = raw_bundle  # type: ignore[assignment]
    sources = annotation["sources"]
    clinvar_source = sources["clinvar"]
    raw_clinvar_used = clinvar_source.get("identifiers_used")
    clinvar_used: list[IdentifierReference] = (
        [
            {"type": item["type"], "value": item["value"]}
            for item in raw_clinvar_used
            if isinstance(item, dict)
            and isinstance(item.get("type"), str)
            and isinstance(item.get("value"), str)
        ]
        if isinstance(raw_clinvar_used, list)
        else []
    )
    clinvar_eligible = [
        *[
            item
            for item in [
                _identifier_reference(
                    "genomic_hgvs",
                    clinvar_source.get("query_hgvs"),
                )
            ]
            if item is not None
        ],
        *_bundle_references(bundle, "transcript_hgvs", "transcript_hgvs"),
        *_bundle_references(bundle, "rsids", "rsid"),
        *_bundle_references(
            bundle,
            "clinvar_variation_ids",
            "clinvar_variation_id",
        ),
        *_bundle_references(bundle, "vcv_accessions", "vcv_accession"),
        *_bundle_references(bundle, "rcv_accessions", "rcv_accession"),
    ]
    clinvar_unused = [
        item for item in clinvar_eligible if item not in clinvar_used
    ]
    raw_rejections = clinvar_source.get("candidate_rejections")
    rejection_reasons = {
        reason
        for rejection in (
            raw_rejections if isinstance(raw_rejections, list) else []
        )
        if isinstance(rejection, dict)
        for reason in rejection.get("reason_codes", [])
        if isinstance(reason, str)
    }
    allele_value = (
        f"{bundle['genome_build']}:{bundle['chromosome']}:"
        f"{bundle['position']}:{bundle['reference']}:"
        f"{bundle['alternate']}"
    )

    definitions = {
        "vep": {
            "provider": "ensembl_vep",
            "strategy": "normalized_genomic_allele",
            "used": [
                {
                    "type": "normalized_allele",
                    "value": allele_value,
                }
            ],
            "unused": [],
        },
        "myvariant": {
            "provider": "myvariant",
            "strategy": "normalized_genomic_hgvs",
            "used": [
                item
                for item in [
                    _identifier_reference(
                        "genomic_hgvs",
                        sources["myvariant"].get("variant_id"),
                    )
                ]
                if item is not None
            ],
            "unused": _bundle_references(
                bundle,
                "transcript_hgvs",
                "transcript_hgvs",
            ),
        },
        "clinvar": {
            "provider": "ncbi_clinvar",
            "strategy": "deterministic_identifier_hierarchy",
            "used": clinvar_used,
            "unused": clinvar_unused,
            "candidates_returned": clinvar_source.get("candidate_count"),
            "candidates_rejected": clinvar_source.get(
                "candidates_rejected"
            ),
            "normalization_mismatch": bool(
                rejection_reasons
                & {
                    "assembly_mismatch",
                    "coordinate_mismatch",
                    "ref_mismatch",
                    "alt_mismatch",
                    "representation_unresolved",
                }
            ),
            "source_absence_confirmed": bool(
                clinvar_source.get("query_strategies")
                and not clinvar_unused
                and clinvar_source.get("candidate_count") == 0
            ),
        },
        "clingen": {
            "provider": "ucsc_gencc",
            "strategy": "locus_then_gene_symbol",
            "used": [
                item
                for item in [
                    _identifier_reference(
                        "gene_symbol",
                        sources["clingen"].get("query_gene"),
                    )
                ]
                if item is not None
            ],
            "unused": [],
        },
        "cspec": {
            "provider": "clingen_cspec",
            "strategy": "gene_then_disease_scope",
            "used": [
                item
                for item in [
                    _identifier_reference(
                        "gene_symbol",
                        sources["cspec"].get("query_gene"),
                    ),
                    *[
                        _identifier_reference("mondo_id", value)
                        for value in sources["cspec"].get(
                            "query_disease_ids",
                            [],
                        )
                    ],
                ]
                if item is not None
            ],
            "unused": [],
        },
    }

    for source_name, definition in definitions.items():
        source = sources[source_name]
        raw_status = source.get("status")
        if raw_status in {"pending", "success", "partial"}:
            source.pop("retrieval_assessment", None)
            continue
        status = "no_match" if raw_status == "not_found" else str(raw_status)
        operational_failure = raw_status in {
            "error",
            "unavailable",
            "invalid_response",
        }
        normalization_mismatch = (
            raw_status == "unsupported"
            or bool(definition.get("normalization_mismatch"))
        )
        identifier_gap = raw_status == "not_applicable"
        source_absence_confirmed = (
            bool(definition.get("source_absence_confirmed"))
            or (
                source_name == "cspec"
                and raw_status == "not_found"
                and source.get("no_match_reason") in {
                    "gene_entity_absent",
                    "gene_present_no_linked_specifications",
                    "gene_present_only_unreleased_specifications",
                }
            )
        )
        source["retrieval_assessment"] = build_retrieval_assessment(
            provider=definition["provider"],
            status=status,
            query_strategy=definition["strategy"],
            identifiers_used=definition["used"],
            unused_eligible_identifiers=definition["unused"],
            operational_failure=operational_failure,
            identifier_gap=identifier_gap,
            normalization_mismatch=normalization_mismatch,
            source_absence_confirmed=source_absence_confirmed,
            candidates_returned=definition.get("candidates_returned"),
            candidates_rejected=definition.get("candidates_rejected"),
        )


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
        settings.VARIANTVALIDATOR_BASE_URL,
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
        source.get("status") not in {
            "success", "not_found", "not_applicable",
        }
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
    readiness_snapshot: ProviderReadinessSnapshot | None = None,
) -> list[AnnotationData]:
    """Annotate with VEP, GeneBe, MyVariant, ClinVar, ClinGen, and CSpec evidence.

    Each external source fails independently and raw source payloads are not
    retained in the returned standardized evidence.
    """
    if isinstance(variants, (str, bytes, dict)):
        raise AnnotationError("variants must be an iterable of dictionaries.")
    if readiness_snapshot is not None and not isinstance(
        readiness_snapshot,
        ProviderReadinessSnapshot,
    ):
        raise AnnotationError(
            "readiness_snapshot must be a ProviderReadinessSnapshot."
        )
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
    vep_circuit = ProviderCircuitState()
    vep_confirmation_state = _ReadinessConfirmationState()
    vep_provider_wide_failure = False

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
                    vep_circuit,
                    readiness_snapshot,
                    vep_confirmation_state,
                )
            except AnnotationServiceError as exc:
                variant_level = isinstance(exc, VepVariantLevelError)
                vep_provider_wide_failure = (
                    vep_provider_wide_failure or not variant_level
                )
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
                        vep_failure=(
                            None
                            if variant_level
                            else (
                                exc.provider_status
                                if isinstance(exc, VepOperationalError)
                                else "unavailable"
                            )
                        ),
                        vep_failure_scope=(
                            "variant_level"
                            if variant_level
                            else "provider_wide"
                        ),
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

        vep_retry_rounds = 0
        if not vep_provider_wide_failure:
            vep_retry_rounds = _retry_failed_source_annotations(
                annotations,
                source="vep",
                max_retries=resolved_retries,
                retry_variant=lambda index: _annotate_vep_variant_once(
                    annotations[index]["variant"],
                    active_session,
                    resolved_retries,
                    vep_circuit,
                    readiness_snapshot,
                    vep_confirmation_state,
                ),
                progress_callback=progress_callback,
                circuit_state=vep_circuit,
                circuit_provider="vep",
            )
        variantvalidator_circuit = ProviderCircuitState()
        for annotation in annotations:
            if _source_failed(annotation, "vep"):
                _apply_vep_fallback(
                    annotation,
                    active_session,
                    variantvalidator_circuit,
                )
            _verify_incomplete_vep_gene_context(
                annotation,
                active_session,
                variantvalidator_circuit,
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
        _refresh_identifier_bundles(annotations, initialize=True)
        _notify_annotation_progress(
            progress_callback,
            "genebe",
            "running",
            "Sending variants to GeneBe.",
        )
        genebe_baselines = deepcopy(annotations)
        genebe_circuit = ProviderCircuitState()
        genebe_confirmation_state = _ReadinessConfirmationState()
        genebe_provider_wide_failure = _annotate_with_genebe(
            annotations,
            active_session,
            resolved_retries,
            genebe_circuit,
            readiness_snapshot,
            genebe_confirmation_state,
        )
        genebe_retry_rounds = 0
        if not genebe_provider_wide_failure:
            genebe_retry_rounds = _retry_failed_source_annotations(
                annotations,
                source="genebe",
                max_retries=resolved_retries,
                retry_variant=lambda index: _retry_annotation_copy(
                    genebe_baselines[index],
                    lambda candidate: _annotate_with_genebe(
                        [candidate],
                        active_session,
                        resolved_retries,
                        genebe_circuit,
                        readiness_snapshot,
                        genebe_confirmation_state,
                    ),
                ),
                progress_callback=progress_callback,
                circuit_state=genebe_circuit,
                circuit_provider="genebe",
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
        ensembl_variation_circuit = ProviderCircuitState()
        for annotation in annotations:
            if _source_failed(annotation, "myvariant"):
                _apply_myvariant_fallback(
                    annotation,
                    active_session,
                    ensembl_variation_circuit,
                )
            _apply_ucsc_normal_population_frequency_fallback(
                annotation,
                active_session,
            )
        _refresh_identifier_bundles(annotations)
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

        for annotation in annotations:
            _resolve_annotation_gene_identity(annotation)

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
        _refresh_identifier_bundles(annotations)
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
        for annotation in annotations:
            if _source_failed(annotation, "cspec"):
                _apply_cspec_lkg_fallback(annotation)
        _refresh_identifier_bundles(annotations)
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

        _notify_annotation_progress(
            progress_callback,
            "erepo",
            "running",
            "Querying ClinGen ERepo exact variant context.",
        )
        erepo_circuit = ProviderCircuitState()
        for annotation in annotations:
            annotation["sources"]["erepo"] = retrieve_expert_curated_context(
                annotation["identifier_bundle"],
                session=active_session,
                circuit_state=erepo_circuit,
                enabled=settings.ENABLE_EREPO,
            )
        erepo_status, erepo_message = _source_progress_summary(
            annotations,
            "erepo",
        )
        _notify_annotation_progress(
            progress_callback,
            "erepo",
            erepo_status,
            erepo_message,
        )
        for annotation in annotations:
            _record_retrieval_assessments(annotation)
    finally:
        if owns_session:
            active_session.close()

    if resolved_use_cache:
        _annotation_cache_put(cache_key, annotations)
    return annotations
