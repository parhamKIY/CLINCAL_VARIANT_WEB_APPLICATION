"""Bounded GRCh38 sequence identity access with operational fallback only."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, MutableMapping
from copy import deepcopy
from threading import Lock
from typing import Literal, TypedDict, cast

import requests

from backend.vcf_processing import normalize_primary_chromosome
from backend.variant_identity import (
    CLINVAR_REFSEQ_BASES,
    CLINVAR_REFSEQ_VERSIONS,
)
from config import settings


ReferenceLookupStatus = Literal["success", "unavailable"]
ReferenceFailureReason = Literal[
    "REFERENCE_LOOKUP_UNAVAILABLE",
    "REFERENCE_RESPONSE_INVALID",
    "REFERENCE_SOURCE_CONFLICT",
]
ReferenceProviderStatus = Literal[
    "success", "unavailable", "response_invalid"
]
ReferenceProvider = Literal[
    "ensembl_grch38_sequence",
    "ucsc_hg38_sequence",
    "ncbi_refseq_grch38_sequence",
]
REFERENCE_SEQUENCE_MAX_ATTEMPTS = 2
REFERENCE_SEQUENCE_TIMEOUT_SECONDS = 5
NCBI_REFERENCE_REQUEST_INTERVAL_SECONDS = 0.34
_NCBI_REFERENCE_LOCK = Lock()
_LAST_NCBI_REFERENCE_REQUEST_AT = 0.0


class ReferenceProviderAttempt(TypedDict):
    source: ReferenceProvider
    status: ReferenceProviderStatus
    attempt_count: int
    request_provenance: str


class ReferenceSequenceResult(TypedDict):
    status: ReferenceLookupStatus
    assembly: str
    chrom: str
    start: int
    end: int
    sequence: str | None
    source: str
    fallback_used: bool
    failure_reason: ReferenceFailureReason | None
    provider_attempts: list[ReferenceProviderAttempt]


ReferenceFetcher = Callable[..., Mapping[str, object]]
ReferenceSequenceCache = MutableMapping[
    tuple[str, str, int, int], Mapping[str, object]
]


def ucsc_interval_from_internal(
    *, chrom: str, start: int, end: int
) -> tuple[str, int, int]:
    """Translate internal 1-based-inclusive coordinates to UCSC's contract.

    UCSC ``getData/sequence`` receives a zero-relative start and a one-relative
    end, so the end of an internal inclusive interval is unchanged.
    """

    normalized_chrom = normalize_primary_chromosome(chrom)
    if start < 1 or end < start:
        raise ValueError("Internal reference interval is invalid.")
    return f"chr{normalized_chrom}", start - 1, end


def _valid_sequence(value: object, length: int) -> str | None:
    if not isinstance(value, str):
        return None
    sequence = value.strip().upper()
    if len(sequence) != length or set(sequence) - {"A", "C", "G", "T"}:
        return None
    return sequence


def _wait_for_ncbi_reference_slot() -> None:
    """Keep reference E-utility traffic below three requests per second."""

    global _LAST_NCBI_REFERENCE_REQUEST_AT
    with _NCBI_REFERENCE_LOCK:
        elapsed = time.monotonic() - _LAST_NCBI_REFERENCE_REQUEST_AT
        remaining = NCBI_REFERENCE_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
        _LAST_NCBI_REFERENCE_REQUEST_AT = time.monotonic()


def _attempt(
    source: ReferenceProvider,
    status: ReferenceProviderStatus,
    attempt_count: int,
    request_provenance: str,
) -> ReferenceProviderAttempt:
    return {
        "source": source,
        "status": status,
        "attempt_count": attempt_count,
        "request_provenance": request_provenance,
    }


def _request_ensembl_sequence(
    *,
    chrom: str,
    start: int,
    end: int,
    session: requests.Session,
) -> tuple[str | None, ReferenceProviderAttempt]:
    """Request Ensembl with the same inclusive region string as the caller."""

    url = (
        f"{settings.VEP_BASE_URL}/sequence/region/human/"
        f"{chrom}:{start}..{end}:1"
    )
    attempts = 0
    for attempts in range(1, REFERENCE_SEQUENCE_MAX_ATTEMPTS + 1):
        try:
            response = session.get(
                url,
                params={"coord_system_version": "GRCh38"},
                headers={"Content-Type": "text/plain"},
                timeout=min(
                    settings.VEP_TIMEOUT,
                    REFERENCE_SEQUENCE_TIMEOUT_SECONDS,
                ),
            )
        except requests.RequestException:
            continue
        if response.status_code in {429, 500, 502, 503, 504}:
            continue
        if response.status_code != 200:
            break
        sequence = _valid_sequence(response.text, end - start + 1)
        if sequence is not None:
            return sequence, _attempt(
                "ensembl_grch38_sequence",
                "success",
                attempts,
                "ensembl_region_1based_inclusive",
            )
        return None, _attempt(
            "ensembl_grch38_sequence",
            "response_invalid",
            attempts,
            "ensembl_region_1based_inclusive",
        )
    return None, _attempt(
        "ensembl_grch38_sequence",
        "unavailable",
        attempts,
        "ensembl_region_1based_inclusive",
    )


def _request_ucsc_sequence(
    *,
    chrom: str,
    start: int,
    end: int,
    session: requests.Session,
) -> tuple[str | None, ReferenceProviderAttempt]:
    """Request UCSC hg38 after explicit coordinate translation."""

    ucsc_chrom, ucsc_start, ucsc_end = ucsc_interval_from_internal(
        chrom=chrom,
        start=start,
        end=end,
    )
    attempts = 0
    for attempts in range(1, settings.UCSC_SEQUENCE_MAX_RETRIES + 2):
        try:
            response = session.get(
                f"{settings.UCSC_SEQUENCE_BASE_URL}/getData/sequence",
                params={
                    "genome": "hg38",
                    "chrom": ucsc_chrom,
                    "start": ucsc_start,
                    "end": ucsc_end,
                },
                headers={"Accept": "application/json"},
                timeout=min(
                    settings.UCSC_SEQUENCE_TIMEOUT,
                    REFERENCE_SEQUENCE_TIMEOUT_SECONDS,
                ),
            )
        except requests.RequestException:
            continue
        if response.status_code in {429, 500, 502, 503, 504}:
            continue
        if response.status_code != 200:
            break
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        sequence = _valid_sequence(
            payload.get("dna") if isinstance(payload, Mapping) else None,
            end - start + 1,
        )
        if sequence is not None:
            return sequence, _attempt(
                "ucsc_hg38_sequence",
                "success",
                attempts,
                "ucsc_hg38_start_0based_end_1based",
            )
        return None, _attempt(
            "ucsc_hg38_sequence",
            "response_invalid",
            attempts,
            "ucsc_hg38_start_0based_end_1based",
        )
    return None, _attempt(
        "ucsc_hg38_sequence",
        "unavailable",
        attempts,
        "ucsc_hg38_start_0based_end_1based",
    )


def _request_ncbi_sequence(
    *,
    chrom: str,
    start: int,
    end: int,
    session: requests.Session,
) -> tuple[str | None, ReferenceProviderAttempt]:
    """Request the exact GRCh38 RefSeq interval from NCBI E-utilities."""

    accession_base = CLINVAR_REFSEQ_BASES[chrom]
    accession_version = CLINVAR_REFSEQ_VERSIONS["GRCh38"][chrom]
    accession = f"{accession_base}.{accession_version}"
    request_provenance = f"ncbi_refseq_{accession}_1based_inclusive"
    attempts = 0
    for attempts in range(1, settings.NCBI_SEQUENCE_MAX_RETRIES + 2):
        _wait_for_ncbi_reference_slot()
        try:
            response = session.get(
                f"{settings.NCBI_SEQUENCE_BASE_URL}/efetch.fcgi",
                params={
                    "db": "nuccore",
                    "id": accession,
                    "seq_start": start,
                    "seq_stop": end,
                    "rettype": "fasta",
                    "retmode": "text",
                    "strand": 1,
                },
                headers={"Accept": "text/plain"},
                timeout=min(
                    settings.NCBI_SEQUENCE_TIMEOUT,
                    REFERENCE_SEQUENCE_TIMEOUT_SECONDS,
                ),
            )
        except requests.RequestException:
            continue
        if response.status_code in {429, 500, 502, 503, 504}:
            continue
        if response.status_code != 200:
            break
        sequence_text = "".join(
            line.strip()
            for line in response.text.splitlines()
            if line.strip() and not line.lstrip().startswith(">")
        )
        sequence = _valid_sequence(sequence_text, end - start + 1)
        if sequence is not None:
            return sequence, _attempt(
                "ncbi_refseq_grch38_sequence",
                "success",
                attempts,
                request_provenance,
            )
        return None, _attempt(
            "ncbi_refseq_grch38_sequence",
            "response_invalid",
            attempts,
            request_provenance,
        )
    return None, _attempt(
        "ncbi_refseq_grch38_sequence",
        "unavailable",
        attempts,
        request_provenance,
    )


def _unavailable_result(
    *,
    assembly: str,
    chrom: str,
    start: int,
    end: int,
    attempts: list[ReferenceProviderAttempt],
) -> ReferenceSequenceResult:
    failure_reason: ReferenceFailureReason = (
        "REFERENCE_RESPONSE_INVALID"
        if any(item["status"] == "response_invalid" for item in attempts)
        else "REFERENCE_LOOKUP_UNAVAILABLE"
    )
    return {
        "status": "unavailable",
        "assembly": assembly,
        "chrom": chrom,
        "start": start,
        "end": end,
        "sequence": None,
        "source": "reference_sequence_unavailable",
        "fallback_used": len(attempts) > 1,
        "failure_reason": failure_reason,
        "provider_attempts": attempts,
    }


def fetch_grch38_reference_sequence(
    *,
    assembly: str,
    chrom: str,
    start: int,
    end: int,
    session: requests.Session | None = None,
    ucsc_session: requests.Session | None = None,
    ncbi_session: requests.Session | None = None,
) -> ReferenceSequenceResult:
    """Fetch one exact GRCh38 interval through the bounded provider chain.

    The internal request is always ``(assembly, chrom, start, end)`` with
    1-based inclusive coordinates. Provider coordinate/accession translation
    is contained here; callers never receive provider-specific coordinates or
    raw response bodies.
    """

    normalized_chrom = normalize_primary_chromosome(chrom)
    if assembly != "GRCh38" or start < 1 or end < start:
        return _unavailable_result(
            assembly=assembly,
            chrom=normalized_chrom,
            start=start,
            end=end,
            attempts=[],
        )
    primary_session = session or requests.Session()
    sequence, primary_attempt = _request_ensembl_sequence(
        chrom=normalized_chrom,
        start=start,
        end=end,
        session=primary_session,
    )
    if sequence is not None:
        return {
            "status": "success",
            "assembly": "GRCh38",
            "chrom": normalized_chrom,
            "start": start,
            "end": end,
            "sequence": sequence,
            "source": "ensembl_grch38_sequence",
            "fallback_used": False,
            "failure_reason": None,
            "provider_attempts": [primary_attempt],
        }
    attempts = [primary_attempt]
    if settings.ENABLE_UCSC_SEQUENCE_FALLBACK:
        fallback_session = ucsc_session or session or requests.Session()
        sequence, fallback_attempt = _request_ucsc_sequence(
            chrom=normalized_chrom,
            start=start,
            end=end,
            session=fallback_session,
        )
        attempts.append(fallback_attempt)
        if sequence is not None:
            return {
                "status": "success",
                "assembly": "GRCh38",
                "chrom": normalized_chrom,
                "start": start,
                "end": end,
                "sequence": sequence,
                "source": "ucsc_hg38_sequence",
                "fallback_used": True,
                "failure_reason": None,
                "provider_attempts": attempts,
            }
    if settings.ENABLE_NCBI_SEQUENCE_FALLBACK:
        final_session = (
            ncbi_session or ucsc_session or session or requests.Session()
        )
        sequence, final_attempt = _request_ncbi_sequence(
            chrom=normalized_chrom,
            start=start,
            end=end,
            session=final_session,
        )
        attempts.append(final_attempt)
        if sequence is not None:
            return {
                "status": "success",
                "assembly": "GRCh38",
                "chrom": normalized_chrom,
                "start": start,
                "end": end,
                "sequence": sequence,
                "source": "ncbi_refseq_grch38_sequence",
                "fallback_used": True,
                "failure_reason": None,
                "provider_attempts": attempts,
            }
    return _unavailable_result(
        assembly="GRCh38",
        chrom=normalized_chrom,
        start=start,
        end=end,
        attempts=attempts,
    )


def compare_reference_sequence_results(
    primary: Mapping[str, object],
    secondary: Mapping[str, object],
) -> ReferenceSequenceResult:
    """Fail closed if two independently obtained exact contexts disagree.

    This lower-level guard is intentionally not used to double-query production
    requests: a valid Ensembl result always short-circuits UCSC.
    """

    assembly = str(primary.get("assembly", ""))
    chrom = str(primary.get("chrom", ""))
    start = primary.get("start")
    end = primary.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return _unavailable_result(
            assembly=assembly,
            chrom=chrom,
            start=0,
            end=0,
            attempts=[],
        )
    primary_sequence = reference_sequence_is_success(
        primary,
        assembly=assembly,
        chrom=chrom,
        start=start,
        end=end,
    )
    secondary_sequence = reference_sequence_is_success(
        secondary,
        assembly=assembly,
        chrom=chrom,
        start=start,
        end=end,
    )
    if (
        primary_sequence is not None
        and secondary_sequence is not None
        and primary_sequence != secondary_sequence
    ):
        return {
            "status": "unavailable",
            "assembly": assembly,
            "chrom": chrom,
            "start": start,
            "end": end,
            "sequence": None,
            "source": "reference_sequence_conflict",
            "fallback_used": False,
            "failure_reason": "REFERENCE_SOURCE_CONFLICT",
            "provider_attempts": [],
        }
    return cast(ReferenceSequenceResult, deepcopy(dict(primary)))


def fetch_reference_sequence_cached(
    *,
    assembly: str,
    chrom: str,
    start: int,
    end: int,
    cache: ReferenceSequenceCache,
    fetcher: ReferenceFetcher = fetch_grch38_reference_sequence,
) -> Mapping[str, object]:
    """Cache one reference result only for the current analysis request."""

    normalized_chrom = normalize_primary_chromosome(chrom)
    key = (assembly, normalized_chrom, start, end)
    if key not in cache:
        cache[key] = dict(
            fetcher(
                assembly=assembly,
                chrom=normalized_chrom,
                start=start,
                end=end,
            )
        )
    return deepcopy(cache[key])


def reference_failure_reason(value: Mapping[str, object]) -> str:
    """Map a provider result to the Stage 2 input-level failure vocabulary."""

    reason = value.get("failure_reason")
    if reason in {
        "REFERENCE_RESPONSE_INVALID",
        "REFERENCE_SOURCE_CONFLICT",
    }:
        return cast(str, reason)
    return "REFERENCE_LOOKUP_UNAVAILABLE"


def reference_verification_provenance(
    value: Mapping[str, object],
    *,
    outcome: str,
) -> str:
    """Return bounded reviewer-safe reference-route provenance."""

    source = value.get("source")
    if source == "ncbi_refseq_grch38_sequence":
        route = "ncbi_refseq_fallback"
    elif source == "ucsc_hg38_sequence":
        route = "ucsc_fallback"
    elif source == "ensembl_grch38_sequence":
        route = "ensembl_primary"
    elif value.get("fallback_used") is True:
        attempted_sources = {
            item.get("source")
            for item in value.get("provider_attempts", [])
            if isinstance(item, Mapping)
        }
        route = (
            "ensembl_then_ucsc_then_ncbi_refseq"
            if "ncbi_refseq_grch38_sequence" in attempted_sources
            else "ensembl_then_ucsc"
        )
    else:
        route = "ensembl_primary"
    return f"{outcome}_{route}_grch38"


def reference_sequence_is_success(
    value: Mapping[str, object],
    *,
    assembly: str,
    chrom: str,
    start: int,
    end: int,
) -> str | None:
    """Accept only an exact accessor result for the requested interval."""

    sequence = value.get("sequence")
    if (
        value.get("status") != "success"
        or value.get("assembly") != assembly
        or value.get("chrom") != chrom
        or value.get("start") != start
        or value.get("end") != end
    ):
        return None
    return _valid_sequence(sequence, end - start + 1)


__all__ = [
    "REFERENCE_SEQUENCE_MAX_ATTEMPTS",
    "REFERENCE_SEQUENCE_TIMEOUT_SECONDS",
    "ReferenceSequenceCache",
    "ReferenceSequenceResult",
    "compare_reference_sequence_results",
    "fetch_grch38_reference_sequence",
    "fetch_reference_sequence_cached",
    "reference_failure_reason",
    "reference_sequence_is_success",
    "reference_verification_provenance",
    "ucsc_interval_from_internal",
]
