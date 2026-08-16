"""Stage 8 runtime-only final evidence capability disposition."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Literal, TypedDict

from backend.evidence_coverage import (
    COVERAGE_STATES,
    SEMANTIC_TARGETS,
    build_evidence_coverage,
    validate_evidence_coverage,
)
from backend.evidence_readiness import (
    READINESS_STATES,
    validate_evidence_readiness_audit,
)


FINAL_DISPOSITION_SCHEMA_VERSION = "1.0"
FinalDispositionState = Literal["READY", "READY_WITH_LIMITATIONS", "BLOCKED"]
FINAL_DISPOSITION_STATES = frozenset({"READY", "READY_WITH_LIMITATIONS", "BLOCKED"})


class ReviewerNotice(TypedDict):
    """A bounded reviewer-facing consequence of one semantic capability."""

    severity: Literal["INFO", "PARTIAL", "BLOCKING"]
    code: str
    semantic_target: str | None
    message: str
    retrieval_states: list[str]
    sources: list[str]
    evidence_paths: list[str]
    correlation_groups: list[str]


class FinalDisposition(TypedDict):
    """Detached, runtime-only Stage 8 projection for one input variant."""

    schema_version: str
    variant_index: int
    variant: dict[str, object]
    internal_readiness: str
    state: FinalDispositionState
    coverage_overall_state: str
    capability_states: dict[str, str]
    limitations: list[str]
    reviewer_notices: list[ReviewerNotice]


class FinalDispositionError(ValueError):
    """Raised when a runtime capability projection is malformed."""


_SEMANTIC_LABELS = {
    "annotation": "Variant annotation",
    "automated_acmg_context": "Automated ACMG context",
    "expert_curated_variant_context": "Expert-curated variant context",
    "clinvar_clinical_evidence": "Clinical variant evidence",
    "cspec_context": "Gene-specific ACMG context",
    "gene_disease_validity": "Gene-disease validity",
    "gene_disease_support": "Gene-disease support",
    "phenotype_gene_ranking": "Phenotype-gene ranking",
    "phenotype_gene_support": "Phenotype-gene support",
    "disease_hpo_context": "Disease-HPO context",
    "population_evidence": "Population evidence",
    "literature_evidence": "Literature evidence",
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _unique_text(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return result


def _notice(
    *,
    severity: Literal["INFO", "PARTIAL", "BLOCKING"],
    code: str,
    target: str | None,
    message: str,
    record: Mapping[str, object] | None = None,
) -> ReviewerNotice:
    data = record or {}
    return {
        "severity": severity,
        "code": code,
        "semantic_target": target,
        "message": message,
        "retrieval_states": _unique_text(data.get("retrieval_states", [])),
        "sources": _unique_text(data.get("sources", [])),
        "evidence_paths": _unique_text(data.get("evidence_paths", [])),
        "correlation_groups": _unique_text(data.get("correlation_groups", [])),
    }


def _record_notices(
    record: Mapping[str, object],
    *,
    support_available: bool = False,
) -> list[ReviewerNotice]:
    target = str(record["semantic_target"])
    label = _SEMANTIC_LABELS[target]
    state = str(record["state"])
    retrieval = set(_unique_text(record.get("retrieval_states", [])))
    notices: list[ReviewerNotice] = []

    if target == "gene_disease_validity" and state == "UNAVAILABLE":
        notices.append(
            _notice(
                severity="PARTIAL",
                code="GENE_DISEASE_VALIDITY_UNAVAILABLE",
                target=target,
                message=(
                    "Gene-disease validity classification is unavailable; "
                    "MedGen provides supporting gene-disease context only."
                    if support_available
                    else "Gene-disease validity classification is unavailable."
                ),
                record=record,
            )
        )
    elif target == "phenotype_gene_ranking" and state == "UNAVAILABLE":
        notices.append(
            _notice(
                severity="PARTIAL",
                code="PHENOTYPE_GENE_RANKING_UNAVAILABLE",
                target=target,
                message=(
                    "Phenotype-gene ranking is unavailable; supporting "
                    "phenotype-gene context is available without a rank/score."
                    if support_available
                    else "Phenotype-gene ranking is unavailable."
                ),
                record=record,
            )
        )
    elif state == "DEGRADED":
        missing = _unique_text(record.get("missing_fields", []))
        detail = f" Missing required fields: {', '.join(missing)}." if missing else ""
        notices.append(
            _notice(
                severity="PARTIAL",
                code="CAPABILITY_DEGRADED",
                target=target,
                message=f"{label} is partially available.{detail}",
                record=record,
            )
        )
    elif state == "UNAVAILABLE" and "no_verified_gene_association" in retrieval:
        notices.append(
            _notice(
                severity="INFO",
                code="NO_VERIFIED_GENE_ASSOCIATION",
                target=target,
                message=(
                    "Candidate disease records were returned, but no exact "
                    "verified gene association was accepted."
                ),
                record=record,
            )
        )
    elif state == "UNAVAILABLE" and "no_match" in retrieval:
        message = (
            "No exact expert-curated ERepo assertion was found for this variant."
            if target == "expert_curated_variant_context"
            else f"No qualifying {label.lower()} was found."
        )
        notices.append(
            _notice(
                severity="INFO",
                code="VALID_NO_MATCH",
                target=target,
                message=message,
                record=record,
            )
        )
    elif state == "UNAVAILABLE" and "operational_failure" in retrieval:
        notices.append(
            _notice(
                severity="PARTIAL",
                code="CAPABILITY_RETRIEVAL_UNAVAILABLE",
                target=target,
                message=(
                    f"{label} could not be retrieved; other collected evidence "
                    "remains available for review."
                ),
                record=record,
            )
        )
    elif state == "UNAVAILABLE":
        notices.append(
            _notice(
                severity="PARTIAL",
                code="CAPABILITY_UNAVAILABLE",
                target=target,
                message=f"{label} is unavailable for this analysis.",
                record=record,
            )
        )
    elif state == "NOT_TRIGGERED":
        notices.append(
            _notice(
                severity="INFO",
                code="CAPABILITY_NOT_TRIGGERED",
                target=target,
                message=f"{label} was not triggered for this analysis.",
                record=record,
            )
        )
    elif state == "NOT_APPLICABLE":
        notices.append(
            _notice(
                severity="INFO",
                code="CAPABILITY_NOT_APPLICABLE",
                target=target,
                message=f"{label} is not applicable to this analysis.",
                record=record,
            )
        )

    composition = record.get("composition_state")
    if target == "annotation" and composition in {"composed", "mixed"}:
        sources = _unique_text(record.get("sources", []))
        source_context = (
            f" Source-attributed to {', '.join(sources)}."
            if sources
            else ""
        )
        notices.append(
            _notice(
                severity="INFO",
                code="COMPOSED_ANNOTATION",
                target=target,
                message=(
                    "Annotation uses safely promoted active fields from validated "
                    "fallback evidence; provider reachability remains a separate "
                    f"technical detail.{source_context}"
                ),
                record=record,
            )
        )
    for group in _unique_text(record.get("correlation_groups", [])):
        notices.append(
            _notice(
                severity="INFO",
                code="CORRELATED_UPSTREAM_EVIDENCE",
                target=target,
                message=(
                    f"{label} includes shared upstream evidence from {group}; "
                    "do not treat it as an independent evidence vote."
                ),
                record=record,
            )
        )
    return notices


def _rescue_state(records: Mapping[str, Mapping[str, object]]) -> FinalDispositionState:
    """Reassess an unfinalized rescue audit using semantic, not provider, coverage."""

    annotation = records["annotation"]
    if annotation["state"] != "FULL":
        return "READY_WITH_LIMITATIONS"
    if any(
        record["state"] in {"DEGRADED", "UNAVAILABLE"}
        for record in records.values()
    ):
        return "READY_WITH_LIMITATIONS"
    return "READY"


def _state(
    readiness: Mapping[str, object],
    records: Mapping[str, Mapping[str, object]],
) -> FinalDispositionState:
    internal = readiness["readiness_after_rescue"]
    if internal == "MINIMUM_IDENTITY_FAILURE":
        return "BLOCKED"
    if internal == "READY":
        return "READY"
    if internal == "READY_WITH_LIMITATIONS":
        return "READY_WITH_LIMITATIONS"
    if internal == "RESCUE_REQUIRED":
        return _rescue_state(records)
    raise FinalDispositionError("Internal readiness state is invalid.")


def build_final_disposition(
    evidence: Mapping[str, object],
    readiness_audit: Mapping[str, object],
    *,
    coverage: Mapping[str, object] | None = None,
) -> FinalDisposition:
    """Derive final reviewer capability without changing readiness or evidence."""

    audit = validate_evidence_readiness_audit(readiness_audit)
    result_coverage = validate_evidence_coverage(
        build_evidence_coverage(evidence) if coverage is None else coverage
    )
    if result_coverage["variant"] != evidence.get("variant"):
        raise FinalDispositionError("Coverage and evidence variant identities differ.")
    records = {
        str(record["semantic_target"]): record
        for record in result_coverage["semantic_capabilities"]
    }
    if set(records) != set(SEMANTIC_TARGETS):
        raise FinalDispositionError("Coverage semantic targets are incomplete.")
    state = _state(audit, records)
    notices: list[ReviewerNotice] = []
    for target in SEMANTIC_TARGETS:
        notices.extend(
            _record_notices(
                records[target],
                support_available=(
                    target == "gene_disease_validity"
                    and records["gene_disease_support"]["state"] == "FULL"
                )
                or (
                    target == "phenotype_gene_ranking"
                    and records["phenotype_gene_support"]["state"] == "FULL"
                ),
            )
        )
    if state == "BLOCKED":
        notices.insert(
            0,
            _notice(
                severity="BLOCKING",
                code="MINIMUM_IDENTITY_FAILURE",
                target=None,
                message=(
                    "The variant input does not meet the minimum identity and "
                    "normalization requirements. Correct the input before continuing."
                ),
            ),
        )
    limitations = _unique_text(
        limitation
        for record in records.values()
        for limitation in _sequence(record.get("limitations"))
    )
    return validate_final_disposition(
        {
            "schema_version": FINAL_DISPOSITION_SCHEMA_VERSION,
            "variant_index": audit["variant_index"],
            "variant": deepcopy(result_coverage["variant"]),
            "internal_readiness": audit["readiness_after_rescue"],
            "state": state,
            "coverage_overall_state": result_coverage["overall_state"],
            "capability_states": {
                target: str(records[target]["state"])
                for target in SEMANTIC_TARGETS
            },
            "limitations": limitations,
            "reviewer_notices": notices,
        }
    )


def build_final_dispositions(
    evidence_objects: Iterable[Mapping[str, object]],
    readiness_audits: Iterable[Mapping[str, object]],
) -> list[FinalDisposition]:
    """Derive input-ordered runtime dispositions without persistence changes."""

    evidence_items = list(evidence_objects)
    audit_items = list(readiness_audits)
    if len(evidence_items) != len(audit_items):
        raise FinalDispositionError("Evidence and readiness cardinalities differ.")
    return [
        build_final_disposition(evidence, audit)
        for evidence, audit in zip(evidence_items, audit_items, strict=True)
    ]


def validate_final_disposition(value: object) -> FinalDisposition:
    """Validate and detach the bounded Stage 8 runtime projection."""

    item = _mapping(value)
    required = {
        "schema_version", "variant_index", "variant", "internal_readiness", "state",
        "coverage_overall_state", "capability_states", "limitations", "reviewer_notices",
    }
    if set(item) != required or item.get("schema_version") != FINAL_DISPOSITION_SCHEMA_VERSION:
        raise FinalDispositionError("Final disposition fields are invalid.")
    index = item.get("variant_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise FinalDispositionError("Final disposition variant index is invalid.")
    variant = _mapping(item.get("variant"))
    if set(variant) != {"chrom", "pos", "ref", "alt"}:
        raise FinalDispositionError("Final disposition variant is invalid.")
    if item.get("internal_readiness") not in READINESS_STATES:
        raise FinalDispositionError("Final disposition readiness is invalid.")
    if item.get("state") not in FINAL_DISPOSITION_STATES:
        raise FinalDispositionError("Final disposition state is invalid.")
    if item.get("coverage_overall_state") not in COVERAGE_STATES:
        raise FinalDispositionError("Final disposition coverage is invalid.")
    capabilities = _mapping(item.get("capability_states"))
    if set(capabilities) != set(SEMANTIC_TARGETS) or any(
        state not in COVERAGE_STATES for state in capabilities.values()
    ):
        raise FinalDispositionError("Final disposition capabilities are invalid.")
    if not isinstance(item.get("limitations"), list) or not isinstance(item.get("reviewer_notices"), list):
        raise FinalDispositionError("Final disposition lists are invalid.")
    notices: list[ReviewerNotice] = []
    for notice in item["reviewer_notices"]:
        data = _mapping(notice)
        expected_notice = {
            "severity", "code", "semantic_target", "message", "retrieval_states",
            "sources", "evidence_paths", "correlation_groups",
        }
        if (
            set(data) != expected_notice
            or data.get("severity") not in {"INFO", "PARTIAL", "BLOCKING"}
            or not isinstance(data.get("code"), str)
            or data.get("semantic_target") not in {*SEMANTIC_TARGETS, None}
            or not isinstance(data.get("message"), str)
            or any(not isinstance(data.get(name), list) for name in expected_notice - {"severity", "code", "semantic_target", "message"})
        ):
            raise FinalDispositionError("Final disposition notice is invalid.")
        notices.append({name: (list(data[name]) if isinstance(data[name], list) else data[name]) for name in expected_notice})
    return {
        "schema_version": FINAL_DISPOSITION_SCHEMA_VERSION,
        "variant_index": index,
        "variant": deepcopy(dict(variant)),
        "internal_readiness": item["internal_readiness"],
        "state": item["state"],
        "coverage_overall_state": item["coverage_overall_state"],
        "capability_states": dict(capabilities),
        "limitations": list(item["limitations"]),
        "reviewer_notices": notices,
    }


__all__ = [
    "FINAL_DISPOSITION_SCHEMA_VERSION",
    "FINAL_DISPOSITION_STATES",
    "FinalDisposition",
    "FinalDispositionError",
    "ReviewerNotice",
    "build_final_disposition",
    "build_final_dispositions",
    "validate_final_disposition",
]
