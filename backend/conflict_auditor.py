"""Deterministic routing-only conflict checks for Evidence Object V2."""

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, TypedDict


ConflictSeverity = Literal[
    "none",
    "minor",
    "moderate",
    "major",
    "critical",
]
AuditPhase = Literal["pre_review", "post_review"]

MAX_CONFLICT_FINDINGS = 25
STALE_EVIDENCE_DAYS = 365
SEVERITY_ORDER: dict[str, int] = {
    "none": 0,
    "minor": 1,
    "moderate": 2,
    "major": 3,
    "critical": 4,
}


class ConflictAuditError(ValueError):
    """Raised when a deterministic conflict audit input is invalid."""


class NormalizedClassification(TypedDict):
    """One source-attributed normalized five-class assertion."""

    evidence_path: str
    source: str
    original_label: str
    normalized_label: str
    review_status: str | None


class ConflictFinding(TypedDict):
    """One deterministic routing finding without a medical score."""

    conflict_type: str
    severity: ConflictSeverity
    evidence_paths: list[str]
    sources: list[str]
    message: str


class ConflictAuditResult(TypedDict):
    """Bounded pre- or post-review audit result."""

    phase: AuditPhase
    status: str
    routing_severity: ConflictSeverity
    findings: list[ConflictFinding]
    normalized_classifications: list[NormalizedClassification]
    final_classification: None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _term_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _transcript_base(value: str) -> str:
    """Strip the numeric version suffix from a transcript identifier.

    Examples::

        "ENST00000350721.9"  -> "ENST00000350721"
        "ENST00000350721.10" -> "ENST00000350721"
        "NM_001234.5"        -> "NM_001234"
        "ENST00000350721"    -> "ENST00000350721"  (no suffix, unchanged)

    Only a trailing ``.N`` where N consists entirely of digits is removed.
    Non-numeric suffixes (e.g. ``.p1``) are preserved so that non-versioned
    dot-separated identifiers are not incorrectly stripped.
    """
    if "." in value:
        base, _, suffix = value.rpartition(".")
        if suffix.isdigit():
            return base
    return value


def normalize_classification_label(value: object) -> str | None:
    """Normalize one unambiguous assertion to the required five classes."""

    label = _text(value)
    if label is None:
        return None
    key = _term_key(label)
    aliases = {
        "benign": "Benign",
        "likely benign": "Likely Benign",
        "vus": "VUS",
        "uncertain significance": "VUS",
        "variant of uncertain significance": "VUS",
        "likely pathogenic": "Likely Pathogenic",
        "pathogenic": "Pathogenic",
        "conflicting classifications of pathogenicity": "Conflicting",
        "conflicting interpretations of pathogenicity": "Conflicting",
    }
    return aliases.get(key)


def _source_name(
    evidence: Mapping[str, object],
    evidence_path: str,
    fallback: str,
) -> str:
    provenance = _mapping(evidence.get("provenance"))
    lineage = provenance.get("lineage")
    if isinstance(lineage, list):
        for record in lineage:
            if (
                isinstance(record, dict)
                and record.get("evidence_path") == evidence_path
            ):
                return _text(record.get("provider")) or fallback
    return fallback


