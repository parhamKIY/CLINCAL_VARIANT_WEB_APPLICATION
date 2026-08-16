"""Stage 7 runtime-only semantic Evidence Coverage Calculator."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any


COVERAGE_SCHEMA_VERSION = "1.0"
COVERAGE_STATES = frozenset(
    {"FULL", "DEGRADED", "UNAVAILABLE", "NOT_TRIGGERED", "NOT_APPLICABLE"}
)
SEMANTIC_TARGETS = (
    "annotation",
    "automated_acmg_context",
    "expert_curated_variant_context",
    "clinvar_clinical_evidence",
    "cspec_context",
    "gene_disease_validity",
    "gene_disease_support",
    "phenotype_gene_ranking",
    "phenotype_gene_support",
    "disease_hpo_context",
    "population_evidence",
    "literature_evidence",
)


class EvidenceCoverageError(ValueError):
    """Raised when an observational coverage result is malformed."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _unique(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text is not None and text not in result:
            result.append(text)
    return result


def _source(payload: Mapping[str, object], fallback: str) -> str:
    return _text(payload.get("provider")) or fallback


def _retrieval_state(payload: Mapping[str, object]) -> str:
    explicit = _text(payload.get("retrieval_state"))
    if explicit is not None:
        return explicit
    status = _text(payload.get("status")) or _text(payload.get("availability"))
    if status in {"not_found", "no_match"}:
        return "no_match"
    if status in {"not_triggered", "skipped", "not_needed"}:
        return "not_triggered"
    if status in {"not_applicable", "unsupported", "missing_identifier"}:
        return "not_applicable"
    if status in {"success", "partial", "available", "found"}:
        return "success"
    if status is not None:
        return "operational_failure"
    return "unavailable"


def _state(*, evidence_present: bool, retrieval_states: Iterable[str]) -> str:
    states = set(retrieval_states)
    if evidence_present:
        return "FULL"
    if states and states.issubset({"not_applicable", "unattempted", "not_needed", "prerequisite_not_met"}):
        return "NOT_APPLICABLE"
    if states and states.issubset({"not_triggered"}):
        return "NOT_TRIGGERED"
    return "UNAVAILABLE"


def _record(
    *,
    semantic_target: str,
    state: str,
    evidence_paths: Iterable[str],
    retrieval_states: Iterable[str],
    composition_state: str = "none",
    sources: Iterable[str] = (),
    limitations: Iterable[str] = (),
    missing_semantic_targets: Iterable[str] = (),
) -> dict[str, Any]:
    if state not in COVERAGE_STATES:
        raise EvidenceCoverageError("Coverage state is invalid.")
    return {
        "semantic_target": semantic_target,
        "state": state,
        "evidence_paths": _unique(evidence_paths),
        "retrieval_states": _unique(retrieval_states),
        "composition_state": composition_state,
        "sources": _unique(sources),
        "correlation_groups": [],
        "limitations": _unique(limitations),
        "missing_semantic_targets": _unique(missing_semantic_targets),
    }


def _annotation(evidence: Mapping[str, object]) -> dict[str, Any]:
    context = _mapping(evidence.get("variant_context"))
    present = {
        "gene": _text(evidence.get("gene")) is not None,
        "transcript": _text(evidence.get("transcript")) is not None,
        "hgvs_c": _text(context.get("hgvs_c")) is not None,
        "hgvs_p": _text(context.get("hgvs_p")) is not None,
        "consequence": _text(evidence.get("consequence")) is not None,
    }
    promotion = _mapping(evidence.get("annotation_promotion"))
    promotion_fields = _mapping(promotion.get("fields"))
    annotations = _mapping(evidence.get("annotations"))
    vep = _mapping(annotations.get("vep"))
    promoted_sources = [
        _mapping(value).get("source")
        for value in promotion_fields.values()
    ]
    active_count = sum(present.values())
    composition_state = "composed" if promotion_fields else "direct" if active_count else "none"
    retrieval_states = [_retrieval_state(vep)]
    if promotion_fields:
        retrieval_states.append(_text(promotion.get("vep_state")) or "composed")
    if active_count == len(present):
        state = "FULL"
    elif active_count:
        state = "DEGRADED"
    else:
        state = _state(evidence_present=False, retrieval_states=retrieval_states)
    missing = [name for name, is_present in present.items() if not is_present]
    sources = promoted_sources if promotion_fields else [_source(vep, "Ensembl VEP")]
    return _record(
        semantic_target="annotation",
        state=state,
        evidence_paths=[
            path
            for name, path in (
                ("gene", "gene"),
                ("transcript", "transcript"),
                ("hgvs_c", "variant_context.hgvs_c"),
                ("hgvs_p", "variant_context.hgvs_p"),
                ("consequence", "consequence"),
            )
            if present[name]
        ],
        retrieval_states=retrieval_states,
        composition_state=composition_state,
        sources=sources,
        limitations=(
            ["active_annotation_fields_missing"] if missing else []
        ),
        missing_semantic_targets=missing,
    )


