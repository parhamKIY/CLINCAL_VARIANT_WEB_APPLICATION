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
        "birth_date",
        "email",
        "email_address",
        "date_of_birth",
        "dob",
        "full_name",
        "given_name",
        "genotype",
        "home_address",
        "family_name",
        "medical_record_number",
        "mrn",
        "patient_id",
        "patient_name",
        "phone",
        "phone_number",
        "postal_address",
        "raw_api_payload",
        "raw_vcf",
        "sample",
        "sample_id",
        "sample_name",
        "source_filename",
        "vcf_content",
    }
)
APPROVED_PATIENT_CONTEXT_FIELDS = frozenset(
    {
        "matched_patient_hpo_terms",
        "patient_hpo_terms",
        "unmatched_patient_hpo_terms",
    }
)
PROHIBITED_IDENTITY_FIELDS = frozenset(
    f"{prefix}_{suffix}"
    for prefix in ("patient", "sample")
    for suffix in (
        "address",
        "barcode",
        "birth_date",
        "dob",
        "email",
        "id",
        "identifier",
        "label",
        "mrn",
        "name",
        "phone",
    )
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
    re.compile(
        r"(?i)\b(?:birth[_ -]?date|email(?:[_ -]?address)?|"
        r"phone(?:[_ -]?number)?|home[_ -]?address|postal[_ -]?address)"
        r"\s*[:=]"
    ),
    re.compile(
        r"(?i)(?:[A-Z]:[\\/]|/)[^\s\r\n]*"
        r"(?:patient|sample)[^\s\r\n]*"
    ),
)
LABELED_CLINICAL_TEXT_PATTERN = re.compile(
    r"(?i)\b(patient[_ -]?(?:name|id)|sample[_ -]?(?:name|id)|"
    r"medical[_ -]?record(?:[_ -]?number)?|mrn|"
    r"date[_ -]?of[_ -]?birth|dob|birth[_ -]?date|"
    r"email(?:[_ -]?address)?|phone(?:[_ -]?number)?|genotype|GT)"
    r"(\s*[:=]\s*)([^\r\n,;]+)"
)
EMAIL_PATTERN = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
CLINICAL_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:[\\/]|/)[^\s\r\n]*"
    r"(?:patient|sample)[^\s\r\n]*"
)
RAW_VCF_LINE_PATTERN = re.compile(
    r"(?im)^.*(?:##fileformat=VCF|#CHROM[\t ]+POS[\t ]+ID[\t ]+REF[\t ]+ALT).*$"
)
CLINICAL_REDACTED = "[REDACTED CLINICAL DATA]"


class ClinicalDataPrivacyError(ValueError):
    """Raised when data violates an approved privacy boundary."""


def _normalize_field_name(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


def is_prohibited_clinical_field(value: object) -> bool:
    """Return whether a field name represents unnecessary identity data."""

    normalized = _normalize_field_name(value)
    if normalized in APPROVED_PATIENT_CONTEXT_FIELDS:
        return False
    return (
        normalized in PROHIBITED_CLINICAL_FIELD_NAMES
        or normalized in PROHIBITED_IDENTITY_FIELDS
        or normalized.startswith("raw_")
        or normalized.endswith(
            ("_email", "_phone", "_address", "_mrn", "_dob")
        )
    )


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
        if any(is_prohibited_clinical_field(key) for key in value):
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


def validate_human_review_content(
    reviewed_user_report: object,
    reviewer_notes: object,
    edit_history: object,
) -> None:
    """Reject unnecessary identifiers before review data reaches storage."""

    validate_llm_payload(
        {
            "reviewed_user_report": reviewed_user_report,
            "reviewer_notes": reviewer_notes,
            "edit_history": edit_history,
        }
    )
    if not isinstance(edit_history, Sequence) or isinstance(
        edit_history,
        (str, bytes, bytearray),
    ):
        return
    for item in edit_history:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        segments = (
            segment.replace("~1", "/").replace("~0", "~")
            for segment in path.split("/")
            if segment
        )
        if any(
            is_prohibited_clinical_field(segment)
            for segment in segments
        ):
            raise ClinicalDataPrivacyError(
                "Human review audit data contains prohibited clinical data."
            )


def redact_clinical_text(value: str) -> str:
    """Remove labelled identifiers, raw VCF headers, and clinical paths."""

    sanitized = RAW_VCF_LINE_PATTERN.sub(CLINICAL_REDACTED, value)
    sanitized = LABELED_CLINICAL_TEXT_PATTERN.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{CLINICAL_REDACTED}"
        ),
        sanitized,
    )
    sanitized = EMAIL_PATTERN.sub(CLINICAL_REDACTED, sanitized)
    return CLINICAL_PATH_PATTERN.sub(CLINICAL_REDACTED, sanitized)


__all__ = [
    "ClinicalDataPrivacyError",
    "CLINICAL_REDACTED",
    "PROHIBITED_CLINICAL_FIELD_NAMES",
    "PUBLIC_VARIANT_FIELDS",
    "is_prohibited_clinical_field",
    "minimize_variant",
    "redact_clinical_text",
    "validate_human_review_content",
    "validate_llm_payload",
    "validate_no_prohibited_fields",
]