def _classification_records(
    evidence: Mapping[str, object],
) -> list[NormalizedClassification]:
    annotations = _mapping(evidence.get("annotations"))
    genebe = _mapping(annotations.get("genebe"))
    population = _mapping(annotations.get("population"))
    pathogenicity = _mapping(evidence.get("pathogenicity"))
    derived = _mapping(genebe.get("clinvar_derived"))
    myvariant_derived = _mapping(population.get("clinvar_derived"))
    candidates = (
        (
            "pathogenicity.automated_acmg_classification",
            "annotations.genebe",
            pathogenicity.get("automated_acmg_classification"),
            genebe.get("review_status"),
            "GeneBe",
        ),
        (
            "pathogenicity.clinvar_classification",
            "pathogenicity.clinvar",
            pathogenicity.get("clinvar_classification"),
            pathogenicity.get("clinvar_review_status"),
            "NCBI ClinVar",
        ),
        (
            "annotations.genebe.clinvar_derived.classification",
            "annotations.genebe.clinvar_derived",
            derived.get("classification"),
            derived.get("review_status"),
            "GeneBe",
        ),
        (
            "annotations.population.clinvar_derived.clinical_significance",
            "annotations.population.clinvar_derived",
            myvariant_derived.get("clinical_significance"),
            myvariant_derived.get("review_status"),
            "MyVariant.info",
        ),
    )
    records: list[NormalizedClassification] = []
    for path, lineage_path, original, review_status, fallback in candidates:
        original_label = _text(original)
        normalized = normalize_classification_label(original)
        if original_label is None or normalized is None:
            continue
        records.append(
            {
                "evidence_path": path,
                "source": _source_name(
                    evidence,
                    lineage_path,
                    fallback,
                ),
                "original_label": original_label,
                "normalized_label": normalized,
                "review_status": _text(review_status),
            }
        )
    erepo_context = _mapping(
        pathogenicity.get("expert_curated_variant_context")
    )
    erepo_records = erepo_context.get("records")
    if isinstance(erepo_records, list):
        for index, record in enumerate(erepo_records):
            if not isinstance(record, Mapping):
                continue
            original_label = _text(record.get("classification"))
            normalized = normalize_classification_label(original_label)
            if original_label is None or normalized is None:
                continue
            evidence_path = (
                "pathogenicity.expert_curated_variant_context."
                f"records[{index}].classification"
            )
            records.append(
                {
                    "evidence_path": evidence_path,
                    "source": _source_name(
                        evidence,
                        evidence_path.rsplit(".classification", 1)[0],
                        "ClinGen ERepo",
                    ),
                    "original_label": original_label,
                    "normalized_label": normalized,
                    "review_status": _text(record.get("assertion_method")),
                }
            )
    return records


def _add_finding(
    findings: list[ConflictFinding],
    *,
    conflict_type: str,
    severity: ConflictSeverity,
    evidence_paths: list[str],
    sources: list[str],
    message: str,
) -> None:
    key = (conflict_type, tuple(sorted(set(evidence_paths))))
    if any(
        (
            finding["conflict_type"],
            tuple(sorted(finding["evidence_paths"])),
        )
        == key
        for finding in findings
    ):
        return
    if len(findings) >= MAX_CONFLICT_FINDINGS:
        return
    findings.append(
        {
            "conflict_type": conflict_type,
            "severity": severity,
            "evidence_paths": sorted(set(evidence_paths)),
            "sources": sorted(set(sources)),
            "message": message,
        }
    )


def _review_level(value: str | None) -> str:
    status = value.casefold() if value is not None else ""
    if "practice guideline" in status or "expert panel" in status:
        return "expert"
    if (
        "single submitter" in status
        or "no assertion criteria" in status
        or "no classification provided" in status
    ):
        return "low"
    return "other"


