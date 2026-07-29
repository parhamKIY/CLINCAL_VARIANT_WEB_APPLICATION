"""External variant annotation and evidence standardization."""

import logging
import time
from collections.abc import Iterable, Iterator
from typing import Any

import requests

from backend.vcf_processing import VariantData
from config import settings


LOGGER = logging.getLogger(__name__)

# Ensembl documents a maximum of 200 variants per POST request.
MAX_VEP_BATCH_SIZE = 200
MAX_STORED_TRANSCRIPTS = 10
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
REQUIRED_VARIANT_FIELDS = {"chrom", "pos", "ref", "alt"}
IMPACT_PRIORITY = {
    "HIGH": 4,
    "MODERATE": 3,
    "LOW": 2,
    "MODIFIER": 1,
}

AnnotationData = dict[str, Any]


class AnnotationError(ValueError):
    """Raised when annotation input or configuration is invalid."""


class AnnotationServiceError(RuntimeError):
    """Raised when an external annotation service cannot be used."""


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

        try:
            response = session.post(
                endpoint,
                headers=headers,
                params=params,
                json={"variants": vep_inputs},
                timeout=settings.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    f"Ensembl VEP request failed: {exc}"
                ) from exc

            LOGGER.warning(
                "Retrying Ensembl VEP after a connection error."
            )
            time.sleep(_retry_delay(attempt))
            continue

        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                LOGGER.warning(
                    "Retrying Ensembl VEP after HTTP %s.",
                    response.status_code,
                )
                time.sleep(_retry_delay(attempt, response))
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


def _clean_transcript(
    transcript: dict[str, Any],
) -> dict[str, Any]:
    """Keep only transcript evidence used by later pipeline stages."""
    consequence_terms = transcript.get("consequence_terms")
    if not isinstance(consequence_terms, list):
        consequence_terms = []

    return {
        "gene_symbol": transcript.get("gene_symbol"),
        "gene_id": transcript.get("gene_id"),
        "transcript_id": transcript.get("transcript_id"),
        "biotype": transcript.get("biotype"),
        "consequence_terms": consequence_terms,
        "impact": transcript.get("impact"),
        "hgvsc": transcript.get("hgvsc"),
        "hgvsp": transcript.get("hgvsp"),
        "canonical": transcript.get("canonical") == 1,
        "mane_select": transcript.get("mane_select"),
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
        "protein_change": None,
        "sources": {
            "vep": {
                "status": status,
                "most_severe_consequence": None,
                "transcript_consequences": [],
                "total_transcript_consequences": 0,
                "transcripts_truncated": False,
            }
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
            "protein_change": representative.get("hgvsp"),
        }
    )
    return annotation


# ---------------------------------------------------------------------------
# Public annotation entry point
# ---------------------------------------------------------------------------

def annotate_variants(
    variants: Iterable[VariantData],
    *,
    batch_size: int | None = None,
    max_retries: int | None = None,
    session: requests.Session | None = None,
) -> list[AnnotationData]:
    """Annotate variants with Ensembl VEP without exposing raw API output.

    A failed batch produces structured error annotations instead of stopping
    the complete pipeline. The same output structure can later receive
    MyVariant, ClinVar, and ClinGen evidence under the ``sources`` field.
    """
    resolved_batch_size = _resolve_batch_size(batch_size)
    resolved_retries = _resolve_retries(max_retries)
    active_session = session or requests.Session()
    owns_session = session is None
    annotations: list[AnnotationData] = []

    try:
        for batch in _iter_batches(variants, resolved_batch_size):
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
                LOGGER.error("Ensembl VEP batch failed: %s", exc)
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
    finally:
        if owns_session:
            active_session.close()

    return annotations