def _context_record(
    *,
    semantic_target: str,
    payload: Mapping[str, object],
    evidence_path: str,
    fallback_source: str,
    present: bool,
    not_applicable: bool = False,
) -> dict[str, Any]:
    retrieval = _retrieval_state(payload)
    state = "NOT_APPLICABLE" if not_applicable else _state(
        evidence_present=present,
        retrieval_states=[retrieval],
    )
    return _record(
        semantic_target=semantic_target,
        state=state,
        evidence_paths=[evidence_path] if present else [],
        retrieval_states=[retrieval],
        sources=[_source(payload, fallback_source)],
        limitations=[] if present or state == "NOT_APPLICABLE" else ["semantic_evidence_unavailable"],
        missing_semantic_targets=[] if present or state == "NOT_APPLICABLE" else [semantic_target],
    )


def _records_present(payload: Mapping[str, object]) -> bool:
    records = payload.get("records")
    return isinstance(records, list) and bool(records)


def _provider_retrieval_states(payload: Mapping[str, object]) -> list[str]:
    providers = _mapping(payload.get("providers"))
    states = [
        _retrieval_state(_mapping(provider))
        for provider in providers.values()
    ]
    return states or [_retrieval_state(payload)]


def _article_sources(payload: Mapping[str, object]) -> list[str]:
    sources: list[object] = []
    articles = payload.get("articles")
    if isinstance(articles, list):
        for article in articles:
            sources.extend(_mapping(article).get("source_providers", []))
    return _unique(sources) or [_source(payload, "literature evidence")]


