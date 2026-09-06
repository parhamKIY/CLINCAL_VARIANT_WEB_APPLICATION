"""Stage 97 consequence-oriented warning semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict

from frontend.interpretation_failure_semantics import (
    EXPLAINABLE_INTERPRETATION_FAILURE_TYPES,
    interpretation_failure_message,
)
from frontend.source_status import build_reviewer_source_status

WarningSeverity = Literal["INFO", "PARTIAL", "ACTION REQUIRED", "BLOCKING"]


class WarningNotice(TypedDict):
    """One reviewer-facing consequence notice."""

    severity: WarningSeverity
    message: str


_CAPABILITY_LABELS = {
    "variant_annotation": "Variant annotation",
    "variant_context": "Variant context",
    "clinvar_evidence": "ClinVar evidence",
    "gene_disease_validity": "Gene-disease evidence",
    "automated_classification": "Automated classification evidence",
    "cspec_context": "CSpec context",
    "phenotype_gene": "Phenotype-gene evidence",
    "disease_context": "Disease context",
    "population_frequency": "Population evidence",
    "literature": "Literature evidence",
}
_EXPECTED_ABSENCE = frozenset({"no_match"})
_PARTIAL_SOURCE_STATES = frozenset({"unavailable", "unsupported"})
_AVAILABLE_STATES = frozenset(
    {"available", "success", "available_via_fallback"}
)
_NO_ASSOCIATION_STATES = frozenset(
    {"no_match", "not_supported", "unrelated"}
)
_UNAVAILABLE_PHENOTYPE_STATES = frozenset({"unavailable", "failed"})
_DISCLAIMER_PREFIXES = (
    "decision-support report only",
    "decision support report only",
)
_EXPECTED_ABSENCE_TERMS = (
    "no exact record",
    "no exact match",
    "no match",
    "not found",
    "no literature",
    "no supported phenotype association",
)
_BLOCKING_TERMS = (
    "invalid allele identity",
    "unsupported input format",
    "corrupt input",
    "normalization impossible",
    "could not be normalized",
)
_SEVERITY_ORDER = {
    "BLOCKING": 0,
    "ACTION REQUIRED": 1,
    "PARTIAL": 2,
    "INFO": 3,
}


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _normalized(value: object) -> str:
    return str(value or "").strip().casefold().replace(" ", "_")


def _notice(
    severity: WarningSeverity,
    message: str,
) -> WarningNotice:
    return {"severity": severity, "message": message}


def _source_notices(sources: Sequence[object]) -> list[WarningNotice]:
    by_capability: dict[str, list[Mapping[str, object]]] = {}
    for item in sources:
        source = _mapping(item)
        if source is None:
            continue
        capability = _normalized(source.get("capability"))
        status = _normalized(source.get("status"))
        if capability and status:
            by_capability.setdefault(capability, []).append(source)

    notices: list[WarningNotice] = []
    for capability, records in by_capability.items():
        statuses = {_normalized(record.get("status")) for record in records}
        if statuses & _AVAILABLE_STATES:
            continue
        label = _CAPABILITY_LABELS.get(
            capability,
            capability.replace("_", " ").capitalize(),
        )
        if statuses & _PARTIAL_SOURCE_STATES:
            notices.append(
                _notice(
                    "PARTIAL",
                    f"{label} could not be included. The report uses the "
                    "evidence that remains available.",
                )
            )
        elif statuses & _EXPECTED_ABSENCE:
            no_match_record = next(
                record
                for record in records
                if _normalized(record.get("status")) == "no_match"
            )
            notices.append(
                _notice(
                    "INFO",
                    build_reviewer_source_status(no_match_record)["message"],
                )
            )
    return notices


def _raw_warning_notice(content: Mapping[str, object]) -> WarningNotice | None:
    interpretation = _mapping(content.get("variant_interpretation")) or {}
    candidates = [
        *_sequence(interpretation.get("warnings")),
        *_sequence(content.get("limitations")),
    ]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        normalized = candidate.strip().casefold()
        if normalized.startswith(_DISCLAIMER_PREFIXES):
            continue
        if any(term in normalized for term in _BLOCKING_TERMS):
            return _notice(
                "BLOCKING",
                "The variant input does not meet the minimum identity and "
                "normalization requirements. Correct the input before continuing.",
            )
        if any(term in normalized for term in _EXPECTED_ABSENCE_TERMS):
            continue
        return _notice(
            "PARTIAL",
            "Some evidence has limitations. Review the report details before "
            "confirmation.",
        )
    return None


def build_warning_notices(report: object) -> list[WarningNotice]:
    """Classify one report by consequence without exposing implementation trivia."""

    draft = _mapping(report)
    content = _mapping(draft.get("reviewed_report")) if draft is not None else None
    if content is None or _mapping(content.get("variant_summary")) is None:
        return [
            _notice(
                "BLOCKING",
                "A valid normalized variant report is unavailable. Correct the "
                "input before continuing.",
            )
        ]

    notices = _source_notices(_sequence(content.get("data_sources")))
    phenotype = _mapping(content.get("phenotype_context")) or {}
    phenotype_status = _normalized(phenotype.get("phenotype_status"))
    if phenotype_status in _NO_ASSOCIATION_STATES:
        notices.append(
            _notice(
                "INFO",
                "No supported phenotype association was found for this variant.",
            )
        )
    elif phenotype_status in _UNAVAILABLE_PHENOTYPE_STATES:
        notices.append(
            _notice(
                "PARTIAL",
                "Phenotype evidence could not be assessed. The report uses the "
                "evidence that remains available.",
            )
        )

    interpretation = _mapping(content.get("variant_interpretation"))
    if interpretation is None or _normalized(interpretation.get("status")) != "success":
        failure_type = (
            interpretation.get("failure_type")
            if interpretation is not None
            else None
        )
        failure_message = (
            interpretation_failure_message(failure_type)
            if failure_type in EXPLAINABLE_INTERPRETATION_FAILURE_TYPES
            else "Interpretation could not be produced after recovery attempts."
        )
        notices.append(
            _notice(
                "ACTION REQUIRED",
                failure_message if failure_type == "insufficient_evidence" else (
                    f"{failure_message} Review the collected evidence and retry "
                    "interpretation."
                ),
            )
        )
    else:
        raw_notice = _raw_warning_notice(content)
        if raw_notice is not None:
            notices.append(raw_notice)

    unique: list[WarningNotice] = []
    seen: set[tuple[str, str]] = set()
    for notice in notices:
        key = (notice["severity"], notice["message"])
        if key not in seen:
            seen.add(key)
            unique.append(notice)
    return sorted(unique, key=lambda item: _SEVERITY_ORDER[item["severity"]])


__all__ = [
    "WarningNotice",
    "WarningSeverity",
    "build_warning_notices",
]
