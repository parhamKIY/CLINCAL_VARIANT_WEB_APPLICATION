"""Clinical-data minimization and LLM privacy boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


PUBLIC_VARIANT_FIELDS = (
    "chrom",
    "pos",
    "ref",
    "alt",
    "qual",
    "filter",
)
PROHIBITED_CLINICAL_FIELD_NAMES = frozenset(
    {
        "date_of_birth",
        "dob",
        "genotype",
        "medical_record_number",
        "mrn",
        "patient_id",
        "patient_name",
        "raw_api_payload",
        "raw_vcf",
        "sample",
        "sample_id",
        "sample_name",
        "source_filename",
        "vcf_content",
    }
)
PROHIBITED_LLM_TEXT_PATTERNS = (
    re.compile(r"(?i)##fileformat=VCF"),
    re.compile(
        r"(?i)#CHROM[\t ]+POS[\t ]+ID[\t ]+REF[\t ]+ALT"
    ),
    re.compile(
        r"(?i)\b(?:patient[_ -]?(?:name|id)|"
        r"sample[_ -]?(?:name|id)|medical[_ -]?record(?:[_ -]?number)?|"
        r"mrn|date[_ -]?of[_ -]?birth|dob|genotype|GT)"
        r"\s*[:=]"
    ),
    re.compile(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
    ),
)


class ClinicalDataPrivacyError(ValueError):
    """Raised when data violates an approved privacy boundary."""


def minimize_variant(
    variant: Mapping[str, Any],
) -> dict[str, object]:
    """Return only non-sample variant fields approved for public output."""

    return {
        field: variant[field]
        for field in PUBLIC_VARIANT_FIELDS
        if field in variant
    }


def validate_no_prohibited_fields(
    value: object,
    *,
    context: str,
) -> None:
    """Reject prohibited clinical keys anywhere in a nested structure."""

    if isinstance(value, Mapping):
        normalized_keys = {
            str(key).strip().casefold().replace("-", "_")
            for key in value
        }
        if (
            normalized_keys & PROHIBITED_CLINICAL_FIELD_NAMES
            or any(key.startswith("raw_") for key in normalized_keys)
        ):
            raise ClinicalDataPrivacyError(
                f"{context} contains prohibited clinical data."
            )
        for item in value.values():
            validate_no_prohibited_fields(item, context=context)
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
    ):
        for item in value:
            validate_no_prohibited_fields(item, context=context)


def validate_llm_payload(value: object) -> None:
    """Reject raw VCF or labelled personal data before an LLM call."""

    validate_no_prohibited_fields(value, context="LLM payload")

    def inspect(item: object) -> None:
        if isinstance(item, str):
            if any(
                pattern.search(item)
                for pattern in PROHIBITED_LLM_TEXT_PATTERNS
            ):
                raise ClinicalDataPrivacyError(
                    "LLM payload contains prohibited clinical data."
                )
        elif isinstance(item, Mapping):
            for nested in item.values():
                inspect(nested)
        elif (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
        ):
            for nested in item:
                inspect(nested)

    inspect(value)


__all__ = [
    "ClinicalDataPrivacyError",
    "PROHIBITED_CLINICAL_FIELD_NAMES",
    "PUBLIC_VARIANT_FIELDS",
    "minimize_variant",
    "validate_llm_payload",
    "validate_no_prohibited_fields",
]