def _contexts(evidence: Mapping[str, object]) -> list[dict[str, Any]]:
    pathogenicity = _mapping(evidence.get("pathogenicity"))
    phenotype = _mapping(evidence.get("phenotype_relationship"))
    annotations = _mapping(evidence.get("annotations"))
    enrichment = _mapping(evidence.get("conditional_enrichment"))
    source_statuses = _mapping(evidence.get("source_statuses"))
    genebe = _mapping(annotations.get("genebe"))
    erepo = _mapping(pathogenicity.get("expert_curated_variant_context"))
    medgen_gene = _mapping(pathogenicity.get("medgen_gene_disease_context"))
    phen2gene = _mapping(phenotype.get("phen2gene"))
    medgen_phenotype = phenotype.get("medgen_phenotype_gene_context")
    medgen_phenotype_items = [
        item for item in medgen_phenotype if isinstance(item, Mapping)
    ] if isinstance(medgen_phenotype, list) else []
    mydisease = _mapping(phenotype.get("mydisease"))
    medgen_disease = _mapping(phenotype.get("medgen_disease_hpo_context"))
    population = _mapping(enrichment.get("population_frequency"))
    literature = _mapping(enrichment.get("literature"))

    automated_present = (
        _text(pathogenicity.get("automated_acmg_classification")) is not None
        or bool(pathogenicity.get("acmg_criteria"))
    )
    clinvar_present = (
        _text(pathogenicity.get("clinvar_classification")) is not None
        or _text(evidence.get("clinvar_accession")) is not None
    )
    cspec_context = pathogenicity.get("cspec_context")
    cspec_present = isinstance(cspec_context, list) and bool(cspec_context)
    validity_present = bool(pathogenicity.get("clingen_context"))
    hpo_terms = phenotype.get("patient_hpo_terms")
    has_hpo = isinstance(hpo_terms, list) and bool(hpo_terms)
    ranking_present = bool(phen2gene)
    support_present = (
        bool(phenotype.get("matched_patient_hpo_terms"))
        or phenotype.get("local_phenotype_score") is not None
        or any(_records_present(item) for item in medgen_phenotype_items)
    )
    mydisease_present = bool(mydisease.get("diseases"))
    medgen_disease_present = _records_present(medgen_disease)
    population_present = (
        isinstance(evidence.get("population_frequency"), (int, float))
        and not isinstance(evidence.get("population_frequency"), bool)
    ) or (
        isinstance(population.get("selected_frequency"), (int, float))
        and not isinstance(population.get("selected_frequency"), bool)
    )
    literature_present = isinstance(literature.get("articles"), list) and bool(literature.get("articles"))

    records = [
        _context_record(
            semantic_target="automated_acmg_context",
            payload=genebe,
            evidence_path="pathogenicity.automated_acmg_context",
            fallback_source="GeneBe",
            present=automated_present,
        ),
        _context_record(
            semantic_target="expert_curated_variant_context",
            payload=erepo,
            evidence_path="pathogenicity.expert_curated_variant_context",
            fallback_source="ClinGen ERepo",
            present=_records_present(erepo),
        ),
        _context_record(
            semantic_target="clinvar_clinical_evidence",
            payload={"status": source_statuses.get("clinvar")},
            evidence_path="pathogenicity.clinvar",
            fallback_source="NCBI ClinVar",
            present=clinvar_present,
        ),
        _context_record(
            semantic_target="cspec_context",
            payload={"status": source_statuses.get("cspec")},
            evidence_path="pathogenicity.cspec_context",
            fallback_source="ClinGen CSpec Registry",
            present=cspec_present,
        ),
        _context_record(
            semantic_target="gene_disease_validity",
            payload={"status": source_statuses.get("clingen")},
            evidence_path="pathogenicity.clingen_context",
            fallback_source="ClinGen/GenCC",
            present=validity_present,
        ),
        _context_record(
            semantic_target="gene_disease_support",
            payload=medgen_gene,
            evidence_path="pathogenicity.medgen_gene_disease_context",
            fallback_source="NCBI MedGen",
            present=_records_present(medgen_gene),
        ),
        _context_record(
            semantic_target="phenotype_gene_ranking",
            payload=phen2gene,
            evidence_path="phenotype_relationship.phen2gene",
            fallback_source="Phen2Gene",
            present=ranking_present,
            not_applicable=not has_hpo,
        ),
        _record(
            semantic_target="phenotype_gene_support",
            state=("NOT_APPLICABLE" if not has_hpo else "FULL" if support_present else "UNAVAILABLE"),
            evidence_paths=(
                ["phenotype_relationship.matched_patient_hpo_terms"]
                if bool(phenotype.get("matched_patient_hpo_terms"))
                else []
            ) + (
                ["phenotype_relationship.medgen_phenotype_gene_context"]
                if any(_records_present(item) for item in medgen_phenotype_items)
                else []
            ),
            retrieval_states=(
                [_retrieval_state(item) for item in medgen_phenotype_items]
                or ["local_support" if support_present else "unavailable"]
            ),
            sources=(
                ["local phenotype support"] if phenotype.get("local_phenotype_score") is not None or bool(phenotype.get("matched_patient_hpo_terms")) else []
            ) + [
                _source(item, "NCBI MedGen")
                for item in medgen_phenotype_items
            ],
            limitations=[] if support_present or not has_hpo else ["semantic_evidence_unavailable"],
            missing_semantic_targets=[] if support_present or not has_hpo else ["phenotype_gene_support"],
        ),
        _record(
            semantic_target="disease_hpo_context",
            state=("FULL" if mydisease_present or medgen_disease_present else _state(evidence_present=False, retrieval_states=[_retrieval_state(mydisease), _retrieval_state(medgen_disease)])),
            evidence_paths=(
                ["phenotype_relationship.mydisease"] if mydisease_present else []
            ) + (
                ["phenotype_relationship.medgen_disease_hpo_context"] if medgen_disease_present else []
            ),
            retrieval_states=[_retrieval_state(mydisease), _retrieval_state(medgen_disease)],
            sources=(
                [_source(mydisease, "MyDisease.info")] if mydisease else []
            ) + (
                [_source(medgen_disease, "NCBI MedGen")] if medgen_disease else []
            ),
            limitations=[] if mydisease_present or medgen_disease_present else ["semantic_evidence_unavailable"],
            missing_semantic_targets=[] if mydisease_present or medgen_disease_present else ["disease_hpo_context"],
        ),
        _context_record(
            semantic_target="population_evidence",
            payload=population,
            evidence_path="conditional_enrichment.population_frequency",
            fallback_source="population evidence",
            present=population_present,
        ),
        _context_record(
            semantic_target="literature_evidence",
            payload=literature,
            evidence_path="conditional_enrichment.literature",
            fallback_source="literature evidence",
            present=literature_present,
        ),
    ]
    literature_record = records[-1]
    literature_record["retrieval_states"] = _provider_retrieval_states(literature)
    literature_record["sources"] = _article_sources(literature)
    return records


def _attach_correlation(
    records: list[dict[str, Any]],
    evidence: Mapping[str, object],
) -> None:
    provenance = _mapping(evidence.get("provenance"))
    groups = provenance.get("shared_upstream_groups")
    if not isinstance(groups, list):
        return
    for record in records:
        paths = record["evidence_paths"]
        for group in groups:
            group_data = _mapping(group)
            upstream = _text(group_data.get("upstream_source"))
            group_paths = group_data.get("evidence_paths")
            if upstream is None or not isinstance(group_paths, list):
                continue
            if any(
                path == group_path or path.startswith(f"{group_path}.") or group_path.startswith(f"{path}.")
                for path in paths
                for group_path in group_paths
                if isinstance(group_path, str)
            ) and upstream not in record["correlation_groups"]:
                record["correlation_groups"].append(upstream)


