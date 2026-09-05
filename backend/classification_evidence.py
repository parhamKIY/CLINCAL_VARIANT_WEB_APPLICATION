"""Deterministic, source-attributed classification-evidence audit."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Literal, TypedDict, cast

from backend.conflict_auditor import (
    classification_disagreement_severity,
    normalize_source_classification_label,
)


CLASSIFICATION_EVIDENCE_SCHEMA_VERSION = "1.0"

ClassificationEvidenceState = Literal[
    "DIRECT_CLASSIFICATION_AVAILABLE",
    "SECONDARY_CLASSIFICATION_EVIDENCE_AVAILABLE",
    "CONFLICTING_CLASSIFICATIONS",
    "NO_CLASSIFICATION_AFTER_RESCUE",
    "NOT_ASSESSED",
]
ClassificationEvidenceRole = Literal["direct", "automated", "derived"]

CLASSIFICATION_STATES = frozenset(
    cast(tuple[str, ...], ClassificationEvidenceState.__args__)
)
CLASSIFICATION_ROLES = frozenset(
    cast(tuple[str, ...], ClassificationEvidenceRole.__args__)
)
RESOLVED_STATUSES = frozenset(
    {"success", "available", "no_match", "not_found", "unsupported"}
)
UNRESOLVED_STATUSES = frozenset(
    {
        "pending",
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
        "error",
    }
)


class ClassificationFinding(TypedDict):
    """One classification observation without autonomous adjudication."""

    source: str
    evidence_role: ClassificationEvidenceRole
    classification: str
    normalized_classification: str
    review_status: str | None
    independent_evidence: bool


class ClassificationSourceOutcome(TypedDict):
    """One configured classification source and its completed outcome."""

    source: str
    status: str
    method: str | None


class ClassificationEvidenceAudit(TypedDict):
    """Bounded explanation for available or unavailable classification evidence."""

    schema_version: str
    state: ClassificationEvidenceState
    classifications: list[ClassificationFinding]
    sources_queried: list[str]
    source_outcomes: list[ClassificationSourceOutcome]
    identifiers_used: list[str]
    rescue_attempted: bool
    rescue_outcome: str | None
    candidates_returned: int | None
    candidates_rejected: int | None
    retrieval_exhausted: bool
    unavailable_reasons: list[str]
    independent_acmg_adjudication: bool


class ClassificationEvidenceError(ValueError):
    """Raised when a classification-evidence audit is invalid."""


AUDIT_FIELDS = frozenset(ClassificationEvidenceAudit.__required_keys__)
FINDING_FIELDS = frozenset(ClassificationFinding.__required_keys__)
OUTCOME_FIELDS = frozenset(ClassificationSourceOutcome.__required_keys__)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _provider_source(
    evidence: Mapping[str, object],
    source_name: str,
) -> Mapping[str, object]:
    providers = _mapping(evidence.get("provenance")).get("providers")
    if not isinstance(providers, list):
        return {}
    return next(
        (
            _mapping(item)
            for item in providers
            if _mapping(item).get("source") == source_name
        ),
        {},
    )


def _finding(
    *,
    source: str,
    role: ClassificationEvidenceRole,
    value: object,
    review_status: object,
    independent: bool,
) -> ClassificationFinding | None:
    original = _text(value)
    normalized = normalize_source_classification_label(value)
    if original is None:
        return None
    normalized = normalized or original
    return {
        "source": source,
        "evidence_role": role,
        "classification": original,
        "normalized_classification": normalized,
        "review_status": _text(review_status),
        "independent_evidence": independent,
    }


def _classifications(evidence: Mapping[str, object]) -> list[ClassificationFinding]:
    pathogenicity = _mapping(evidence.get("pathogenicity"))
    annotations = _mapping(evidence.get("annotations"))
    population = _mapping(annotations.get("population"))
    derived = _mapping(population.get("clinvar_derived"))
    capability = _mapping(
        _mapping(evidence.get("capability_results")).get("clinvar_evidence")
    )
    genebe_lineage = next(
        (
            _mapping(item)
            for item in _mapping(evidence.get("provenance")).get("lineage", [])
            if _mapping(item).get("evidence_path") == "annotations.genebe"
        ),
        {},
    )
    genebe_upstream = genebe_lineage.get("upstream_sources")
    genebe_independent = not (
        isinstance(genebe_upstream, list) and "ClinVar" in genebe_upstream
    )
    direct_role = capability.get("provider_role") != "fallback"
    direct_source = (
        "NCBI ClinVar"
        if direct_role
        else "MyVariant.info (ClinVar-derived fallback)"
    )
    findings = [
        _finding(
            source=direct_source,
            role="direct" if direct_role else "derived",
            value=pathogenicity.get("clinvar_classification"),
            review_status=pathogenicity.get("clinvar_review_status"),
            independent=direct_role,
        ),
        _finding(
            source="GeneBe",
            role="automated",
            value=pathogenicity.get("automated_acmg_classification"),
            review_status="Automated annotation only",
            independent=genebe_independent,
        ),
        _finding(
            source="MyVariant.info",
            role="derived",
            value=derived.get("clinical_significance"),
            review_status=derived.get("review_status"),
            independent=False,
        ),
    ]
    retained: list[ClassificationFinding] = []
    seen: set[tuple[str, str]] = set()
    for item in findings:
        if item is None:
            continue
        key = (item["source"], item["normalized_classification"])
        if key not in seen:
            seen.add(key)
            retained.append(item)
    return retained


def _source_outcomes(
    evidence: Mapping[str, object],
) -> list[ClassificationSourceOutcome]:
    capabilities = _mapping(evidence.get("capability_results"))
    clinvar = _mapping(capabilities.get("clinvar_evidence"))
    myvariant = _mapping(capabilities.get("variant_context"))
    genebe = _mapping(_mapping(evidence.get("annotations")).get("genebe"))
    genebe_provider = _provider_source(evidence, "genebe")
    return [
        {
            "source": "NCBI ClinVar",
            "status": _text(clinvar.get("status")) or "not_assessed",
            "method": _text(clinvar.get("method")),
        },
        {
            "source": "GeneBe",
            "status": (
                _text(genebe.get("status"))
                or _text(genebe_provider.get("status"))
                or "not_assessed"
            ),
            "method": "automated_acmg_annotation",
        },
        {
            "source": "MyVariant.info",
            "status": _text(myvariant.get("status")) or "not_assessed",
            "method": _text(myvariant.get("method")),
        },
    ]


def _query_audit(
    evidence: Mapping[str, object],
) -> tuple[list[str], bool, str | None, int | None, int | None, list[str]]:
    capability = _mapping(
        _mapping(evidence.get("capability_results")).get("clinvar_evidence")
    )
    provenance = _mapping(capability.get("provenance"))
    assessment = _mapping(provenance.get("retrieval_assessment"))
    rescue = _mapping(provenance.get("evidence_rescue"))
    identifiers: list[str] = []
    for raw in assessment.get("identifiers_used", []):
        item = _mapping(raw)
        kind = _text(item.get("type"))
        value = _text(item.get("value"))
        if kind and value:
            identifiers.append(f"{kind}: {value}")
    for raw in rescue.get("alternate_identifiers", []):
        item = _mapping(raw)
        kind = _text(item.get("type"))
        value = _text(item.get("value"))
        label = f"{kind}: {value}" if kind and value else None
        if label and label not in identifiers:
            identifiers.append(label)
    if not identifiers:
        variant = _mapping(evidence.get("variant"))
        assembly = _text(evidence.get("assembly"))
        chrom = _text(variant.get("chrom"))
        pos = variant.get("pos")
        ref = _text(variant.get("ref"))
        alt = _text(variant.get("alt"))
        if assembly and chrom and isinstance(pos, int) and ref and alt:
            identifiers.append(
                f"normalized_allele: {assembly}:{chrom}:{pos}:{ref}>{alt}"
            )
    attempted = rescue.get("attempted") is True
    rescue_outcome = _text(rescue.get("stop_reason"))
    returned = assessment.get("candidates_returned")
    rejected = assessment.get("candidates_rejected")
    reasons: list[str] = []
    cause = _text(assessment.get("cause"))
    if cause:
        reasons.append(f"NCBI ClinVar retrieval: {cause}")
    if rescue_outcome:
        reasons.append(f"Classification rescue: {rescue_outcome}")
    return (
        identifiers,
        attempted,
        rescue_outcome,
        returned if isinstance(returned, int) and not isinstance(returned, bool) else None,
        rejected if isinstance(rejected, int) and not isinstance(rejected, bool) else None,
        reasons,
    )


def _retrieval_exhausted(
    outcomes: list[ClassificationSourceOutcome],
    *,
    clinvar_assessment: Mapping[str, object],
    rescue: Mapping[str, object],
    classification_available: bool,
) -> bool:
    if any(item["status"] in UNRESOLVED_STATUSES for item in outcomes):
        return False
    if any(item["status"] not in RESOLVED_STATUSES for item in outcomes):
        return False
    cause = clinvar_assessment.get("cause")
    if cause in {
        "OPERATIONAL_FAILURE",
        "QUERY_WEAKNESS",
        "IDENTIFIER_GAP",
        "LIVE_VERIFICATION_REQUIRED",
    }:
        return False
    if classification_available:
        return True
    clinvar_status = outcomes[0]["status"]
    if clinvar_status in {"no_match", "not_found"}:
        if rescue.get("attempted") is not True:
            return False
        if rescue.get("stop_reason") in {
            "missing_identifier",
            "secondary_unavailable",
            "attempt_limit_reached",
        }:
            return False
    return True


def build_classification_evidence_audit(
    evidence: Mapping[str, object],
) -> ClassificationEvidenceAudit:
    """Build the classification state and the evidence-retrieval explanation."""

    if not isinstance(evidence, Mapping):
        raise ClassificationEvidenceError("Classification evidence must be a mapping.")
    findings = _classifications(evidence)
    outcomes = _source_outcomes(evidence)
    capability = _mapping(
        _mapping(evidence.get("capability_results")).get("clinvar_evidence")
    )
    provenance = _mapping(capability.get("provenance"))
    assessment = _mapping(provenance.get("retrieval_assessment"))
    rescue = _mapping(provenance.get("evidence_rescue"))
    identifiers, attempted, rescue_outcome, returned, rejected, reasons = (
        _query_audit(evidence)
    )
    exhausted = _retrieval_exhausted(
        outcomes,
        clinvar_assessment=assessment,
        rescue=rescue,
        classification_available=bool(findings),
    )
    normalized = {item["normalized_classification"] for item in findings}
    if classification_disagreement_severity(normalized) == "major":
        state: ClassificationEvidenceState = "CONFLICTING_CLASSIFICATIONS"
    elif any(item["evidence_role"] == "direct" for item in findings):
        state = "DIRECT_CLASSIFICATION_AVAILABLE"
    elif findings:
        state = "SECONDARY_CLASSIFICATION_EVIDENCE_AVAILABLE"
    elif exhausted:
        state = "NO_CLASSIFICATION_AFTER_RESCUE"
    else:
        state = "NOT_ASSESSED"
    if not findings:
        reasons.extend(
            f"{item['source']}: {item['status']}" for item in outcomes
        )
    audit: ClassificationEvidenceAudit = {
        "schema_version": CLASSIFICATION_EVIDENCE_SCHEMA_VERSION,
        "state": state,
        "classifications": findings,
        "sources_queried": [item["source"] for item in outcomes],
        "source_outcomes": outcomes,
        "identifiers_used": identifiers,
        "rescue_attempted": attempted,
        "rescue_outcome": rescue_outcome,
        "candidates_returned": returned,
        "candidates_rejected": rejected,
        "retrieval_exhausted": exhausted,
        "unavailable_reasons": reasons if not findings else [],
        "independent_acmg_adjudication": False,
    }
    return validate_classification_evidence_audit(audit)


def validate_classification_evidence_audit(
    value: object,
) -> ClassificationEvidenceAudit:
    """Validate and detach a bounded classification-evidence audit."""

    if not isinstance(value, Mapping) or set(value) != AUDIT_FIELDS:
        raise ClassificationEvidenceError("Classification audit fields are invalid.")
    if value["schema_version"] != CLASSIFICATION_EVIDENCE_SCHEMA_VERSION:
        raise ClassificationEvidenceError("Classification audit version is unsupported.")
    if value["state"] not in CLASSIFICATION_STATES:
        raise ClassificationEvidenceError("Classification state is invalid.")
    findings = value["classifications"]
    if not isinstance(findings, list) or len(findings) > 3:
        raise ClassificationEvidenceError("Classifications must be a bounded list.")
    for item in findings:
        if not isinstance(item, Mapping) or set(item) != FINDING_FIELDS:
            raise ClassificationEvidenceError("A classification finding is invalid.")
        if item["evidence_role"] not in CLASSIFICATION_ROLES:
            raise ClassificationEvidenceError("Classification role is invalid.")
        for field in ("source", "classification", "normalized_classification"):
            if _text(item[field]) is None:
                raise ClassificationEvidenceError("Classification text is invalid.")
        if item["review_status"] is not None and _text(item["review_status"]) is None:
            raise ClassificationEvidenceError("Classification review status is invalid.")
        if not isinstance(item["independent_evidence"], bool):
            raise ClassificationEvidenceError("Evidence independence is invalid.")
    outcomes = value["source_outcomes"]
    if not isinstance(outcomes, list) or len(outcomes) != 3:
        raise ClassificationEvidenceError("Classification source outcomes are invalid.")
    for item in outcomes:
        if not isinstance(item, Mapping) or set(item) != OUTCOME_FIELDS:
            raise ClassificationEvidenceError("A source outcome is invalid.")
        if _text(item["source"]) is None or _text(item["status"]) is None:
            raise ClassificationEvidenceError("Source outcome text is invalid.")
        if item["method"] is not None and _text(item["method"]) is None:
            raise ClassificationEvidenceError("Source method is invalid.")
    for field in ("sources_queried", "identifiers_used", "unavailable_reasons"):
        items = value[field]
        if not isinstance(items, list) or len(items) > 20:
            raise ClassificationEvidenceError(f"{field} is invalid.")
        if any(_text(item) is None for item in items) or len(set(items)) != len(items):
            raise ClassificationEvidenceError(f"{field} is invalid.")
    for field in ("rescue_attempted", "retrieval_exhausted"):
        if not isinstance(value[field], bool):
            raise ClassificationEvidenceError(f"{field} must be boolean.")
    if value["rescue_outcome"] is not None and _text(value["rescue_outcome"]) is None:
        raise ClassificationEvidenceError("rescue_outcome is invalid.")
    for field in ("candidates_returned", "candidates_rejected"):
        item = value[field]
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or item < 0
        ):
            raise ClassificationEvidenceError(f"{field} is invalid.")
    if value["independent_acmg_adjudication"] is not False:
        raise ClassificationEvidenceError(
            "The application cannot claim independent ACMG adjudication."
        )
    if value["state"] == "NO_CLASSIFICATION_AFTER_RESCUE" and (
        findings or value["retrieval_exhausted"] is not True
    ):
        raise ClassificationEvidenceError(
            "Unavailable classification requires exhausted retrieval and no evidence."
        )
    if value["state"] == "NOT_ASSESSED" and value["retrieval_exhausted"] is True:
        raise ClassificationEvidenceError("NOT_ASSESSED cannot be exhausted.")
    return cast(ClassificationEvidenceAudit, deepcopy(dict(value)))


__all__ = [
    "CLASSIFICATION_EVIDENCE_SCHEMA_VERSION",
    "ClassificationEvidenceAudit",
    "ClassificationEvidenceError",
    "build_classification_evidence_audit",
    "validate_classification_evidence_audit",
]
