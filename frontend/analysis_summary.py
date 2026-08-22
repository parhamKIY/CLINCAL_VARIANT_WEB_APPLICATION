"""Stage 95 product-level analysis summary projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypedDict

from frontend.variant_status import build_variant_status_cards


AVAILABLE_SOURCE_STATUSES = frozenset(
    {"success", "available", "available_via_fallback"}
)
IGNORED_SOURCE_STATUSES = frozenset(
    {"not_assessed", "not_applicable", "not_triggered", "skipped"}
)
CORE_COVERAGE_CAPABILITIES = frozenset(
    {
        "variant_annotation",
        "variant_context",
        "clinvar_evidence",
        "gene_disease_validity",
        "automated_classification",
    }
)
INPUT_VALIDATION_MESSAGES = {
    "excel": "Excel input validated",
    "manual": "Manual variant input validated",
    "vcf": "VCF input validated",
    "vcf_gz": "Compressed VCF input validated",
}


class AnalysisSummary(TypedDict):
    """Concise outcome counts shown before technical details."""

    headline: str
    input_message: str
    variants_analyzed: int
    draft_reports_prepared: int
    reports_ready: int
    partial_source_coverage: int
    interpretations_requiring_attention: int


def input_validation_message(input_type: object) -> str:
    """Return terminology matching the actual persisted input type."""

    return INPUT_VALIDATION_MESSAGES.get(
        str(input_type).strip().casefold(),
        "Input validated",
    )


def _sequence_length(value: object) -> int:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return 0
    return len(value)


def _normalized_status(value: object) -> str:
    return str(value or "").strip().casefold().replace(" ", "_")


def _has_partial_source_coverage(evidence: object) -> bool:
    if not isinstance(evidence, Mapping):
        return False

    source_statuses = evidence.get("source_statuses")
    if isinstance(source_statuses, Mapping):
        for status in source_statuses.values():
            normalized = _normalized_status(status)
            if (
                normalized
                and normalized not in AVAILABLE_SOURCE_STATUSES
                and normalized not in IGNORED_SOURCE_STATUSES
            ):
                return True

    capabilities = evidence.get("capability_results")
    if isinstance(capabilities, Mapping):
        for capability_name in CORE_COVERAGE_CAPABILITIES:
            capability = capabilities.get(capability_name)
            if not isinstance(capability, Mapping):
                continue
            normalized = _normalized_status(capability.get("status"))
            if (
                normalized
                and normalized not in AVAILABLE_SOURCE_STATUSES
                and normalized not in IGNORED_SOURCE_STATUSES
            ):
                return True
    return False


def build_analysis_summary(result: Mapping[str, object]) -> AnalysisSummary:
    """Project exact analysis-level counts without provider-log leakage."""

    status = str(result.get("status") or "").casefold()
    current_stage = str(result.get("current_stage") or "").casefold()
    headline = (
        "Analysis completed partially"
        if status == "partial"
        else "Analysis complete"
        if status == "success" and current_stage == "completed"
        else "Analysis requires attention"
        if status == "error"
        else "Analysis in progress"
    )
    context = result.get("analysis_context")
    input_type = context.get("input_type") if isinstance(context, Mapping) else None
    evidence_objects = result.get("evidence_objects")
    evidence_items = (
        evidence_objects
        if isinstance(evidence_objects, Sequence)
        and not isinstance(evidence_objects, (str, bytes))
        else ()
    )
    interpretations = result.get("variant_interpretation_results")
    interpretation_items = (
        interpretations
        if isinstance(interpretations, Sequence)
        and not isinstance(interpretations, (str, bytes))
        else ()
    )
    cards = build_variant_status_cards(result)
    return {
        "headline": headline,
        "input_message": input_validation_message(input_type),
        "variants_analyzed": _sequence_length(evidence_items),
        "draft_reports_prepared": _sequence_length(
            result.get("draft_variant_reports")
        ),
        "reports_ready": sum(
            card["status"]
            in {"Report ready", "Report ready with partial evidence"}
            for card in cards
        ),
        "partial_source_coverage": sum(
            _has_partial_source_coverage(evidence) for evidence in evidence_items
        ),
        "interpretations_requiring_attention": sum(
            isinstance(item, Mapping)
            and _normalized_status(item.get("status")) == "failed"
            for item in interpretation_items
        ),
    }


__all__ = [
    "AnalysisSummary",
    "build_analysis_summary",
    "input_validation_message",
]
