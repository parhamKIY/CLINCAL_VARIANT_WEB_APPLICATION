"""Core output safety without mandatory optional provider fields."""
import json
from copy import deepcopy

import pytest

from backend.llm import LLMClient, LLMResponse
from backend.gene_identity import resolve_gene_identity
from backend.interpretation_guardrails import has_assessment_material
from backend.report import build_evidence_object
from backend.variant_interpretation import interpret_variant, interpret_variants
from backend.variant_report import build_draft_variant_report
from backend.report_data_projection import build_report_data_from_draft
from frontend.interpretation_failure_semantics import interpretation_failure_message
from frontend.results import build_partial_analysis_presentation
from frontend.warning_semantics import build_warning_notices
from tests.test_pipeline import FakeLLMAdapter, FakeResponse, SequenceLLMAdapter, TestEvidenceObject as EvidenceFactory
from tests.test_stage12_3_gene_identity_resolution import _annotate, AnnotationFactory


pytestmark = [pytest.mark.stage59_testing_v3, pytest.mark.testing_v3_interpretation]


def _response(classification="Uncertain significance", text="The available evidence remains inconclusive for this variant."):
    return LLMResponse(content=json.dumps({
        "ai_classification": classification, "interpretation": text,
        "conflict_assessment": "The source assertions require review.",
        "phenotype_conclusion": "partially supported", "warnings": [],
    }), model="test-model", finish_reason="stop")


def test_insufficient_evidence_abstention_is_not_forced_to_vus_or_retried():
    evidence = EvidenceFactory._complete_evidence_object()
    adapter = FakeLLMAdapter(_response(None, "The supplied evidence is insufficient for a meaningful classification."))
    fallback = FakeLLMAdapter(_response("Pathogenic"))
    result = interpret_variants([evidence], client=LLMClient(adapter),
                                fallback_client=LLMClient(fallback), fallback_model="fallback")[0]
    assert result["status"] == "failed"
    assert result["error_type"] == "insufficient_evidence"
    assert result["ai_classification"] is None
    assert len(adapter.requests) == 1
    assert not fallback.requests
    draft = build_draft_variant_report(evidence, result, variant_index=0)
    projected = build_report_data_from_draft(draft, analysis_id="analysis-" + "4" * 32)
    assert projected["conclusive_result"]["classification"] is None
    assert "insufficient" in interpretation_failure_message(result["error_type"]).lower()
    notices = build_warning_notices(draft)
    assert any("insufficient" in notice["message"].lower() for notice in notices)
    assert not any("retry interpretation" in notice["message"].lower() for notice in notices)
    presentation = build_partial_analysis_presentation({
        "status": "partial", "variants": [evidence["variant"]],
        "variant_interpretation_results": [result],
    })
    assert presentation["rows"][0]["Failure category"] == "Insufficient evidence"
    assert "additional usable evidence" in presentation["rows"][0]["Next action"]


@pytest.mark.parametrize("text", [".", "[R1]", "Pathogenic", "The variant is classified as benign.",
    "Our draft classification is Likely benign.",
    "The variant is benign based on ACMG evidence.",
    "The supplied evidence is insufficient for a meaningful classification."])
def test_invalid_core_gets_one_repair_instead_of_being_accepted(text):
    adapter = SequenceLLMAdapter([_response("Pathogenic", text), _response()])
    result = interpret_variant(EvidenceFactory._complete_evidence_object(), client=LLMClient(adapter))
    assert result["ai_classification"] == "Uncertain significance"
    assert len(adapter.requests) == 2


@pytest.mark.parametrize("text", [
    "ClinVar reports this variant as benign, but the evidence remains inconclusive.",
    "If the variant is classified as benign in future, the report would change.",
    "The variant is not classified as benign because the evidence remains inconclusive.",
    "The variant is predicted to be pathogenic, but its clinical significance remains uncertain.",
    "The variant is classified as uncertain significance because the sources disagree.",
    "The variant is benign according to ClinVar, but other evidence is inconclusive.",
    "There is insufficient evidence to classify the variant as pathogenic.",
])
def test_source_quotes_predictions_and_uncertainty_are_not_false_conflicts(text):
    adapter = FakeLLMAdapter(_response(text=text))
    result = interpret_variant(EvidenceFactory._complete_evidence_object(), client=LLMClient(adapter))
    assert result["status"] == "success"
    assert len(adapter.requests) == 1