def _overall_state(records: Iterable[Mapping[str, object]]) -> str:
    states = [record.get("state") for record in records]
    applicable = [state for state in states if state != "NOT_APPLICABLE"]
    if not applicable:
        return "NOT_APPLICABLE"
    if all(state == "FULL" for state in applicable):
        return "FULL"
    if any(state in {"FULL", "DEGRADED"} for state in applicable):
        return "DEGRADED"
    if all(state == "NOT_TRIGGERED" for state in applicable):
        return "NOT_TRIGGERED"
    return "UNAVAILABLE"


def build_evidence_coverage(evidence: Mapping[str, object]) -> dict[str, Any]:
    """Derive one bounded coverage record without mutating EvidenceObject."""

    variant = _mapping(evidence.get("variant"))
    if set(variant) != {"chrom", "pos", "ref", "alt"}:
        raise EvidenceCoverageError("Evidence has no complete variant identity.")
    annotation = _annotation(evidence)
    records = [annotation, *_contexts(evidence)]
    _attach_correlation(records, evidence)
    result = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "variant": deepcopy(dict(variant)),
        "overall_state": _overall_state(records),
        "semantic_capabilities": records,
        "missing_semantic_targets": [
            record["semantic_target"]
            for record in records
            if record["state"] in {"DEGRADED", "UNAVAILABLE", "NOT_TRIGGERED"}
        ],
        "limitations": _unique(
            limitation
            for record in records
            for limitation in record["limitations"]
        ),
    }
    return validate_evidence_coverage(result)


def build_evidence_coverages(
    evidence_objects: Iterable[Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Derive one ordered runtime record per EvidenceObject."""

    if isinstance(evidence_objects, (str, bytes, Mapping)):
        raise EvidenceCoverageError("Evidence objects must be an iterable.")
    return [build_evidence_coverage(item) for item in evidence_objects]


def validate_evidence_coverage(value: object) -> dict[str, Any]:
    """Validate the bounded runtime-only Stage 7 representation."""

    item = _mapping(value)
    required = {"schema_version", "variant", "overall_state", "semantic_capabilities", "missing_semantic_targets", "limitations"}
    if set(item) != required or item.get("schema_version") != COVERAGE_SCHEMA_VERSION:
        raise EvidenceCoverageError("Coverage fields are invalid.")
    if item.get("overall_state") not in COVERAGE_STATES:
        raise EvidenceCoverageError("Overall coverage state is invalid.")
    variant = _mapping(item.get("variant"))
    if set(variant) != {"chrom", "pos", "ref", "alt"}:
        raise EvidenceCoverageError("Coverage variant is invalid.")
    records = item.get("semantic_capabilities")
    if not isinstance(records, list) or len(records) != len(SEMANTIC_TARGETS):
        raise EvidenceCoverageError("Coverage capability records are invalid.")
    clean_records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        data = _mapping(record)
        expected = {"semantic_target", "state", "evidence_paths", "retrieval_states", "composition_state", "sources", "correlation_groups", "limitations", "missing_semantic_targets"}
        if set(data) != expected:
            raise EvidenceCoverageError("Coverage capability fields are invalid.")
        target = _text(data.get("semantic_target"))
        if target not in SEMANTIC_TARGETS or target in seen or data.get("state") not in COVERAGE_STATES:
            raise EvidenceCoverageError("Coverage semantic target is invalid.")
        lists = ("evidence_paths", "retrieval_states", "sources", "correlation_groups", "limitations", "missing_semantic_targets")
        if data.get("composition_state") not in {"none", "direct", "composed"} or any(not isinstance(data.get(name), list) for name in lists):
            raise EvidenceCoverageError("Coverage semantic record state is invalid.")
        clean_records.append({name: (list(data[name]) if name in lists else data[name]) for name in expected})
        seen.add(target)
    if seen != set(SEMANTIC_TARGETS):
        raise EvidenceCoverageError("Coverage semantic targets are incomplete.")
    top_level_lists = ("missing_semantic_targets", "limitations")
    if any(not isinstance(item.get(name), list) for name in top_level_lists):
        raise EvidenceCoverageError("Coverage summary lists are invalid.")
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "variant": deepcopy(dict(variant)),
        "overall_state": item["overall_state"],
        "semantic_capabilities": clean_records,
        "missing_semantic_targets": list(item["missing_semantic_targets"]),
        "limitations": list(item["limitations"]),
    }


__all__ = [
    "COVERAGE_SCHEMA_VERSION",
    "COVERAGE_STATES",
    "SEMANTIC_TARGETS",
    "EvidenceCoverageError",
    "build_evidence_coverage",
    "build_evidence_coverages",
    "validate_evidence_coverage",
]
