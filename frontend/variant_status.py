"""Deterministic Stage 96 per-variant status-card projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict

from backend.final_disposition import (
    FinalDisposition,
    FinalDispositionError,
    build_final_dispositions,
)
from frontend.warning_semantics import WarningNotice, build_warning_notices


VariantCardStatus = Literal[
    "Report ready",
    "Report ready with partial evidence",
    "Interpretation requires attention",
    "Input requires attention",
]


class TechnicalProviderDetail(TypedDict):
    """One compact, reviewer-readable provider detail."""

    source: str
    capability: str
    status: str
    method: str | None


class VariantStatusCard(TypedDict):
    """One stable product-facing status projection for an input variant."""

    variant_index: int
    heading: str
    status: VariantCardStatus
    annotation: str
    population: str
    clinvar: str
    phenotype: str
    interpretation: str
    notices: list[WarningNotice]
    final_disposition: str | None
    capability_summary: str | None
    capability_notices: list[WarningNotice]
    technical_details: list[TechnicalProviderDetail]


_PARTIAL_STATUSES = frozenset({"no_match", "unsupported", "unavailable"})


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalized(value: object) -> str:
    return str(value or "").strip().casefold().replace(" ", "_")


def _source_statuses(
    sources: Sequence[object], capability: str
) -> list[str]:
    statuses: list[str] = []
    for item in sources:
        source = _mapping(item)
        if source is None or source.get("capability") != capability:
            continue
        statuses.append(_normalized(source.get("status")))
    return statuses


def _evidence_line(
    label: str,
    statuses: Sequence[str],
    *,
    no_match: str,
) -> tuple[str, bool]:
    if "available" in statuses:
        return f"{label} available", False
    if "no_match" in statuses:
        return f"{label}: {no_match}", True
    if "unsupported" in statuses:
        return f"{label}: unsupported", True
    if "unavailable" in statuses:
        return f"{label}: unavailable", True
    return f"{label}: not assessed", False


def _heading(index: int, total: int, summary: Mapping[str, object] | None) -> str:
    if summary is None:
        return f"Variant {index + 1} of {total}"
    gene = _text(summary.get("gene"))
    hgvs_c = _text(summary.get("hgvs_c"))
    label = " ".join(item for item in (gene, hgvs_c) if item)
    if not label:
        label = _text(summary.get("display_label")) or "Identity unavailable"
    return f"Variant {index + 1} of {total} — {label}"


def _technical_details(sources: Sequence[object]) -> list[TechnicalProviderDetail]:
    details: list[TechnicalProviderDetail] = []
    for item in sources:
        source = _mapping(item)
        if source is None:
            continue
        name = _text(source.get("source"))
        capability = _text(source.get("capability"))
        status = _text(source.get("status"))
        if name is None or capability is None or status is None:
            continue
        details.append(
            {
                "source": name,
                "capability": capability,
                "status": status,
                "method": _text(source.get("method")),
            }
        )
    return details


def build_variant_status_card(
    report: object,
    *,
    variant_index: int,
    total: int,
    final_disposition: FinalDisposition | None = None,
) -> VariantStatusCard:
    """Build one card without exposing internal exception names as its status."""

    draft = _mapping(report)
    content = _mapping(draft.get("reviewed_report")) if draft is not None else None
    summary = _mapping(content.get("variant_summary")) if content is not None else None
    input_requires_attention = content is None or summary is None
    sources = _sequence(content.get("data_sources")) if content is not None else ()
    notices = build_warning_notices(report)
    input_requires_attention = input_requires_attention or any(
        notice["severity"] == "BLOCKING" for notice in notices
    )

    annotation, annotation_partial = _evidence_line(
        "Annotation",
        _source_statuses(sources, "variant_annotation"),
        no_match="no exact record",
    )
    population, population_partial = _evidence_line(
        "Population evidence",
        _source_statuses(sources, "population_frequency"),
        no_match="no exact record",
    )
    clinvar, clinvar_partial = _evidence_line(
        "ClinVar",
        _source_statuses(sources, "clinvar_evidence"),
        no_match="no exact record",
    )

    phenotype_context = (
        _mapping(content.get("phenotype_context")) if content is not None else None
    )
    phenotype_status = _normalized(
        phenotype_context.get("phenotype_status")
        if phenotype_context is not None
        else None
    )
    if phenotype_status in {
        "supported",
        "strong_match",
        "partial_match",
        "partially_supported",
    }:
        phenotype = "Phenotype relationship supported"
        phenotype_partial = False
    elif phenotype_status in {"no_match", "not_supported", "unrelated"}:
        phenotype = "Phenotype relationship: no supported association"
        phenotype_partial = True
    else:
        phenotype = "Phenotype relationship: not assessed"
        phenotype_partial = False

    interpretation_section = (
        _mapping(content.get("variant_interpretation")) if content is not None else None
    )
    interpretation_failed = (
        interpretation_section is None
        or _normalized(interpretation_section.get("status")) != "success"
    )
    interpretation = (
        "Interpretation requires attention"
        if interpretation_failed
        else "Interpretation completed"
    )

    source_partial = any(
        status in _PARTIAL_STATUSES
        for capability in (
            "variant_annotation",
            "population_frequency",
            "clinvar_evidence",
        )
        for status in _source_statuses(sources, capability)
    )
    disposition_state = (
        final_disposition["state"] if final_disposition is not None else None
    )
    if input_requires_attention or disposition_state == "BLOCKED":
        status: VariantCardStatus = "Input requires attention"
    elif interpretation_failed:
        status = "Interpretation requires attention"
    elif disposition_state == "READY_WITH_LIMITATIONS" or source_partial or any(
        (annotation_partial, population_partial, clinvar_partial, phenotype_partial)
    ):
        status = "Report ready with partial evidence"
    else:
        status = "Report ready"

    capability_notices = (
        [
            {"severity": notice["severity"], "message": notice["message"]}
            for notice in final_disposition["reviewer_notices"]
        ]
        if final_disposition is not None
        else []
    )
    capability_summary = (
        "Evidence capability: "
        f"{final_disposition['state'].replace('_', ' ')} "
        f"(coverage: {final_disposition['coverage_overall_state']})"
        if final_disposition is not None
        else None
    )

    return {
        "variant_index": variant_index,
        "heading": _heading(variant_index, total, summary),
        "status": status,
        "annotation": annotation,
        "population": population,
        "clinvar": clinvar,
        "phenotype": phenotype,
        "interpretation": interpretation,
        "notices": notices,
        "final_disposition": disposition_state,
        "capability_summary": capability_summary,
        "capability_notices": capability_notices,
        "technical_details": _technical_details(sources),
    }


def build_variant_status_cards(result: Mapping[str, object]) -> list[VariantStatusCard]:
    """Project all input variants into ordered, variant-local cards."""

    reports = {
        item.get("variant_index"): item
        for candidate in _sequence(result.get("draft_variant_reports"))
        if (item := _mapping(candidate)) is not None
        and isinstance(item.get("variant_index"), int)
        and not isinstance(item.get("variant_index"), bool)
    }
    declared_total = result.get("variant_count")
    total = (
        declared_total
        if isinstance(declared_total, int)
        and not isinstance(declared_total, bool)
        and declared_total > 0
        else max((int(index) for index in reports), default=-1) + 1
    )
    dispositions: dict[int, FinalDisposition] = {}
    evidence_objects = _sequence(result.get("evidence_objects"))
    readiness_audits = _sequence(result.get("evidence_readiness"))
    if len(evidence_objects) == len(readiness_audits) and evidence_objects:
        try:
            dispositions = {
                item["variant_index"]: item
                for item in build_final_dispositions(
                    [item for item in evidence_objects if isinstance(item, Mapping)],
                    [item for item in readiness_audits if isinstance(item, Mapping)],
                )
            }
        except FinalDispositionError:
            # Historical/partial runtime results retain the established card projection.
            dispositions = {}
    return [
        build_variant_status_card(
            reports.get(index),
            variant_index=index,
            total=total,
            final_disposition=dispositions.get(index),
        )
        for index in range(total)
    ]
