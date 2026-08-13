"""Deterministic evidence-readiness assessment before interpretation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, TypedDict, cast

from backend.variant_integrity import (
    VariantIntegrityError,
    stable_allele_identity,
)


EVIDENCE_READINESS_SCHEMA_VERSION = "1.0"

ReadinessState = Literal[
    "READY",
    "READY_WITH_LIMITATIONS",
    "RESCUE_REQUIRED",
    "MINIMUM_IDENTITY_FAILURE",
]
DimensionStatus = Literal[
    "available",
    "missing",
    "not_assessed",
    "conflicting",
    "degraded",
    "valid",
]
RescueActionName = Literal[
    "population_evidence_rescue",
    "literature_context_rescue",
]
RescueActionStatus = Literal[
    "planned",
    "success",
    "no_match",
    "unavailable",
    "skipped",
]

READINESS_STATES = frozenset(cast(tuple[str, ...], ReadinessState.__args__))
DIMENSION_STATUSES = frozenset(
    cast(tuple[str, ...], DimensionStatus.__args__)
)
RESCUE_ACTION_NAMES = frozenset(
    cast(tuple[str, ...], RescueActionName.__args__)
)
RESCUE_ACTION_STATUSES = frozenset(
    cast(tuple[str, ...], RescueActionStatus.__args__)
)
DIMENSION_NAMES = (
    "allele_identity",
    "annotation_consequence",
    "classification_evidence",
    "population_evidence",
    "gene_disease_context",
    "phenotype_relationship",
    "computational_evidence",
    "literature",
    "conflict_state",
    "provenance_quality",
)
DEFICIT_NAMES = frozenset(
    {
        "allele_identity_unvalidated",
        "annotation_or_consequence_missing",
        "classification_evidence_missing",
        "population_evidence_missing",
        "gene_disease_context_missing",
        "phenotype_relationship_missing",
        "computational_evidence_missing",
        "literature_missing",
        "meaningful_conflict",
        "provenance_quality_degraded",
    }
)


class EvidenceReadinessDimensions(TypedDict):
    """Clinically meaningful dimensions assessed without provider counting."""

    allele_identity: DimensionStatus
    annotation_consequence: DimensionStatus
    classification_evidence: DimensionStatus
    population_evidence: DimensionStatus
    gene_disease_context: DimensionStatus
    phenotype_relationship: DimensionStatus
    computational_evidence: DimensionStatus
    literature: DimensionStatus
    conflict_state: DimensionStatus
    provenance_quality: DimensionStatus


class EvidenceReadinessRescueAction(TypedDict):
    """One targeted rescue route and its bounded outcome."""

    action: RescueActionName
    targets: list[str]
    status: RescueActionStatus


class EvidenceReadinessAudit(TypedDict):
    """Pre/post rescue decision retained for one interpretation attempt."""

    schema_version: str
    variant_index: int
    allele_identity: str | None
    readiness_before_rescue: ReadinessState
    identified_deficits: list[str]
    rescue_actions: list[EvidenceReadinessRescueAction]
    readiness_after_rescue: ReadinessState
    remaining_deficits: list[str]
    dimensions_before_rescue: EvidenceReadinessDimensions
    dimensions_after_rescue: EvidenceReadinessDimensions


class EvidenceReadinessError(ValueError):
    """Raised when readiness input or audit provenance is invalid."""


AUDIT_FIELDS = frozenset(EvidenceReadinessAudit.__required_keys__)
ACTION_FIELDS = frozenset(EvidenceReadinessRescueAction.__required_keys__)
DIMENSION_FIELDS = frozenset(EvidenceReadinessDimensions.__required_keys__)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_content(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_has_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_content(item) for item in value)
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return _non_empty(value)


def _allele_identity(evidence: Mapping[str, object]) -> str | None:
    try:
        return stable_allele_identity(
            _mapping(evidence.get("variant")),
            assembly=evidence.get("assembly"),
        )
    except VariantIntegrityError:
        return None


def _classification_available(evidence: Mapping[str, object]) -> bool:
    audit = _mapping(_mapping(evidence.get("conflict_audit")).get(
        "pre_review"
    ))
    normalized = audit.get("normalized_classifications")
    if isinstance(normalized, list) and bool(normalized):
        return True
    pathogenicity = _mapping(evidence.get("pathogenicity"))
    return any(
        _non_empty(pathogenicity.get(field))
        for field in (
            "automated_acmg_classification",
            "clinvar_classification",
        )
    )


def _population_available(evidence: Mapping[str, object]) -> bool:
    frequency = evidence.get("population_frequency")
    if (
        not isinstance(frequency, bool)
        and isinstance(frequency, (int, float))
        and math.isfinite(float(frequency))
    ):
        return True
    population = _mapping(
        _mapping(evidence.get("conditional_enrichment")).get(
            "population_frequency"
        )
    )
    return population.get("status") in {"available", "partial", "success"}


def _disease_context_available(evidence: Mapping[str, object]) -> bool:
    if evidence.get("clinvar_conditions") or evidence.get("clingen_curations"):
        return True
    phenotype = _mapping(evidence.get("phenotype_relationship"))
    mydisease = _mapping(phenotype.get("mydisease"))
    return bool(mydisease.get("diseases"))


def _phenotype_status(evidence: Mapping[str, object]) -> DimensionStatus:
    hpo_terms = evidence.get("hpo_terms")
    if not isinstance(hpo_terms, list) or not hpo_terms:
        return "not_assessed"
    phenotype = _mapping(evidence.get("phenotype_relationship"))
    if phenotype.get("phenotype_status") in {
        "exact_match",
        "partial_match",
        "no_exact_match",
    }:
        return "available"
    return "missing"


def _provenance_status(evidence: Mapping[str, object]) -> DimensionStatus:
    capabilities = _mapping(evidence.get("capability_results"))
    operational = {
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
    }
    if any(
        _mapping(value).get("status") in operational
        for value in capabilities.values()
    ):
        return "degraded"
    return "available"


def assess_evidence_dimensions(
    evidence: Mapping[str, object],
) -> tuple[str | None, EvidenceReadinessDimensions, list[str]]:
    """Assess meaningful evidence dimensions without provider-count scoring."""

    if not isinstance(evidence, Mapping):
        raise EvidenceReadinessError("Evidence readiness input must be a mapping.")
    identity = _allele_identity(evidence)
    annotation = (
        "available"
        if _non_empty(evidence.get("consequence"))
        else "missing"
    )
    classification = (
        "available" if _classification_available(evidence) else "missing"
    )
    population = "available" if _population_available(evidence) else "missing"
    disease = (
        "available" if _disease_context_available(evidence) else "missing"
    )
    phenotype = _phenotype_status(evidence)
    predictors = _mapping(_mapping(evidence.get("annotations")).get(
        "predictors"
    ))
    computational = "available" if _has_content(predictors) else "missing"
    literature = _mapping(
        _mapping(evidence.get("conditional_enrichment")).get("literature")
    )
    literature_status = (
        "available"
        if bool(literature.get("articles"))
        or literature.get("status") in {"available", "partial", "success"}
        else "missing"
    )
    conflict = _mapping(_mapping(evidence.get("conflict_audit")).get(
        "pre_review"
    ))
    conflict_status = (
        "conflicting"
        if conflict.get("routing_severity") in {
            "moderate",
            "major",
            "critical",
        }
        else "available"
    )
    provenance = _provenance_status(evidence)
    dimensions: EvidenceReadinessDimensions = {
        "allele_identity": "valid" if identity is not None else "missing",
        "annotation_consequence": cast(DimensionStatus, annotation),
        "classification_evidence": cast(DimensionStatus, classification),
        "population_evidence": cast(DimensionStatus, population),
        "gene_disease_context": cast(DimensionStatus, disease),
        "phenotype_relationship": phenotype,
        "computational_evidence": cast(DimensionStatus, computational),
        "literature": cast(DimensionStatus, literature_status),
        "conflict_state": cast(DimensionStatus, conflict_status),
        "provenance_quality": provenance,
    }
    deficit_by_dimension = {
        "allele_identity": "allele_identity_unvalidated",
        "annotation_consequence": "annotation_or_consequence_missing",
        "classification_evidence": "classification_evidence_missing",
        "population_evidence": "population_evidence_missing",
        "gene_disease_context": "gene_disease_context_missing",
        "phenotype_relationship": "phenotype_relationship_missing",
        "computational_evidence": "computational_evidence_missing",
        "literature": "literature_missing",
        "conflict_state": "meaningful_conflict",
        "provenance_quality": "provenance_quality_degraded",
    }
    deficits = [
        deficit_by_dimension[name]
        for name in DIMENSION_NAMES
        if dimensions[name] in {"missing", "not_assessed", "conflicting", "degraded"}
    ]
    return identity, dimensions, deficits


def _rescue_already_assessed(
    evidence: Mapping[str, object],
    field: str,
) -> bool:
    enrichment = _mapping(evidence.get("conditional_enrichment"))
    payload = _mapping(enrichment.get(field))
    return payload.get("status") not in {None, "not_triggered"}


def _plan_actions(
    evidence: Mapping[str, object],
    deficits: list[str],
) -> list[EvidenceReadinessRescueAction]:
    actions: list[EvidenceReadinessRescueAction] = []
    if (
        "population_evidence_missing" in deficits
        and not _rescue_already_assessed(evidence, "population_frequency")
    ):
        actions.append(
            {
                "action": "population_evidence_rescue",
                "targets": ["population_evidence_missing"],
                "status": "planned",
            }
        )
    context_targets = [
        deficit
        for deficit in (
            "classification_evidence_missing",
            "gene_disease_context_missing",
            "phenotype_relationship_missing",
        )
        if deficit in deficits
    ]
    if context_targets and not _rescue_already_assessed(
        evidence,
        "literature",
    ):
        actions.append(
            {
                "action": "literature_context_rescue",
                "targets": context_targets,
                "status": "planned",
            }
        )
    return actions


def _readiness_state(
    dimensions: EvidenceReadinessDimensions,
    deficits: list[str],
    *,
    planned_actions: list[EvidenceReadinessRescueAction],
    after_rescue: bool,
) -> ReadinessState:
    if dimensions["allele_identity"] != "valid":
        return "MINIMUM_IDENTITY_FAILURE"
    if not after_rescue and planned_actions:
        return "RESCUE_REQUIRED"
    return "READY_WITH_LIMITATIONS" if deficits else "READY"


def _action_outcome(
    evidence: Mapping[str, object],
    action: RescueActionName,
) -> RescueActionStatus:
    enrichment = _mapping(evidence.get("conditional_enrichment"))
    payload = _mapping(
        enrichment.get(
            "population_frequency"
            if action == "population_evidence_rescue"
            else "literature"
        )
    )
    status = payload.get("status")
    if status in {"available", "partial", "success"}:
        return "success"
    if status in {"no_match", "not_found", "missing_identifier"}:
        return "no_match"
    if status in {"unavailable", "invalid_response"}:
        return "unavailable"
    return "skipped"


def build_evidence_readiness_audit(
    evidence: Mapping[str, object],
    *,
    variant_index: int,
    before_rescue: Mapping[str, object] | None = None,
) -> EvidenceReadinessAudit:
    """Build a deterministic pre/post readiness audit for one variant."""

    if (
        isinstance(variant_index, bool)
        or not isinstance(variant_index, int)
        or variant_index < 0
    ):
        raise EvidenceReadinessError("variant_index must be non-negative.")
    identity, dimensions, deficits = assess_evidence_dimensions(evidence)
    if before_rescue is None:
        actions = _plan_actions(evidence, deficits)
        state = _readiness_state(
            dimensions,
            deficits,
            planned_actions=actions,
            after_rescue=False,
        )
        return validate_evidence_readiness_audit(
            {
                "schema_version": EVIDENCE_READINESS_SCHEMA_VERSION,
                "variant_index": variant_index,
                "allele_identity": identity,
                "readiness_before_rescue": state,
                "identified_deficits": deficits,
                "rescue_actions": actions,
                "readiness_after_rescue": state,
                "remaining_deficits": deficits,
                "dimensions_before_rescue": dimensions,
                "dimensions_after_rescue": dimensions,
            }
        )

    prior = validate_evidence_readiness_audit(before_rescue)
    if prior["variant_index"] != variant_index:
        raise EvidenceReadinessError(
            "Readiness audit variant index changed during rescue."
        )
    actions = [
        {
            "action": action["action"],
            "targets": list(action["targets"]),
            "status": _action_outcome(evidence, action["action"]),
        }
        for action in prior["rescue_actions"]
    ]
    after = _readiness_state(
        dimensions,
        deficits,
        planned_actions=actions,
        after_rescue=True,
    )
    return validate_evidence_readiness_audit(
        {
            "schema_version": EVIDENCE_READINESS_SCHEMA_VERSION,
            "variant_index": variant_index,
            "allele_identity": identity,
            "readiness_before_rescue": prior["readiness_before_rescue"],
            "identified_deficits": list(prior["identified_deficits"]),
            "rescue_actions": actions,
            "readiness_after_rescue": after,
            "remaining_deficits": deficits,
            "dimensions_before_rescue": dict(
                prior["dimensions_before_rescue"]
            ),
            "dimensions_after_rescue": dimensions,
        }
    )


def validate_evidence_readiness_audit(
    value: object,
) -> EvidenceReadinessAudit:
    """Validate and detach one bounded readiness audit."""

    if not isinstance(value, Mapping) or set(value) != AUDIT_FIELDS:
        raise EvidenceReadinessError("Evidence readiness fields are invalid.")
    item = dict(value)
    if item["schema_version"] != EVIDENCE_READINESS_SCHEMA_VERSION:
        raise EvidenceReadinessError("Evidence readiness version is unsupported.")
    index = item["variant_index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise EvidenceReadinessError("Evidence readiness index is invalid.")
    identity = item["allele_identity"]
    if identity is not None and (
        not isinstance(identity, str)
        or not identity.startswith("allele-sha256:")
    ):
        raise EvidenceReadinessError("Evidence readiness identity is invalid.")
    for field in ("readiness_before_rescue", "readiness_after_rescue"):
        if item[field] not in READINESS_STATES:
            raise EvidenceReadinessError(f"{field} is invalid.")
    for field in ("identified_deficits", "remaining_deficits"):
        values = item[field]
        if (
            not isinstance(values, list)
            or len(values) > len(DEFICIT_NAMES)
            or len(set(values)) != len(values)
            or not set(values).issubset(DEFICIT_NAMES)
        ):
            raise EvidenceReadinessError(f"{field} is invalid.")
    for field in ("dimensions_before_rescue", "dimensions_after_rescue"):
        dimensions = item[field]
        if (
            not isinstance(dimensions, Mapping)
            or set(dimensions) != DIMENSION_FIELDS
            or any(
                status not in DIMENSION_STATUSES
                for status in dimensions.values()
            )
        ):
            raise EvidenceReadinessError(f"{field} is invalid.")
    actions = item["rescue_actions"]
    if not isinstance(actions, list) or len(actions) > len(RESCUE_ACTION_NAMES):
        raise EvidenceReadinessError("rescue_actions is invalid.")
    seen_actions: set[str] = set()
    for action in actions:
        if not isinstance(action, Mapping) or set(action) != ACTION_FIELDS:
            raise EvidenceReadinessError("A rescue action is invalid.")
        name = action["action"]
        targets = action["targets"]
        if name not in RESCUE_ACTION_NAMES or name in seen_actions:
            raise EvidenceReadinessError("A rescue action name is invalid.")
        seen_actions.add(cast(str, name))
        if (
            not isinstance(targets, list)
            or not targets
            or len(set(targets)) != len(targets)
            or not set(targets).issubset(DEFICIT_NAMES)
            or action["status"] not in RESCUE_ACTION_STATUSES
        ):
            raise EvidenceReadinessError("A rescue action outcome is invalid.")
    return cast(EvidenceReadinessAudit, deepcopy(item))


__all__ = [
    "EVIDENCE_READINESS_SCHEMA_VERSION",
    "EvidenceReadinessAudit",
    "EvidenceReadinessError",
    "build_evidence_readiness_audit",
    "validate_evidence_readiness_audit",
]