def test_empty_variant_context_does_not_become_a_guess():
    response = deepcopy(AnnotationFactory._vep_response())
    primary = response["transcript_consequences"][0]
    primary["consequence_terms"] = []
    for field in ("sift_score", "sift_prediction", "polyphen_score", "polyphen_prediction"):
        primary.pop(field, None)
    annotation, _ = _annotate(response)
    # Preserve exact identity and gene while removing optional evaluative inputs.
    annotation["consequence"] = None
    annotation["predictors"] = {}
    annotation["hgvsp"] = None
    annotation["protein_change"] = None
    annotation["gene_identity_resolution"] = resolve_gene_identity(annotation)
    evidence = build_evidence_object(annotation)
    adapter = FakeLLMAdapter(_response("Pathogenic"))
    result = interpret_variants([evidence], client=LLMClient(adapter))[0]
    assert result["error_type"] == "insufficient_evidence"
    assert not adapter.requests


@pytest.mark.parametrize("material", [
    {"population_frequency": 0.0},
    {"annotations": {"predictors": {"vep": {"sift_score": 0.0}}}},
    {"conditional_enrichment": {"population_frequency": {"gnomad": {"allele_frequency": 0.0}}}},
    {"pathogenicity": {"clinvar_classification": "Conflicting classifications of pathogenicity"}},
    {"pathogenicity": {"expert_curated_variant_context": {"records": [{"classification": "Likely pathogenic"}]}}},
])
def test_sparse_real_assessment_material_is_not_rejected(material):
    assert has_assessment_material(material)


def test_metadata_case_context_and_missing_numbers_are_not_assessment_material():
    assert not has_assessment_material({
        "gene": "GENE1", "transcript": "NM_000001.2", "population_frequency": None,
        "phenotype_relationship": {"local_phenotype_score": 1.0},
        "annotations": {"predictors": {"provider": "Example", "version": "1.0",
                                        "compaction": {"omitted_item_count": 4}}},
        "conditional_enrichment": {"literature": {"status": "available",
            "articles": [{"pmid": "12345678", "title": "Variant study"}]}},
    })


def test_unrepaired_contradiction_is_variant_local_and_does_not_leak_to_report(caplog):
    evidence = EvidenceFactory._complete_evidence_object()
    bad = _response("Pathogenic", "The variant is classified as benign.")
    adapter = SequenceLLMAdapter([bad, bad, _response()])
    results = interpret_variants([evidence, deepcopy(evidence)], client=LLMClient(adapter))
    assert [result["status"] for result in results] == ["failed", "success"]
    assert results[0]["error_type"] == "output_schema_failure"
    assert "schema_error=classification_narrative_mismatch" in caplog.text
    assert "The variant is classified as benign" not in caplog.text
    assert results[0]["ai_classification"] is None
    assert results[0]["interpretation"] is None
    assert len(adapter.requests) == 3


def test_regeneration_abstention_preserves_prior_draft():
    from backend.error_handling import PipelineError
    from backend.pipeline import regenerate_successful_variant_interpretation
    from tests.test_pipeline import TestStage40FrontendReviewWorkflow

    current = TestStage40FrontendReviewWorkflow._draft_result()
    before = deepcopy(current)
    adapter = FakeLLMAdapter(_response(None, "The supplied evidence is insufficient for a meaningful classification."))
    with pytest.raises(PipelineError, match="prior draft was preserved"):
        regenerate_successful_variant_interpretation(
            current, variant_index=0, client=LLMClient(adapter),
            max_retries=0, timestamp="2026-09-03T08:10:00Z",
        )
    assert current == before
    assert len(adapter.requests) == 1


def test_verified_supporting_protein_context_remains_assessable_without_merging():
    response = deepcopy(AnnotationFactory._vep_response())
    response["transcript_consequences"][0]["hgvsc"] = None
    annotation, _ = _annotate(response, variantvalidator_response=FakeResponse(
        200, AnnotationFactory._variantvalidator_response(),
    ))
    annotation.update(consequence=None, predictors={}, hgvsp=None, protein_change=None)
    annotation["gene_identity_resolution"] = resolve_gene_identity(annotation)
    evidence = build_evidence_object(annotation)
    assert evidence["annotations"]["vep"]["gene_identity_resolution"]["hgvs_p"] is None
    result = interpret_variant(evidence, client=LLMClient(FakeLLMAdapter(_response())))
    assert result["status"] == "success"
