"""Narrow GRCh38 reference-sequence access for input identity proof."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypedDict

import requests

from backend.vcf_processing import normalize_primary_chromosome
from config import settings


ReferenceLookupStatus = Literal["success", "unavailable"]
REFERENCE_SEQUENCE_MAX_ATTEMPTS = 2
REFERENCE_SEQUENCE_TIMEOUT_SECONDS = 5


class ReferenceSequenceResult(TypedDict):
    status: ReferenceLookupStatus
    assembly: str
    chrom: str
    start: int
    end: int
    sequence: str | None
    source: str


def fetch_grch38_reference_sequence(
    *,
    assembly: str,
    chrom: str,
    start: int,
    end: int,
    session: requests.Session | None = None,
) -> ReferenceSequenceResult:
    """Return one exact GRCh38 interval or an explicit operational failure."""

    normalized_chrom = normalize_primary_chromosome(chrom)
    if assembly != "GRCh38" or start < 1 or end < start:
        return {
            "status": "unavailable",
            "assembly": assembly,
            "chrom": normalized_chrom,
            "start": start,
            "end": end,
            "sequence": None,
            "source": "ensembl_grch38_sequence",
        }
    active_session = session or requests.Session()
    timeout_seconds = min(settings.VEP_TIMEOUT, REFERENCE_SEQUENCE_TIMEOUT_SECONDS)
    url = (
        f"{settings.VEP_BASE_URL}/sequence/region/human/"
        f"{normalized_chrom}:{start}..{end}:1"
    )
    for _attempt in range(REFERENCE_SEQUENCE_MAX_ATTEMPTS):
        try:
            response = active_session.get(
                url,
                params={"coord_system_version": "GRCh38"},
                headers={"Accept": "text/plain"},
                timeout=timeout_seconds,
            )
        except requests.RequestException:
            continue
        if response.status_code in {429, 500, 502, 503, 504}:
            continue
        if response.status_code != 200:
            break
        sequence = response.text.strip().upper()
        if len(sequence) == end - start + 1 and set(sequence) <= {
            "A",
            "C",
            "G",
            "T",
        }:
            return {
                "status": "success",
                "assembly": "GRCh38",
                "chrom": normalized_chrom,
                "start": start,
                "end": end,
                "sequence": sequence,
                "source": "ensembl_grch38_sequence",
            }
        break
    return {
        "status": "unavailable",
        "assembly": "GRCh38",
        "chrom": normalized_chrom,
        "start": start,
        "end": end,
        "sequence": None,
        "source": "ensembl_grch38_sequence",
    }


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
        or not isinstance(sequence, str)
        or len(sequence) != end - start + 1
    ):
        return None
    normalized = sequence.strip().upper()
    return normalized if set(normalized) <= {"A", "C", "G", "T"} else None


__all__ = [
    "REFERENCE_SEQUENCE_MAX_ATTEMPTS",
    "REFERENCE_SEQUENCE_TIMEOUT_SECONDS",
    "ReferenceSequenceResult",
    "fetch_grch38_reference_sequence",
    "reference_sequence_is_success",
]
