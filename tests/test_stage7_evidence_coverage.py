"""Deterministic Stage 7 runtime Evidence Coverage Calculator contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.evidence_coverage import (
    COVERAGE_SCHEMA_VERSION,
    SEMANTIC_REQUIREMENT_PROFILES,
    build_evidence_coverage,
    build_evidence_coverages,
)
from backend.evidence_readiness import build_evidence_readiness_audit
from backend.report import build_evidence_object
from backend.shadow_composition import shadow_free_evidence_for_llm
from backend.variant_interpretation import _build_prompt
from tests.test_pipeline import TestEvidenceObject as EvidenceFactory


pytestmark = pytest.mark.stage59_testing_v3


def _evidence() -> dict[str, object]:
    return deepcopy(EvidenceFactory._complete_evidence_object())


def _capability(coverage: dict[str, object], name: str) -> dict[str, object]:
    records = coverage["semantic_capabilities"]
    assert isinstance(records, list)
    return next(
        record
        for record in records
        if isinstance(record, dict) and record["semantic_target"] == name
    )


def _annotation_field(record: dict[str, object], field: str) -> dict[str, object]:
    fields = record["annotation_fields"]
    assert isinstance(fields, list)
    return next(
        item
        for item in fields
        if isinstance(item, dict) and item["field"] == field
    )


def test_semantic_requirement_profiles_are_explicit_and_provider_neutral() -> None:
    assert set(SEMANTIC_REQUIREMENT_PROFILES) == {
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
    }
    assert SEMANTIC_REQUIREMENT_PROFILES["annotation"]["critical_fields"] == (
        "gene",
        "transcript",
        "variant_context.hgvs_c",
        "variant_context.hgvs_p",
        "consequence",
    )
    assert all(
        "provider" not in field.lower()
        for profile in SEMANTIC_REQUIREMENT_PROFILES.values()
        for fields in profile.values()
        if isinstance(fields, tuple)
        for field in fields
    )


def test_direct_vep_annotation_is_full_without_provider_counting() -> None:
    evidence = _evidence()
    context = evidence["variant_context"]
    assert isinstance(context, dict)
    context["hgvs_c"] = "NM_000001.5:c.100A>G"
    coverage = build_evidence_coverage(evidence)
    annotation = _capability(coverage, "annotation")

    assert coverage["schema_version"] == COVERAGE_SCHEMA_VERSION
    assert annotation["state"] == "FULL"
    assert annotation["composition_state"] == "direct"
    assert annotation["missing_semantic_targets"] == []
    assert annotation["critical_fields"] == list(
        SEMANTIC_REQUIREMENT_PROFILES["annotation"]["critical_fields"]
    )
    assert annotation["satisfied_fields"] == annotation["critical_fields"]
    assert _annotation_field(annotation, "gene") == {
        "field": "gene",
        "state": "FULL",
        "evidence_path": "gene",
        "composition_state": "direct",
        "promotion_state": "NOT_PROMOTED",
        "source": "Ensembl VEP",
        "limitations": [],
    }


def test_safe_stage6_composition_is_degraded_when_consequence_remains_missing() -> None:
    candidate = EvidenceFactory._pipeline_candidate()
    candidate.update(
        {
            "gene": None,
            "gene_id": None,
            "transcript": None,
            "hgvsc": None,
            "hgvsp": None,
            "protein_change": None,
            "consequence": None,
            "impact": None,
        }
    )
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    sources["vep"] = {
        "status": "error",
        "provider": "Ensembl VEP",
        "primary_failure": "timeout",
    }
    sources["genebe"] = {
        "status": "success",
        "provider": "GeneBe",
        "request_assembly": "GRCh38",
        "returned_variant": {"chrom": "2", "pos": 166848215, "ref": "C", "alt": "T"},
        "gene": "GENE1",
        "transcript": "NM_000001.5",
        "consequences": [
            {
                "gene": "GENE1",
                "transcript": "NM_000001.5",
                "hgvs_c": "NM_000001.5:c.100A>G",
                "hgvs_p": "NP_000001.1:p.(Lys34Arg)",
                "consequence_terms": ["missense_variant"],
            }
        ],
    }
    evidence = build_evidence_object(candidate)
    coverage = build_evidence_coverage(evidence)
    annotation = _capability(coverage, "annotation")

    assert evidence["source_statuses"]["vep"] == "error"
    assert annotation["state"] == "DEGRADED"
    assert annotation["composition_state"] == "composed"
    assert annotation["sources"] == ["GeneBe"]
    assert annotation["missing_semantic_targets"] == ["consequence"]
    assert _annotation_field(annotation, "gene")["promotion_state"] == "PROMOTED"
    assert _annotation_field(annotation, "gene")["source"] == "GeneBe"
    assert _annotation_field(annotation, "consequence")["state"] == "UNAVAILABLE"


def test_partial_direct_annotation_is_degraded_without_relabeling_vep_failure() -> None:
    evidence = _evidence()
    context = evidence["variant_context"]
    assert isinstance(context, dict)
    context["hgvs_c"] = None
    context["hgvs_p"] = None

    annotation = _capability(build_evidence_coverage(evidence), "annotation")

    assert annotation["state"] == "DEGRADED"
    assert annotation["composition_state"] == "direct"
    assert annotation["missing_semantic_targets"] == ["hgvs_c", "hgvs_p"]


def test_only_one_active_annotation_field_is_degraded_not_full() -> None:
    evidence = _evidence()
    evidence.update({"transcript": None, "consequence": None})
    context = evidence["variant_context"]
    assert isinstance(context, dict)
    context.update({"hgvs_c": None, "hgvs_p": None})

    annotation = _capability(build_evidence_coverage(evidence), "annotation")

    assert annotation["state"] == "DEGRADED"
    assert annotation["satisfied_fields"] == ["gene"]


def test_mixed_direct_and_promoted_annotation_is_field_level() -> None:
    evidence = _evidence()
    context = evidence["variant_context"]
    assert isinstance(context, dict)
    context["hgvs_c"] = "NM_000001.5:c.100A>G"
    evidence["annotation_promotion"] = {
        "vep_state": "operational_failure",
        "fields": {
            "hgvs_c": {
                "source": "GeneBe",
                "composition_state": "COMPOSABLE",
                "promotion_state": "PROMOTED",
                "limitations": ["vep_field_insufficient"],
            }
        },
    }

    annotation = _capability(build_evidence_coverage(evidence), "annotation")

    assert annotation["state"] == "FULL"
    assert annotation["composition_state"] == "mixed"
    assert _annotation_field(annotation, "hgvs_c")["source"] == "GeneBe"
    assert _annotation_field(annotation, "gene")["source"] == "Ensembl VEP"


def test_shadow_only_or_valid_no_match_never_counts_as_active_annotation() -> None:
    evidence = _evidence()
    evidence["gene"] = None
    evidence["transcript"] = None
    evidence["protein_change"] = None
    context = evidence["variant_context"]
    assert isinstance(context, dict)
    context.update({"gene": None, "transcript": None, "hgvs_c": None, "hgvs_p": None, "consequence": None})
    evidence["consequence"] = None
    annotations = evidence["annotations"]
    assert isinstance(annotations, dict)
    annotations["vep"] = {"status": "not_found", "provider": "Ensembl VEP"}
    evidence["shadow_composition"] = {"untrusted": "candidate"}

    annotation = _capability(build_evidence_coverage(evidence), "annotation")

    assert annotation["state"] == "UNAVAILABLE"
    assert annotation["composition_state"] == "none"
    assert annotation["retrieval_states"] == ["no_match"]


@pytest.mark.parametrize(
    ("payload", "expected_state", "expected_retrieval"),
    [
        ({"status": "success", "records": []}, "UNAVAILABLE", "success"),
        ({"status": "not_triggered", "records": []}, "NOT_TRIGGERED", "not_triggered"),
        ({"status": "timeout", "records": []}, "UNAVAILABLE", "operational_failure"),
    ],
)
def test_provider_result_states_do_not_fabricate_semantic_coverage(
    payload: dict[str, object],
    expected_state: str,
    expected_retrieval: str,
) -> None:
    evidence = _evidence()
    pathogenicity = evidence["pathogenicity"]
    assert isinstance(pathogenicity, dict)
    pathogenicity["medgen_gene_disease_context"] = payload

    support = _capability(build_evidence_coverage(evidence), "gene_disease_support")

    assert support["state"] == expected_state
    assert support["retrieval_states"] == [expected_retrieval]


def test_medgen_retrieval_states_preserve_true_no_match_and_unverified_candidates() -> None:
    for retrieval_state in ("no_match", "no_verified_gene_association"):
        evidence = _evidence()
        pathogenicity = evidence["pathogenicity"]
        assert isinstance(pathogenicity, dict)
        pathogenicity["medgen_gene_disease_context"] = {
            "status": "no_match",
            "retrieval_state": retrieval_state,
            "records": [],
            "candidate_diagnostics": [{"rejection_reason": "gene_mismatch"}],
            "provider": "NCBI MedGen",
        }
        support = _capability(build_evidence_coverage(evidence), "gene_disease_support")

        assert support["state"] == "UNAVAILABLE"
        assert support["retrieval_states"] == [retrieval_state]
        if retrieval_state == "no_verified_gene_association":
            assert support["diagnostic_paths"] == [
                "pathogenicity.medgen_gene_disease_context.candidate_diagnostics"
            ]
        else:
            assert support["diagnostic_paths"] == []


def test_gencc_validity_and_medgen_support_remain_non_equivalent() -> None:
    evidence = _evidence()
    pathogenicity = evidence["pathogenicity"]
    assert isinstance(pathogenicity, dict)
    pathogenicity["clingen_context"] = []
    pathogenicity["medgen_gene_disease_context"] = {
        "status": "success",
        "retrieval_state": "accepted_records",
        "records": [{"title": "Supporting disease context"}],
        "candidate_diagnostics": [],
        "provider": "NCBI MedGen",
    }
    coverage = build_evidence_coverage(evidence)

    assert _capability(coverage, "gene_disease_validity")["state"] == "UNAVAILABLE"
    assert _capability(coverage, "gene_disease_support")["state"] == "FULL"


def test_phen2gene_ranking_and_medgen_local_support_remain_separate() -> None:
    evidence = _evidence()
    phenotype = evidence["phenotype_relationship"]
    assert isinstance(phenotype, dict)
    phenotype["patient_hpo_terms"] = ["HP:0001250"]
    phenotype["phen2gene"] = {}
    phenotype["matched_patient_hpo_terms"] = ["HP:0001250"]
    phenotype["local_phenotype_score"] = 0.5
    phenotype["medgen_phenotype_gene_context"] = [
        {
            "status": "success",
            "retrieval_state": "accepted_records",
            "records": [{"title": "Supporting context"}],
            "candidate_diagnostics": [],
            "provider": "NCBI MedGen",
        }
    ]
    coverage = build_evidence_coverage(evidence)

    assert _capability(coverage, "phenotype_gene_ranking")["state"] == "UNAVAILABLE"
    assert _capability(coverage, "phenotype_gene_support")["state"] == "FULL"

    phenotype["patient_hpo_terms"] = []
    coverage = build_evidence_coverage(evidence)
    assert _capability(coverage, "phenotype_gene_ranking")["state"] == "NOT_APPLICABLE"
    assert _capability(coverage, "phenotype_gene_support")["state"] == "NOT_APPLICABLE"


def test_disease_hpo_context_keeps_mydisease_and_medgen_paths_separate() -> None:
    evidence = _evidence()
    phenotype = evidence["phenotype_relationship"]
    assert isinstance(phenotype, dict)
    phenotype["mydisease"] = {"diseases": [{"name": "Disease A"}], "status": "success", "provider": "MyDisease.info"}
    phenotype["medgen_disease_hpo_context"] = {
        "status": "success",
        "retrieval_state": "accepted_records",
        "records": [{"title": "Disease B"}],
        "candidate_diagnostics": [],
        "provider": "NCBI MedGen",
    }
    disease = _capability(build_evidence_coverage(evidence), "disease_hpo_context")

    assert disease["state"] == "FULL"
    assert disease["evidence_paths"] == [
        "phenotype_relationship.mydisease.diseases",
        "phenotype_relationship.medgen_disease_hpo_context.records",
    ]


def test_pathogenicity_contexts_remain_separate_and_cspec_is_context_only() -> None:
    evidence = _evidence()
    pathogenicity = evidence["pathogenicity"]
    assert isinstance(pathogenicity, dict)
    pathogenicity.update(
        {
            "automated_acmg_classification": "VUS",
            "acmg_criteria": ["PM2"],
            "expert_curated_variant_context": {"status": "success", "retrieval_state": "accepted_records", "records": [{"ca_id": "CA1"}], "provider": "ClinGen ERepo"},
            "cspec_context": [{"specification_id": "GN001"}],
        }
    )
    coverage = build_evidence_coverage(evidence)

    assert _capability(coverage, "automated_acmg_context")["state"] == "FULL"
    assert _capability(coverage, "expert_curated_variant_context")["state"] == "FULL"
    assert _capability(coverage, "clinvar_clinical_evidence")["state"] == "FULL"
    assert _capability(coverage, "cspec_context")["state"] == "FULL"


def test_population_literature_and_correlation_are_semantic_not_provider_counts() -> None:
    evidence = _evidence()
    enrichment = evidence["conditional_enrichment"]
    assert isinstance(enrichment, dict)
    enrichment["population_frequency"] = {"status": "success", "provider": "UCSC gnomAD", "population_frequency": 0.0}
    enrichment["literature"] = {
        "status": "success",
        "provider": "Europe PMC",
        "articles": [{"pmid": "1"}],
        "providers": {"litvar": {"status": "timeout"}, "europe_pmc": {"status": "success"}, "pubmed": {"status": "not_triggered"}},
    }
    provenance = evidence["provenance"]
    assert isinstance(provenance, dict)
    provenance["shared_upstream_groups"] = [
        {"upstream_source": "ClinVar", "evidence_paths": ["pathogenicity.clinvar", "annotations.population.clinvar_derived"], "providers": ["NCBI ClinVar", "MyVariant.info"], "independent_vote_count": 1},
        {"upstream_source": "gnomAD", "evidence_paths": ["conditional_enrichment.population_frequency"], "providers": ["UCSC gnomAD"], "independent_vote_count": 1},
    ]
    coverage = build_evidence_coverage(evidence)
    population = _capability(coverage, "population_evidence")
    literature = _capability(coverage, "literature_evidence")
    clinvar = _capability(coverage, "clinvar_clinical_evidence")

    assert population["state"] == "FULL"
    assert literature["state"] == "FULL"
    assert literature["sources"] == ["Europe PMC"]
    assert clinvar["correlation_groups"] == ["ClinVar"]
    assert population["correlation_groups"] == ["gnomAD"]


@pytest.mark.parametrize(
    ("population_frequency", "payload", "expected_state", "expected_retrieval"),
    [
        (0.0, {"status": "not_triggered"}, "FULL", "not_triggered"),
        (None, {"status": "success", "population_frequency": 0.01}, "FULL", "success"),
        (None, {"status": "no_match"}, "UNAVAILABLE", "no_match"),
        (None, {"status": "timeout"}, "UNAVAILABLE", "operational_failure"),
    ],
)
def test_population_coverage_distinguishes_direct_fallback_no_match_and_failure(
    population_frequency: float | None,
    payload: dict[str, object],
    expected_state: str,
    expected_retrieval: str,
) -> None:
    evidence = _evidence()
    evidence["population_frequency"] = population_frequency
    enrichment = evidence["conditional_enrichment"]
    assert isinstance(enrichment, dict)
    enrichment["population_frequency"] = payload

    population = _capability(build_evidence_coverage(evidence), "population_evidence")

    assert population["state"] == expected_state
    assert population["retrieval_states"] == [expected_retrieval]


def test_partial_population_evidence_is_degraded_against_its_profile() -> None:
    evidence = _evidence()
    evidence["population_frequency"] = None
    enrichment = evidence["conditional_enrichment"]
    assert isinstance(enrichment, dict)
    enrichment["population_frequency"] = {
        "status": "partial",
        "population_frequency": 0.01,
    }

    population = _capability(build_evidence_coverage(evidence), "population_evidence")

    assert population["state"] == "DEGRADED"
    assert population["satisfied_fields"] == [
        "conditional_enrichment.population_frequency.population_frequency"
    ]


@pytest.mark.parametrize(
    ("articles", "providers", "expected_state", "expected_sources", "expected_retrieval"),
    [
        (
            [{"pmid": "1", "source_providers": ["LitVar2"]}],
            {"litvar": {"status": "available"}},
            "FULL",
            ["LitVar2"],
            ["success"],
        ),
        (
            [{"pmid": "2", "source_providers": ["Europe PMC"]}],
            {
                "litvar": {"status": "timeout"},
                "europe_pmc": {"status": "available"},
            },
            "FULL",
            ["Europe PMC"],
            ["operational_failure", "success"],
        ),
        (
            [],
            {"litvar": {"status": "no_match"}},
            "UNAVAILABLE",
            ["literature evidence"],
            ["no_match"],
        ),
        (
            [],
            {"litvar": {"status": "timeout"}},
            "UNAVAILABLE",
            ["literature evidence"],
            ["operational_failure"],
        ),
    ],
)
def test_literature_coverage_retains_direct_fallback_no_match_and_failure(
    articles: list[dict[str, object]],
    providers: dict[str, dict[str, str]],
    expected_state: str,
    expected_sources: list[str],
    expected_retrieval: list[str],
) -> None:
    evidence = _evidence()
    enrichment = evidence["conditional_enrichment"]
    assert isinstance(enrichment, dict)
    enrichment["literature"] = {"articles": articles, "providers": providers}

    literature = _capability(build_evidence_coverage(evidence), "literature_evidence")

    assert literature["state"] == expected_state
    assert literature["sources"] == expected_sources
    assert literature["retrieval_states"] == expected_retrieval


def test_runtime_derivation_preserves_readiness_prompt_and_order() -> None:
    first = _evidence()
    second = _evidence()
    second["variant"] = {"chrom": "7", "pos": 140453136, "ref": "A", "alt": "T"}
    before = deepcopy(first)
    readiness = build_evidence_readiness_audit(first, variant_index=0)

    coverages = build_evidence_coverages([first, second])

    assert first == before
    assert [coverage["variant"] for coverage in coverages] == [first["variant"], second["variant"]]
    assert build_evidence_readiness_audit(first, variant_index=0) == readiness
    assert _build_prompt(first, prompt_mode="standard", readiness_audit=readiness) == _build_prompt(
        shadow_free_evidence_for_llm(first),
        prompt_mode="standard",
        readiness_audit=readiness,
    )
