"""Stage 9.2.4 controlled draft-classification acceptance scenarios."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.llm import LLMClient, LLMResponse
from backend.report import build_evidence_object, validate_evidence_object
from backend.report_data_projection import build_report_data_from_draft
from backend.variant_interpretation import interpret_variant
from backend.variant_report import build_draft_variant_report
from frontend.report_preview import render_draft_report_preview_pages
from test_pipeline import FakeLLMAdapter, TestEvidenceObject as EvidenceFactory


def _genebe_source(classification: str, criteria: list[str]) -> dict[str, object]:
    return {
        "status": "success",
        "provider": "GeneBe",
        "provider_version": "calibration-test",
        "retrieved_at": "2026-08-24T00:00:00Z",
        "gene": "SCN1A",
        "gene_hgnc_id": 10585,
        "transcript": "ENST00000303395",
        "automated_acmg_classification": classification,
        "automated_acmg_criteria": criteria,
        "automated_acmg_score": (
            10.0 if classification == "Pathogenic" else -8.0
        ),
        "population_annotations": {},
        "predictor_annotations": {},
        "clinvar_derived": {},
    }


def _scenario(kind: str) -> dict[str, object]:
    candidate = EvidenceFactory._pipeline_candidate()
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    clinvar = sources["clinvar"]
    assert isinstance(clinvar, dict)

    if kind == "pathogenic":
        sources["genebe"] = _genebe_source(
            "Pathogenic",
            ["PVS1", "PS3"],
        )
    elif kind == "conflict":
        sources["genebe"] = _genebe_source("Benign", ["BA1", "BS1"])
    elif kind == "benign":
        candidate["population_frequency"] = 0.2
        candidate["consequence"] = "synonymous_variant"
        candidate["impact"] = "LOW"
        candidate["protein_change"] = None
        clinvar["clinical_significance"] = "Benign"
        sources["genebe"] = _genebe_source("Benign", ["BA1", "BS1"])
    elif kind == "insufficient":
        candidate["population_frequency"] = None
        candidate["consequence"] = None
        candidate["impact"] = None
        candidate["protein_change"] = None
        sources["clinvar"] = {"status": "not_found"}
    else:
        raise AssertionError(f"Unknown calibration scenario: {kind}")

    return validate_evidence_object(build_evidence_object(candidate))


def _response(classification: str) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": classification,
                "interpretation": (
                    "The draft synthesis follows the supplied source evidence."
                ),
                "conflict_assessment": (
                    "Any source disagreement remains explicit for human review."
                ),
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="stage9-2-4-calibration-model",
        finish_reason="stop",
    )


@pytest.mark.parametrize(
    (
        "kind",
        "ai_classification",
        "clinvar_classification",
        "automated_classification",
        "conflict_status",
        "prompt_mode",
    ),
    [
        (
            "pathogenic",
            "Likely pathogenic",
            "Pathogenic",
            "Pathogenic",
            "none",
            "standard",
        ),
        (
            "conflict",
            "Uncertain significance",
            "Pathogenic",
            "Benign",
            "conflict",
            "conflict_aware",
        ),
        (
            "benign",
            "Benign",
            "Benign",
            "Benign",
            "none",
            "standard",
        ),
        (
            "insufficient",
            "Uncertain significance",
            None,
            None,
            "none",
            "standard",
        ),
    ],
)
def test_controlled_classification_scenarios_preserve_source_context(
    kind: str,
    ai_classification: str,
    clinvar_classification: str | None,
    automated_classification: str | None,
    conflict_status: str,
    prompt_mode: str,
) -> None:
    evidence = _scenario(kind)
    original_evidence = deepcopy(evidence)
    interpretation = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(_response(ai_classification))),
        timestamp="2026-08-24T00:00:00Z",
    )
    draft = build_draft_variant_report(evidence, interpretation, variant_index=0)
    report_data = build_report_data_from_draft(
        draft,
        analysis_id=f"analysis-{'4' * 32}",
    )
    preview = render_draft_report_preview_pages(draft)[0]

    assert evidence == original_evidence
    assert interpretation["status"] == "success"
    assert interpretation["ai_classification"] == ai_classification
    assert interpretation["interpretation"] is not None
    assert interpretation["prompt_mode"] == prompt_mode
    assert report_data["conclusive_result"]["classification"] == (
        ai_classification
    )
    assert report_data["conclusive_result"]["classification_source"] == (
        "LLM draft classification"
    )
    assert report_data["classification_summary"]["clinvar_classification"] == (
        clinvar_classification
    )
    assert report_data["classification_summary"]["automated_classification"] == (
        automated_classification
    )
    assert report_data["classification_summary"]["conflict_status"] == (
        conflict_status
    )
    assert report_data["classification_summary"][
        "independent_acmg_adjudication"
    ] is False
    assert f"System classification: {ai_classification}" in preview
    assert "Source classification context:" in preview
    assert "Brief Interpretation(s)" in preview