def _condition_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def _add_structural_findings(
    evidence: Mapping[str, object],
    findings: list[ConflictFinding],
) -> None:
    variant = _mapping(evidence.get("variant_context"))
    annotations = _mapping(evidence.get("annotations"))
    vep = _mapping(annotations.get("vep"))
    genebe = _mapping(annotations.get("genebe"))
    population = _mapping(annotations.get("population"))
    pathogenicity = _mapping(evidence.get("pathogenicity"))
    clinvar_source = _source_name(
        evidence,
        "pathogenicity.clinvar",
        "NCBI ClinVar",
    )

    transcript_values = {
        path: value
        for path, value in (
            ("variant_context.transcript", _text(variant.get("transcript"))),
            ("annotations.vep.transcript", _text(vep.get("transcript"))),
            (
                "annotations.genebe.transcript",
                _text(genebe.get("transcript")),
            ),
        )
        if value is not None
    }
    if len({value for value in transcript_values.values()}) > 1:
        # Determine whether the mismatch is purely a version-suffix difference
        # (e.g. ENST00000350721.9 vs ENST00000350721) or a genuine switch to a
        # different transcript.  Version-only mismatches are downgraded to
        # "minor" so they do not trigger conflict_aware LLM prompt mode.
        base_ids = {_transcript_base(v) for v in transcript_values.values()}
        version_only = len(base_ids) == 1
        _add_finding(
            findings,
            conflict_type="transcript_mismatch",
            severity="minor" if version_only else "major",
            evidence_paths=list(transcript_values),
            sources=["Variant context", "VEP", "GeneBe"],
            message=(
                "Transcript version suffixes differ across retained evidence "
                "(same base transcript, annotation currency difference only)."
                if version_only
                else
                "Transcript identifiers disagree across retained evidence."
            ),
        )

    assembly_values = {
        path: value
        for path, value in (
            ("variant_context.assembly", _text(variant.get("assembly"))),
            ("annotations.vep.assembly", _text(vep.get("assembly"))),
            (
                "annotations.genebe.request_assembly",
                _text(genebe.get("request_assembly")),
            ),
            (
                "annotations.population.assembly",
                _text(population.get("assembly")),
            ),
        )
        if value is not None
    }
    if len({value for value in assembly_values.values()}) > 1:
        _add_finding(
            findings,
            conflict_type="assembly_mismatch",
            severity="critical",
            evidence_paths=list(assembly_values),
            sources=["Variant context", "VEP", "GeneBe", "MyVariant.info"],
            message="Genome assemblies disagree across retained evidence.",
        )

    derived = _mapping(genebe.get("clinvar_derived"))
    direct_conditions = _condition_values(
        pathogenicity.get("clinvar_conditions")
    )
    derived_disease = _text(derived.get("disease"))
    if direct_conditions and derived_disease is not None:
        direct_keys = {_term_key(item) for item in direct_conditions}
        if _term_key(derived_disease) not in direct_keys:
            _add_finding(
                findings,
                conflict_type="condition_mismatch",
                severity="moderate",
                evidence_paths=[
                    "pathogenicity.clinvar_conditions",
                    "annotations.genebe.clinvar_derived.disease",
                ],
                sources=[clinvar_source, "GeneBe"],
                message=(
                    "Condition labels do not overlap across ClinVar evidence."
                ),
            )

    clinvar_conflict = _mapping(
        pathogenicity.get("clinvar_conflicting_submissions")
    )
    if (
        clinvar_conflict.get("detected") is True
        or clinvar_conflict.get("status") == "conflicting"
    ):
        _add_finding(
            findings,
            conflict_type="clinvar_conflicting_submissions",
            severity="major",
            evidence_paths=[
                "pathogenicity.clinvar_conflicting_submissions"
            ],
            sources=[clinvar_source],
            message="ClinVar explicitly reports conflicting submissions.",
        )


def _add_lineage_and_freshness_findings(
    evidence: Mapping[str, object],
    findings: list[ConflictFinding],
) -> None:
    provenance = _mapping(evidence.get("provenance"))
    shared_groups = provenance.get("shared_upstream_groups")
    if isinstance(shared_groups, list):
        for group in shared_groups:
            if not isinstance(group, dict):
                continue
            upstream = _text(group.get("upstream_source"))
            paths = _condition_values(group.get("evidence_paths"))
            providers = _condition_values(group.get("providers"))
            if upstream is None or len(paths) < 2:
                continue
            _add_finding(
                findings,
                conflict_type="upstream_dependency",
                severity="minor",
                evidence_paths=paths,
                sources=providers,
                message=(
                    f"Multiple evidence paths share {upstream} and count "
                    "as one independent upstream vote."
                ),
            )

    timestamps: list[tuple[datetime, str, str]] = []
    lineage = provenance.get("lineage")
    if isinstance(lineage, list):
        for record in lineage:
            if (
                not isinstance(record, dict)
                or record.get("evidence_present") is not True
            ):
                continue
            timestamp = _text(record.get("retrieved_at"))
            path = _text(record.get("evidence_path"))
            provider = _text(record.get("provider"))
            if timestamp is None or path is None or provider is None:
                continue
            try:
                parsed = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                timestamps.append((parsed, path, provider))
    if timestamps:
        oldest = min(timestamps, key=lambda item: item[0])
        newest = max(timestamps, key=lambda item: item[0])
        if (newest[0] - oldest[0]).days >= STALE_EVIDENCE_DAYS:
            _add_finding(
                findings,
                conflict_type="stale_evidence",
                severity="moderate",
                evidence_paths=[oldest[1], newest[1]],
                sources=[oldest[2], newest[2]],
                message=(
                    "Retained source retrieval times differ by at least "
                    f"{STALE_EVIDENCE_DAYS} days."
                ),
            )


