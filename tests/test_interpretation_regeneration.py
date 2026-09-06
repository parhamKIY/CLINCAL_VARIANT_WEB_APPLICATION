"""Persistent, bounded AI-draft regeneration contract."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from backend.database import (
    load_pipeline_state,
    save_analysis,
    save_pipeline_state,
)
from backend.error_handling import PipelineError, safe_ui_error_message
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
    PROJECT_ROOT,
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


def test_regeneration_uses_a_distinct_auditable_prompt_without_randomness() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    initial_adapter = FakeLLMAdapter(
        LLMResponse(
            content=json.dumps(
                {
                    "ai_classification": "Likely benign",
                    "interpretation": "Initial evidence-based draft.",
                    "conflict_assessment": "No material conflict was identified.",
                    "phenotype_conclusion": "partially supported",
                    "warnings": [],
                }
            ),
            model="initial-model",
            finish_reason="stop",
        )
    )
    prior = interpret_variant(
        evidence,
        client=LLMClient(initial_adapter),
        timestamp="2026-09-03T08:00:00Z",
    )
    regeneration_adapter = FakeLLMAdapter(
        LLMResponse(
            content=json.dumps(
                {
                    "ai_classification": "Likely benign",
                    "interpretation": "Alternative evidence-based draft.",
                    "conflict_assessment": "No material conflict was identified.",
                    "phenotype_conclusion": "partially supported",
                    "warnings": [],
                }
            ),
            model="regeneration-model",
            finish_reason="stop",
        )
    )

    regenerated = regenerate_variant_interpretation(
        evidence,
        prior,
        client=LLMClient(regeneration_adapter),
        timestamp="2026-09-03T08:05:00Z",
    )

    initial_request = initial_adapter.requests[0]
    regeneration_request = regeneration_adapter.requests[0]
    assert regeneration_request.messages != initial_request.messages
    assert regeneration_request.temperature == 0.0
    assert regenerated["prompt_version"] == (
        "variant-interpretation-regeneration-v1.1"
    )
    regeneration_prompt = regeneration_request.messages[1].content
    assert "comparison context only" in regeneration_prompt.casefold()
    serialized_prior = regeneration_prompt.split(
        "BEGIN_PRIOR_VALIDATED_AI_DRAFT\n",
        1,
    )[1].split("\nEND_PRIOR_VALIDATED_AI_DRAFT", 1)[0]
    assert json.loads(serialized_prior) == {
        "ai_classification": "Likely benign",
        "interpretation": "Initial evidence-based draft.",
    }
    assert set(json.loads(serialized_prior)) == {
        "ai_classification",
        "interpretation",
    }


def test_full_generation_history_is_rejected_before_an_llm_request() -> None:
    evidence, current = _generate(
        classification="Uncertain significance",
        narrative="Initial evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )
    for generation in range(5):
        _, replacement = _generate(
            classification="Uncertain significance",
            narrative=f"Alternative draft {generation + 1}.",
            timestamp=f"2026-09-03T08:{10 + generation:02d}:00Z",
        )
        current = retain_prior_interpretation_generation(
            current,
            replacement,
            evidence=evidence,
        )
    adapter = FakeLLMAdapter(LLMTimeoutError("must not be called"))

    with pytest.raises(
        VariantInterpretationError,
        match="history limit",
    ):
        regenerate_variant_interpretation(
            evidence,
            current,
            client=LLMClient(adapter),
        )

    assert adapter.requests == []


def test_historical_success_without_ai_classification_can_be_regenerated() -> None:
    evidence, current = _generate(
        classification="Uncertain significance",
        narrative="Historical interpretation draft.",
        timestamp="2026-09-03T08:00:00Z",
    )
    historical = dict(current)
    historical["schema_version"] = "1.1"
    for field in (
        "ai_classification",
        "phenotype_conclusion",
        "field_validation",
        "generation_history",
    ):
        historical.pop(field)
    adapter = FakeLLMAdapter(
        LLMResponse(
            content=json.dumps(
                {
                    "ai_classification": "Uncertain significance",
                    "interpretation": "Replacement interpretation draft.",
                    "conflict_assessment": "No material conflict was identified.",
                    "phenotype_conclusion": "partially supported",
                    "warnings": [],
                }
            ),
            model="regeneration-model",
            finish_reason="stop",
        )
    )

    regenerated = regenerate_variant_interpretation(
        evidence,
        historical,
        client=LLMClient(adapter),
        timestamp="2026-09-03T08:05:00Z",
    )

    assert regenerated["ai_classification"] == "Uncertain significance"
    prior_block = adapter.requests[0].messages[1].content.split(
        "BEGIN_PRIOR_VALIDATED_AI_DRAFT\n",
        1,
    )[1].split("\nEND_PRIOR_VALIDATED_AI_DRAFT", 1)[0]
    assert json.loads(prior_block)["ai_classification"] is None


def test_ui_discloses_that_regeneration_replaces_both_draft_fields() -> None:
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = (
        Stage40FrontendReviewWorkflow._draft_result()
    )
    app.run(timeout=10)

    button_labels = [button.label for button in app.button]
    assert "Regenerate AI classification and interpretation" in button_labels
    assert any(
        "classification and interpretation" in caption.value.casefold()
        and "providers are not rerun" in caption.value.casefold()
        for caption in app.caption
    )


def test_ui_regeneration_failure_hides_internal_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "frontend.evidence_review.regenerate_successful_variant_interpretation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PipelineError("secret internal provider detail")
        ),
    )
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = (
        Stage40FrontendReviewWorkflow._draft_result()
    )
    app.run(timeout=10)

    next(
        button
        for button in app.button
        if button.label == "Regenerate AI classification and interpretation"
    ).click().run(timeout=10)

    assert app.error
    assert all("secret internal" not in error.value for error in app.error)
    assert any("current AI draft was preserved" in error.value for error in app.error)


def test_ui_disables_regeneration_when_bounded_history_is_full() -> None:
    current = Stage40FrontendReviewWorkflow._draft_result()
    for generation in range(5):
        adapter = FakeLLMAdapter(
            LLMResponse(
                content=json.dumps(
                    {
                        "ai_classification": "Uncertain significance",
                        "interpretation": f"Alternative draft {generation + 1}.",
                        "conflict_assessment": (
                            "No material conflict was identified."
                        ),
                        "phenotype_conclusion": "partially supported",
                        "warnings": [],
                    }
                ),
                model="history-limit-model",
                finish_reason="stop",
            )
        )
        current = regenerate_successful_variant_interpretation(
            current,
            variant_index=0,
            client=LLMClient(adapter),
            timestamp=f"2026-09-03T08:{10 + generation:02d}:00Z",
        )
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = current
    app.run(timeout=10)

    regenerate = next(
        button
        for button in app.button
        if button.label == "Regenerate AI classification and interpretation"
    )
    assert regenerate.disabled
    assert any("history limit" in caption.value.casefold() for caption in app.caption)


def test_regeneration_ui_error_mapping_is_actionable_and_safe() -> None:
    message = safe_ui_error_message(
        PipelineError("secret internal provider detail"),
        context="interpretation_regeneration",
    )

    assert "current AI draft was preserved" in message
    assert "secret internal" not in message
