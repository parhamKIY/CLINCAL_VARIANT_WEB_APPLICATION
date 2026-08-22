"""Clinical-data minimization and LLM privacy boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


PUBLIC_VARIANT_FIELDS = (
    "input_index",
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
PHONE_PATTERN = re.compile(
    r"(?<![\w:])(?:\+?\d{1,3}[ .-])?"
    r"(?:\(?\d{2,4}\)?[ .-]){2,4}\d{2,4}(?!\w)"
)
GOVERNMENT_ID_PATTERN = re.compile(
    r"(?i)\b(?:ssn|social[ _-]?security|national[ _-]?id|"
    r"passport|identity[ _-]?(?:number|no|id))"
    r"\s*(?::|=|is)?\s*[A-Z0-9][A-Z0-9 .-]{4,30}\b"
)
PERSON_NAME_CONTEXT_PATTERN = re.compile(
    r"(?i)\b(?:patient|subject|individual|proband|participant)"
    r"(?:[ _-]?name)?\s*(?::|=|is|was)\s*"
    r"[A-Z][A-Z'’-]{1,39}(?:\s+[A-Z][A-Z'’-]{1,39}){1,3}\b"
)
CLINICAL_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:[\\/]|/)[^\s\r\n]*"
    r"(?:patient|sample)[^\s\r\n]*"
)
RAW_VCF_LINE_PATTERN = re.compile(
    r"(?im)^.*(?:##fileformat=VCF|#CHROM[\t ]+POS[\t ]+ID[\t ]+REF[\t ]+ALT).*$"
)
CLINICAL_REDACTED = "[REDACTED CLINICAL DATA]"
MAX_PHENOTYPE_CLINICAL_TEXT_CHARACTERS = 4_000
PHENOTYPE_EXTRACTION_TASK = "extract_hpo_candidates"
VARIANT_INTERPRETATION_TASK = "interpret_variant"
PHENOTYPE_EXTRACTION_PAYLOAD_FIELDS = frozenset(
    {"clinical_text_fa", "task"}
)
VARIANT_INTERPRETATION_PAYLOAD_FIELDS = frozenset(
    {"evidence", "prompt_mode", "reference_catalog", "task"}
)
VARIANT_INTERPRETATION_PROHIBITED_EVIDENCE_FIELDS = frozenset(
    {
        "annotation_promotion",
        "attempts",
        "candidate_diagnostics",
        "candidate_rejections",
        "candidates_rejected",
        "candidates_returned",
        "circuit_open",
        "detail_attempts",
        "detail_http_status",
        "enrichment_decision",
        "fallback_http_status",
        "human_review",
        "http_status",
        "intermediate_http_status",
        "omitted_content_sha256",
        "primary_circuit_open",
        "primary_http_status",
        "primary_request_attempts",
        "post_review",
        "request_attempts",
        "retry_after",
        "retry_count",
        "section_budget_bytes",
        "shadow_composition",
    }
)
VARIANT_INTERPRETATION_COMPACTION_FIELDS = frozenset(
    {"applied", "omitted_item_count"}
)
VARIANT_REFERENCE_CATALOG_FIELDS = frozenset(
    {"identifier", "identifier_type", "reference_id", "source", "title"}
)
MAX_VARIANT_INTERPRETATION_REFERENCES = 200
PERSIAN_DIGIT = r"0-9۰-۹٠-٩"
PERSIAN_LABELED_IDENTIFIER_PATTERN = re.compile(
    r"(?:نام[\s‌]+بیمار|نام[\s‌]+و[\s‌]+نام[\s‌]+خانوادگی|"
    r"کد[\s‌]+ملی|شماره[\s‌]+(?:ملی|پرونده|تماس|تلفن)|"
    r"تلفن(?:[\s‌]+همراه)?|موبایل|ایمیل|رایانامه|"
    r"تاریخ[\s‌]+تولد|نشانی|آدرس)"
    r"\s*(?:[:=：]|است)?\s*[^\r\n,;،؛.]+"
)
IRANIAN_PHONE_PATTERN = re.compile(
    rf"(?<!\w)(?:\+?(?:98|۹۸|٩٨)[ .-]?)?"
    rf"(?:0|۰|٠)?(?:9|۹|٩)[{PERSIAN_DIGIT}]{{9}}(?!\w)"
)


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
            ) or PERSIAN_LABELED_IDENTIFIER_PATTERN.search(
                item
            ) or IRANIAN_PHONE_PATTERN.search(item):
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


def validate_phenotype_extraction_payload(
    value: object,
) -> dict[str, str]:
    """Validate the exact minimum-data payload for phenotype extraction."""

    if not isinstance(value, Mapping) or set(value) != (
        PHENOTYPE_EXTRACTION_PAYLOAD_FIELDS
    ):
        raise ClinicalDataPrivacyError(
            "Phenotype extraction payload has unsupported fields."
        )
    if value.get("task") != PHENOTYPE_EXTRACTION_TASK:
        raise ClinicalDataPrivacyError(
            "Phenotype extraction payload has an invalid task."
        )
    clinical_text = value.get("clinical_text_fa")
    sanitized = sanitize_phenotype_clinical_text(clinical_text)
    if sanitized != clinical_text:
        raise ClinicalDataPrivacyError(
            "Phenotype extraction payload is not fully sanitized."
        )
    normalized = {
        "clinical_text_fa": sanitized,
        "task": PHENOTYPE_EXTRACTION_TASK,
    }
    validate_llm_payload(normalized)
    return normalized


def validate_variant_interpretation_payload(value: object) -> None:
    """Validate the separate bounded interpretation-model payload."""

    if not isinstance(value, Mapping) or set(value) != (
        VARIANT_INTERPRETATION_PAYLOAD_FIELDS
    ):
        raise ClinicalDataPrivacyError(
            "Variant interpretation payload has unsupported fields."
        )
    if value.get("task") != VARIANT_INTERPRETATION_TASK:
        raise ClinicalDataPrivacyError(
            "Variant interpretation payload has an invalid task."
        )
    if value.get("prompt_mode") not in {"standard", "conflict_aware"}:
        raise ClinicalDataPrivacyError(
            "Variant interpretation payload has an invalid prompt mode."
        )
    evidence = value.get("evidence")
    references = value.get("reference_catalog")
    if not isinstance(evidence, Mapping):
        raise ClinicalDataPrivacyError(
            "Variant interpretation evidence is invalid."
        )
    if (
        not isinstance(references, Sequence)
        or isinstance(references, (str, bytes, bytearray))
        or len(references) > MAX_VARIANT_INTERPRETATION_REFERENCES
        or any(not isinstance(item, Mapping) for item in references)
    ):
        raise ClinicalDataPrivacyError(
            "Variant interpretation reference catalog is invalid."
        )
    if any(
        set(item) != VARIANT_REFERENCE_CATALOG_FIELDS
        for item in references
        if isinstance(item, Mapping)
    ):
        raise ClinicalDataPrivacyError(
            "Variant interpretation reference catalog has unsupported fields."
        )
    if any(
        isinstance(nested, str)
        and re.search(r"(?i)(?:https?://|www\.)", nested)
        for item in references
        if isinstance(item, Mapping)
        for nested in item.values()
    ):
        raise ClinicalDataPrivacyError(
            "Variant interpretation reference catalog must not contain URLs."
        )

    def contains_phenotype_text(item: object) -> bool:
        if isinstance(item, Mapping):
            return any(
                str(key).strip().casefold() == "clinical_text_fa"
                or contains_phenotype_text(nested)
                for key, nested in item.items()
            )
        if (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
        ):
            return any(contains_phenotype_text(nested) for nested in item)
        return False

    if contains_phenotype_text(evidence):
        raise ClinicalDataPrivacyError(
            "Variant interpretation payload contains phenotype-extraction text."
        )

    def validate_evidence_projection(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = _normalize_field_name(key)
                if normalized in (
                    VARIANT_INTERPRETATION_PROHIBITED_EVIDENCE_FIELDS
                ):
                    raise ClinicalDataPrivacyError(
                        "Variant interpretation evidence contains internal "
                        "operational metadata."
                    )
                if normalized == "compaction":
                    if not isinstance(nested, Mapping) or {
                        _normalize_field_name(field) for field in nested
                    } != VARIANT_INTERPRETATION_COMPACTION_FIELDS:
                        raise ClinicalDataPrivacyError(
                            "Variant interpretation compaction summary is invalid."
                        )
                validate_evidence_projection(nested)
        elif (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
        ):
            for nested in item:
                validate_evidence_projection(nested)

    validate_evidence_projection(evidence)
    validate_llm_payload(value)


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

    def inspect_free_text(item: object) -> None:
        if isinstance(item, str):
            if any(
                pattern.search(item)
                for pattern in (
                    PHONE_PATTERN,
                    GOVERNMENT_ID_PATTERN,
                    PERSON_NAME_CONTEXT_PATTERN,
                    PERSIAN_LABELED_IDENTIFIER_PATTERN,
                    IRANIAN_PHONE_PATTERN,
                )
            ):
                raise ClinicalDataPrivacyError(
                    "Human review content contains prohibited "
                    "clinical data."
                )
        elif isinstance(item, Mapping):
            for nested in item.values():
                inspect_free_text(nested)
        elif (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
        ):
            for nested in item:
                inspect_free_text(nested)

    inspect_free_text(reviewer_notes)
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
        if item.get("change_type") in {"added", "modified"}:
            inspect_free_text(item.get("new_value"))


def redact_clinical_text(value: str) -> str:
    """Remove labelled identifiers, raw VCF headers, and clinical paths."""

    sanitized = RAW_VCF_LINE_PATTERN.sub(CLINICAL_REDACTED, value)
    sanitized = LABELED_CLINICAL_TEXT_PATTERN.sub(
        CLINICAL_REDACTED,
        sanitized,
    )
    sanitized = EMAIL_PATTERN.sub(CLINICAL_REDACTED, sanitized)
    sanitized = PHONE_PATTERN.sub(CLINICAL_REDACTED, sanitized)
    sanitized = GOVERNMENT_ID_PATTERN.sub(
        CLINICAL_REDACTED,
        sanitized,
    )
    sanitized = PERSON_NAME_CONTEXT_PATTERN.sub(
        CLINICAL_REDACTED,
        sanitized,
    )
    sanitized = PERSIAN_LABELED_IDENTIFIER_PATTERN.sub(
        CLINICAL_REDACTED,
        sanitized,
    )
    sanitized = IRANIAN_PHONE_PATTERN.sub(
        CLINICAL_REDACTED,
        sanitized,
    )
    return CLINICAL_PATH_PATTERN.sub(CLINICAL_REDACTED, sanitized)


def sanitize_phenotype_clinical_text(value: object) -> str:
    """Return bounded de-identified text for phenotype extraction only."""

    if not isinstance(value, str) or not value.strip():
        raise ClinicalDataPrivacyError(
            "The clinical description must be non-empty text."
        )
    normalized = value.strip()
    if len(normalized) > MAX_PHENOTYPE_CLINICAL_TEXT_CHARACTERS:
        raise ClinicalDataPrivacyError(
            "The clinical description exceeds the 4000-character limit."
        )
    if any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"}
        for character in normalized
    ):
        raise ClinicalDataPrivacyError(
            "The clinical description contains invalid control characters."
        )
    sanitized = redact_clinical_text(normalized).strip()
    validate_llm_payload(
        {
            "task": "extract_hpo_candidates",
            "clinical_text_fa": sanitized,
        }
    )
    return sanitized


__all__ = [
    "ClinicalDataPrivacyError",
    "CLINICAL_REDACTED",
    "MAX_PHENOTYPE_CLINICAL_TEXT_CHARACTERS",
    "PHENOTYPE_EXTRACTION_TASK",
    "PROHIBITED_CLINICAL_FIELD_NAMES",
    "PUBLIC_VARIANT_FIELDS",
    "is_prohibited_clinical_field",
    "minimize_variant",
    "redact_clinical_text",
    "sanitize_phenotype_clinical_text",
    "validate_human_review_content",
    "validate_llm_payload",
    "validate_no_prohibited_fields",
    "validate_phenotype_extraction_payload",
    "validate_variant_interpretation_payload",
]