def _add_gene_context_finding(
    evidence: Mapping[str, object],
    findings: list[ConflictFinding],
) -> None:
    variant = _mapping(evidence.get("variant_context"))
    phenotype = _mapping(evidence.get("phenotype_relationship"))
    phen2gene = _mapping(phenotype.get("phen2gene"))
    mydisease = _mapping(phenotype.get("mydisease"))
    expected_gene = _text(variant.get("gene"))
    values = {
        path: gene
        for path, gene in (
            ("variant_context.gene", expected_gene),
            (
                "phenotype_relationship.phen2gene.gene",
                _text(phen2gene.get("gene")),
            ),
            (
                "phenotype_relationship.mydisease.query_gene",
                _text(mydisease.get("query_gene")),
            ),
        )
        if gene is not None
    }
    mismatch_paths = (
        list(values)
        if len({gene.casefold() for gene in values.values()}) > 1
        else []
    )
    query_gene_id = _text(mydisease.get("query_gene_id"))
    diseases = mydisease.get("diseases")
    if isinstance(diseases, list):
        for index, disease in enumerate(diseases):
            if not isinstance(disease, dict):
                continue
            relation = _mapping(disease.get("gene_disease_relation"))
            if relation.get("is_direct") is not True:
                continue
            requested = _text(relation.get("requested_gene_id"))
            matched = _text(relation.get("matched_gene_id"))
            if (
                requested is not None
                and matched is not None
                and requested.casefold() != matched.casefold()
            ):
                mismatch_paths.extend(
                    [
                        (
                            "phenotype_relationship.mydisease.diseases"
                            f"[{index}].gene_disease_relation."
                            "requested_gene_id"
                        ),
                        (
                            "phenotype_relationship.mydisease.diseases"
                            f"[{index}].gene_disease_relation."
                            "matched_gene_id"
                        ),
                    ]
                )
            if (
                query_gene_id is not None
                and matched is not None
                and query_gene_id.split(":", 1)[0].casefold()
                == matched.split(":", 1)[0].casefold()
                and query_gene_id.casefold() != matched.casefold()
            ):
                mismatch_paths.append(
                    "phenotype_relationship.mydisease.query_gene_id"
                )
    if mismatch_paths:
        _add_finding(
            findings,
            conflict_type="gene_disease_context_mismatch",
            severity="major",
            evidence_paths=mismatch_paths,
            sources=["Variant context", "Phen2Gene", "MyDisease.info"],
            message="Gene identifiers disagree across phenotype context.",
        )


