"""Deterministic, provenance-preserving LLM payload slimming tests."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.privacy import validate_llm_payload
from backend.report import build_evidence_object
from backend.shadow_composition import (
    semantic_evidence_for_llm,
    shadow_free_evidence_for_llm,
)
from backend.evidence_readiness import build_evidence_readiness_audit
from backend.variant_interpretation import _build_prompt
from tests.test_pipeline import TestEvidenceObject as EvidenceFactory

pytestmark = pytest.mark.stage59_testing_v3

OPERATIONAL_KEYS = (
    "accepted_uuids",
    "candidate_count",
    "endpoint",
    "query_column",
    "query_key",
    "retrieved_at",
    "strategy_id",
)


def _enriched_candidate() -> dict[str, object]:
    candidate = EvidenceFactory._pipeline_candidate()
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    sources["genebe"] = {
        "status": "success",
        "provider": "GeneBe",
        "provider_version": "1.2",
        "retrieved_at": "2026-08-05T00:00:00Z",
        "gene": "SCN1A",
        "automated_acmg_classification": "Pathogenic",
        "automated_acmg_criteria": ["PS1", "PM2"],
        "population_annotations": {"gnomad_exomes_af": 0.00001},
    }
    sources["mydisease"] = {
        "status": "available",
        "provider": "MyDisease.info",
        "provider_version": "20260720",
        "retrieved_at": "2026-08-05T00:00:00Z",
        "query_gene": "SCN1A",
        "diseases": [
            {
                "disease_id": "MONDO:0012320",
                "disease_name": "familial hemiplegic migraine 3",
                "primary_source": "MONDO",
                "gene_disease_relation": {
                    "association_type": "direct_gene_disease",
                    "upstream_source": "MONDO",
                    "is_direct": True,
                },
            }
        ],
    }
    return candidate


def _base_evidence() -> dict[str, object]:
    return build_evidence_object(_enriched_candidate())


def _slim(evidence: dict[str, object]) -> dict[str, object]:
    return semantic_evidence_for_llm(deepcopy(evidence))


def _semantic_fixture() -> dict[str, object]:
    evidence = _base_evidence()
    mydisease = evidence["phenotype_relationship"]["mydisease"]
    assert isinstance(mydisease, dict)
    mydisease["compaction"] = {
        "applied": True,
        "omitted_item_count": 7,
    }

    provenance = evidence["provenance"]
    assert isinstance(provenance, dict)
    provenance["shared_upstream_groups"] = [
        {
            "upstream_source": "ClinVar",
            "evidence_paths": [
                "annotations.population.myvariant",
                "pathogenicity.clinvar",
            ],
            "providers": ["MyVariant.info", "NCBI ClinVar"],
            "independent_vote_count": 1,
        }
    ]

    capability = evidence["capability_results"]["clinvar_evidence"]
    assert isinstance(capability, dict)
    capability_provenance = capability["provenance"]
    assert isinstance(capability_provenance, dict)
    capability_provenance.update(
        {
            "no_match_reason": "exact_allele_not_found",
            "query_scope": {"assembly": "GRCh38", "match": "exact_allele"},
            "retrieved_at": "2026-08-05T00:00:00Z",
        }
    )
    return evidence


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value), set())
    return set()


def test_only_operational_fields_are_removed_from_llm_payload() -> None:
    evidence = _semantic_fixture()

    slimmed = _slim(evidence)

    assert not (_keys(slimmed) & set(OPERATIONAL_KEYS))


def test_provenance_correlation_and_capability_semantics_survive() -> None:
    evidence = _semantic_fixture()
    slimmed = _slim(evidence)

    provenance = slimmed["provenance"]
    assert provenance["providers"] == evidence["provenance"]["providers"]
    assert provenance["upstream_sources"] == (
        evidence["provenance"]["upstream_sources"]
    )
    assert provenance["versions"] == evidence["provenance"]["versions"]
    assert provenance["shared_upstream_groups"] == (
        evidence["provenance"]["shared_upstream_groups"]
    )
    assert "retrieved_at" not in provenance
    assert all("retrieved_at" not in item for item in provenance["lineage"])

    capability = slimmed["capability_results"]["clinvar_evidence"]
    assert capability["data"]["evidence_path"] == "pathogenicity.clinvar"
    assert capability["data"]["evidence_present"] is True
    assert capability["provenance"]["no_match_reason"] == (
        "exact_allele_not_found"
    )
    assert capability["provenance"]["query_scope"] == {
        "assembly": "GRCh38",
        "match": "exact_allele",
    }
    assert "retrieved_at" not in capability["provenance"]


def test_compaction_limitations_survive_projection() -> None:
    slimmed = _slim(_semantic_fixture())

    assert slimmed["phenotype_relationship"]["mydisease"]["compaction"] == {
        "applied": True,
        "omitted_item_count": 7,
    }


def test_projection_does_not_mutate_persisted_evidence() -> None:
    evidence = _semantic_fixture()
    original = deepcopy(evidence)

    _slim(evidence)

    assert evidence == original


def test_biological_content_and_singular_upstream_source_survive() -> None:
    slimmed = _slim(_base_evidence())

    assert slimmed["clinvar_significance"] == "Pathogenic"
    assert slimmed["consequence"] == "missense_variant"
    annotations = slimmed["annotations"]
    genebe = annotations.get("genebe")
    assert isinstance(genebe, dict)
    assert genebe.get("automated_acmg_classification") == "Pathogenic"
    assert genebe.get("provider_version") == "1.2"
    mydisease = annotations.get("mydisease") or {}
    diseases = mydisease.get("diseases") if isinstance(mydisease, dict) else None
    if isinstance(diseases, list) and diseases:
        relation = diseases[0].get("gene_disease_relation")
        assert isinstance(relation, dict)
        assert relation.get("upstream_source") == "MONDO"


def test_source_attribution_survives_projection() -> None:
    evidence = _base_evidence()
    original = evidence["annotations"]["genebe"]
    assert isinstance(original, dict)

    slimmed = _slim(evidence)
    annotations = slimmed["annotations"]
    genebe = annotations.get("genebe")
    assert isinstance(genebe, dict)

    assert "provider" in genebe
    assert "status" in genebe
    for key, value in original.items():
        if key not in OPERATIONAL_KEYS:
            assert genebe[key] == value


def test_capability_results_keep_semantic_status_and_provenance() -> None:
    evidence = _base_evidence()
    capabilities = evidence.get("capability_results")
    if capabilities:
        slimmed = _slim(evidence)
        projected_capabilities = slimmed.get("capability_results")
        assert isinstance(projected_capabilities, dict)
        for capability in projected_capabilities.values():
            assert isinstance(capability, dict)
            assert "data" in capability
            assert "provenance" in capability
            assert "capability" in capability or not capability


def _mutate_operational_values(value: object) -> None:
    """Change only values that the projection deliberately excludes."""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in OPERATIONAL_KEYS:
                if isinstance(item, str) and item:
                    value[key] = f"{item}|mutated-for-payload-indifference"
                elif isinstance(item, int) and not isinstance(item, bool):
                    value[key] = item + 1
                elif isinstance(item, list):
                    value[key] = [*item, "mutated-for-payload-indifference"]
            _mutate_operational_values(item)
    elif isinstance(value, list):
        for item in value:
            _mutate_operational_values(item)


def test_prompts_ignore_purely_operational_differences() -> None:
    evidence_a = deepcopy(_base_evidence())
    evidence_b = deepcopy(_base_evidence())
    _mutate_operational_values(evidence_b)

    # Readiness is computed upstream by the pipeline and passed separately;
    # use one shared audit so the comparison isolates the prompt payload.
    readiness = build_evidence_readiness_audit(
        deepcopy(evidence_a), variant_index=0
    )

    prompt_a = _build_prompt(
        evidence_a,
        prompt_mode="standard",
        readiness_audit=readiness,
    )
    prompt_b = _build_prompt(
        evidence_b,
        prompt_mode="standard",
        readiness_audit=readiness,
    )
    assert prompt_a == prompt_b


def test_prompts_preserve_compaction_difference() -> None:
    evidence_a = _semantic_fixture()
    evidence_b = deepcopy(evidence_a)
    evidence_b["phenotype_relationship"]["mydisease"]["compaction"][
        "omitted_item_count"
    ] = 8
    readiness = build_evidence_readiness_audit(
        deepcopy(evidence_a), variant_index=0
    )

    prompt_a = _build_prompt(
        evidence_a,
        prompt_mode="standard",
        readiness_audit=readiness,
    )
    prompt_b = _build_prompt(
        evidence_b,
        prompt_mode="standard",
        readiness_audit=readiness,
    )

    assert prompt_a != prompt_b
    assert '"omitted_item_count":7' in prompt_a
    assert '"omitted_item_count":8' in prompt_b


def test_prompt_preserves_shared_upstream_single_vote_warning() -> None:
    evidence = _semantic_fixture()
    readiness = build_evidence_readiness_audit(
        deepcopy(evidence), variant_index=0
    )

    prompt = _build_prompt(
        evidence,
        prompt_mode="standard",
        readiness_audit=readiness,
    )

    assert '"shared_upstream_groups":' in prompt
    assert '"upstream_source":"ClinVar"' in prompt
    assert '"independent_vote_count":1' in prompt


def test_slimmed_payload_passes_privacy_boundary_and_is_smaller() -> None:
    evidence = _semantic_fixture()
    slimmed = _slim(evidence)

    validate_llm_payload(slimmed)
    before = len(
        json.dumps(shadow_free_evidence_for_llm(evidence), ensure_ascii=False).encode()
    )
    after = len(json.dumps(slimmed, ensure_ascii=False).encode())
    assert after < before
