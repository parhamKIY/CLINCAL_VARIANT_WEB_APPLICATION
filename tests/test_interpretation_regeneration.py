"""Persistent, bounded AI-draft regeneration contract."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.database import (
    load_pipeline_state,
    save_analysis,
    save_pipeline_state,
)
from backend.error_handling import PipelineError
from backend.llm import LLMClient, LLMResponse, LLMTimeoutError
from backend.pipeline import (
    regenerate_successful_variant_interpretation,
    update_draft_variant_report,
)
from backend.variant_interpretation import (
    VariantInterpretationError,
    interpret_variant,
    regenerate_variant_interpretation,
    retain_prior_interpretation_generation,
    validate_variant_interpretation_result,
)
from backend.variant_report import set_draft_variant_report_inclusion
from test_pipeline import (
    FakeLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    TestStage40FrontendReviewWorkflow as Stage40FrontendReviewWorkflow,
)


def _generate(
    *,
    classification: str,
    narrative: str,
    timestamp: str,
) -> tuple[dict[str, object], dict[str, object]]:
    evidence = EvidenceFactory._complete_evidence_object()
    response = LLMResponse(
        content=json.dumps(
            {
                "ai_classification": classification,
                "interpretation": narrative,
                "conflict_assessment": "No material source conflict was identified.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="regeneration-contract-model",
        finish_reason="stop",
    )
    result = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(response)),
        timestamp=timestamp,
    )
    return evidence, result


def test_initial_generation_has_empty_persistent_history() -> None:
    _, result = _generate(
        classification="Uncertain significance",
        narrative="Initial evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )

    assert result["schema_version"] == "1.4"
    assert result["generation_history"] == []


def test_prior_successful_draft_is_retained_with_integrity_protection() -> None:
    evidence, prior = _generate(
        classification="Uncertain significance",
        narrative="Initial evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )
    _, replacement = _generate(
        classification="Likely benign",
        narrative="Regenerated evidence-based AI draft.",
        timestamp="2026-09-03T08:05:00Z",
    )

    result = retain_prior_interpretation_generation(
        prior,
        replacement,
        evidence=evidence,
    )

    assert result["ai_classification"] == "Likely benign"
    assert result["interpretation"] == "Regenerated evidence-based AI draft."
    assert len(result["generation_history"]) == 1
    record = result["generation_history"][0]
    assert record["reason"] == "user_requested_regeneration"
    assert record["prior_result"]["ai_classification"] == (
        "Uncertain significance"
    )
    assert record["prior_result"]["interpretation"] == (
        "Initial evidence-based AI draft."
    )

    corrupted = deepcopy(result)
    corrupted["generation_history"][0]["prior_result"]["interpretation"] = (
        "Tampered draft."
    )
    with pytest.raises(
        VariantInterpretationError,
        match="integrity check failed",
    ):
        validate_variant_interpretation_result(corrupted, evidence=evidence)


def test_historical_schema_1_3_loads_without_generation_history() -> None:
    evidence, current = _generate(
        classification="Uncertain significance",
        narrative="Historical evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )
    historical = dict(current)
    historical["schema_version"] = "1.3"
    historical.pop("generation_history")

    normalized = validate_variant_interpretation_result(
        historical,
        evidence=evidence,
    )

    assert normalized["status"] == "success"
    assert normalized["interpretation"] == "Historical evidence-based AI draft."
    assert normalized["generation_history"] == []


def test_pipeline_regenerates_only_ai_draft_from_retained_evidence() -> None:
    current = Stage40FrontendReviewWorkflow._draft_result()
    before = deepcopy(current)
    adapter = FakeLLMAdapter(
        LLMResponse(
            content=json.dumps(
                {
                    "ai_classification": "Likely benign",
                    "interpretation": "A different evidence-based AI draft.",
                    "conflict_assessment": "No material conflict was identified.",
                    "phenotype_conclusion": "partially supported",
                    "warnings": [],
                }
            ),
            model="regenerated-draft-model",
            finish_reason="stop",
        )
    )

    regenerated = regenerate_successful_variant_interpretation(
        current,
        variant_index=0,
        client=LLMClient(adapter),
        timestamp="2026-09-03T08:10:00Z",
    )

    assert current == before
    assert regenerated["evidence_objects"] == before["evidence_objects"]
    assert regenerated["annotations"] == before["annotations"]
    assert len(adapter.requests) == 1
    result = regenerated["variant_interpretation_results"][0]
    assert result["ai_classification"] == "Likely benign"
    assert result["interpretation"] == "A different evidence-based AI draft."
    assert result["generation_history"][0]["prior_result"] == (
        before["variant_interpretation_results"][0]
    )
    report_interpretation = regenerated["draft_variant_reports"][0][
        "machine_original_report"
    ]["variant_interpretation"]
    assert report_interpretation["ai_classification"] == "Likely benign"
    assert report_interpretation["narrative"] == (
        "A different evidence-based AI draft."
    )


def test_failed_regeneration_preserves_prior_valid_draft() -> None:
    current = Stage40FrontendReviewWorkflow._draft_result()
    before = deepcopy(current)

    with pytest.raises(PipelineError, match="prior draft was preserved"):
        regenerate_successful_variant_interpretation(
            current,
            variant_index=0,
            client=LLMClient(
                FakeLLMAdapter(LLMTimeoutError("private provider detail"))
            ),
            max_retries=0,
            timestamp="2026-09-03T08:10:00Z",
        )

    assert current == before


def test_direct_regeneration_requires_a_successful_prior_result() -> None:
    evidence, successful = _generate(
        classification="Uncertain significance",
        narrative="Initial evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )
    failed = deepcopy(successful)
    failed.update(
        {
            "status": "failed",
            "response_model": None,
            "ai_classification": None,
            "interpretation": None,
            "conflict_assessment": None,
            "warnings": [],
            "phenotype_conclusion": None,
            "cited_reference_ids": [],
            "field_validation": {
                key: "not_available"
                for key in successful["field_validation"]
            },
            "usage": None,
            "error_type": "request_timeout",
        }
    )

    with pytest.raises(
        VariantInterpretationError,
        match="Only a successful interpretation",
    ):
        regenerate_variant_interpretation(
            evidence,
            failed,
            client=LLMClient(
                FakeLLMAdapter(LLMTimeoutError("must not be called"))
            ),
        )


def test_regeneration_cannot_overwrite_a_reviewer_decision() -> None:
    current = Stage40FrontendReviewWorkflow._draft_result()
    reviewed_report = set_draft_variant_report_inclusion(
        current["draft_variant_reports"][0],
        False,
        timestamp="2026-09-03T08:05:00Z",
    )
    reviewed = update_draft_variant_report(current, reviewed_report)
    adapter = FakeLLMAdapter(LLMTimeoutError("must not be called"))

    with pytest.raises(PipelineError, match="reviewer decisions"):
        regenerate_successful_variant_interpretation(
            reviewed,
            variant_index=0,
            client=LLMClient(adapter),
            timestamp="2026-09-03T08:10:00Z",
        )

    assert adapter.requests == []


def test_regeneration_history_survives_pipeline_persistence(tmp_path) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    current = Stage40FrontendReviewWorkflow._draft_result()
    analysis = save_analysis(status="success", database_path=database_path)
    current["analysis_id"] = analysis["analysis_id"]
    adapter = FakeLLMAdapter(
        LLMResponse(
            content=json.dumps(
                {
                    "ai_classification": "Likely benign",
                    "interpretation": "Persisted regenerated AI draft.",
                    "conflict_assessment": "No material conflict was identified.",
                    "phenotype_conclusion": "partially supported",
                    "warnings": [],
                }
            ),
            model="regenerated-draft-model",
            finish_reason="stop",
        )
    )
    regenerated = regenerate_successful_variant_interpretation(
        current,
        variant_index=0,
        client=LLMClient(adapter),
        timestamp="2026-09-03T08:10:00Z",
    )

    save_pipeline_state(regenerated, database_path=database_path)
    restored = load_pipeline_state(
        analysis["analysis_id"],
        database_path=database_path,
    )

    assert restored == regenerated
    assert len(
        restored["variant_interpretation_results"][0]["generation_history"]
    ) == 1