def _add_user_override_finding(
    evidence: Mapping[str, object],
    classifications: list[NormalizedClassification],
    reviewed_values: Mapping[str, object],
    findings: list[ConflictFinding],
) -> None:
    override_paths: list[str] = []
    edited_paths = reviewed_values.get("edited_evidence_paths")
    if isinstance(edited_paths, list):
        override_paths.extend(
            path
            for path in edited_paths
            if isinstance(path, str) and path
        )
    classification = normalize_classification_label(
        reviewed_values.get("classification")
    )
    if classification is not None and any(
        record["normalized_label"] != classification
        for record in classifications
    ):
        override_paths.append("reviewed_values.classification")

    variant = _mapping(evidence.get("variant_context"))
    for field in ("assembly", "transcript", "gene"):
        reviewed = _text(reviewed_values.get(field))
        original = _text(variant.get(field))
        if (
            reviewed is not None
            and original is not None
            and reviewed.casefold() != original.casefold()
        ):
            override_paths.append(f"reviewed_values.{field}")

    reviewed_conditions = _condition_values(
        reviewed_values.get("conditions")
    )
    source_conditions = _condition_values(
        _mapping(evidence.get("pathogenicity")).get(
            "clinvar_conditions"
        )
    )
    if reviewed_conditions and source_conditions:
        reviewed_keys = {_term_key(item) for item in reviewed_conditions}
        source_keys = {_term_key(item) for item in source_conditions}
        if reviewed_keys.isdisjoint(source_keys):
            override_paths.append("reviewed_values.conditions")

    if override_paths:
        _add_finding(
            findings,
            conflict_type="user_override_conflict",
            severity="major",
            evidence_paths=list(dict.fromkeys(override_paths)),
            sources=["Human review", "Original source evidence"],
            message="Reviewed values conflict with preserved source evidence.",
        )


def audit_evidence_conflicts(
    evidence: Mapping[str, object],
    *,
    phase: AuditPhase = "pre_review",
    reviewed_values: Mapping[str, object] | None = None,
) -> ConflictAuditResult:
    """Run deterministic conflict checks without assigning a final class."""

    if not isinstance(evidence, Mapping):
        raise ConflictAuditError("Evidence must be a mapping.")
    if phase not in {"pre_review", "post_review"}:
        raise ConflictAuditError("Audit phase is unsupported.")
    if reviewed_values is not None and not isinstance(
        reviewed_values,
        Mapping,
    ):
        raise ConflictAuditError("Reviewed values must be a mapping.")

    classifications = _classification_records(evidence)
    findings: list[ConflictFinding] = []
    labels = {item["normalized_label"] for item in classifications}
    if len(labels) > 1:
        _add_finding(
            findings,
            conflict_type="classification_disagreement",
            severity="major",
            evidence_paths=[
                item["evidence_path"] for item in classifications
            ],
            sources=[item["source"] for item in classifications],
            message="Normalized five-class source assertions disagree.",
        )

    clinvar_records = [
        item
        for item in classifications
        if "clinvar" in item["evidence_path"]
    ]
    review_statuses = {
        _term_key(item["review_status"])
        for item in clinvar_records
        if item["review_status"] is not None
    }
    if len(review_statuses) > 1:
        _add_finding(
            findings,
            conflict_type="review_status_mismatch",
            severity="minor",
            evidence_paths=[
                item["evidence_path"] for item in clinvar_records
            ],
            sources=[item["source"] for item in clinvar_records],
            message="ClinVar-derived review statuses disagree.",
        )
    if len(labels) > 1:
        levels = {
            _review_level(item["review_status"])
            for item in classifications
        }
        if {"expert", "low"}.issubset(levels):
            _add_finding(
                findings,
                conflict_type="source_quality_mismatch",
                severity="moderate",
                evidence_paths=[
                    item["evidence_path"] for item in classifications
                ],
                sources=[item["source"] for item in classifications],
                message=(
                    "A high-review assertion conflicts with a low-review "
                    "assertion."
                ),
            )

    _add_structural_findings(evidence, findings)
    _add_lineage_and_freshness_findings(evidence, findings)
    _add_gene_context_finding(evidence, findings)
    if phase == "post_review" and reviewed_values is not None:
        _add_user_override_finding(
            evidence,
            classifications,
            reviewed_values,
            findings,
        )

    routing_severity = max(
        (finding["severity"] for finding in findings),
        key=lambda severity: SEVERITY_ORDER[severity],
        default="none",
    )
    return {
        "phase": phase,
        "status": "conflict" if findings else "no_conflict",
        "routing_severity": routing_severity,
        "findings": findings,
        "normalized_classifications": classifications,
        "final_classification": None,
    }


__all__ = [
    "ConflictAuditError",
    "ConflictAuditResult",
    "audit_evidence_conflicts",
    "normalize_classification_label",
]
