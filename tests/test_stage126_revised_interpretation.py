"""Stage 126 reviewer-requested interpretation version acceptance tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from backend.llm import LLMClient
from backend.pipeline import (
    PipelineError,
    confirm_reviewed_evidence,
    request_revised_variant_interpretation,
    validate_pipeline_result,
)
from backend.variant_interpretation import interpret_variant
from test_pipeline import (
    FakeLLMAdapter,
    TestStage40FrontendReviewWorkflow as DraftFactory,
    TestStage50SingleModelInterpretation as InterpretationFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage126_revised_interpretation


def _revised_interpretation_app() -> None:
    from frontend.evidence_review import _render_revised_interpretation_request
    from test_pipeline import TestStage40FrontendReviewWorkflow

    result = TestStage40FrontendReviewWorkflow._draft_result()
    _render_revised_interpretation_request(
        result["draft_variant_reports"][0],
        result,
        model=None,
    )


def test_review_surface_requires_an_explicit_reinterpretation_request() -> None:
    app = AppTest.from_function(_revised_interpretation_app).run(timeout=10)

    assert not app.exception
    assert [area.label for area in app.text_area] == [
        "Reviewer-approved context or conflict resolution"
    ]
    assert any(
        button.label == "Request revised interpretation"
        for button in app.button
    )


def test_reviewer_context_creates_an_auditable_distinct_version() -> None:
    result = DraftFactory._draft_result()
    initial = deepcopy(result["variant_interpretation_results"])
    drafts = deepcopy(result["draft_variant_reports"])
    adapter = FakeLLMAdapter(_variant_interpretation_response())

    revised = request_revised_variant_interpretation(
        result,
        variant_index=0,
        reviewer_context=(
            "The reviewer resolved the source hierarchy after checking the "
            "retained evidence details."
        ),
        client=LLMClient(adapter),
        timestamp="2026-08-14T12:00:00Z",
    )

    assert revised["variant_interpretation_results"] == initial
    assert revised["draft_variant_reports"] == drafts
    assert len(revised["revised_interpretations"]) == 1
    version = revised["revised_interpretations"][0]
    assert version["revision_number"] == 1
    assert version["reviewer_context"].startswith("The reviewer resolved")
    assert version["interpretation"]["configured_model"]
    assert "BEGIN_REVIEWER_APPROVED_CONTEXT" in adapter.requests[0].messages[1].content
    assert validate_pipeline_result(revised) == revised


def test_revisions_are_ordered_and_confirmation_does_not_generate_one() -> None:
    result = DraftFactory._draft_result()
    first = request_revised_variant_interpretation(
        result,
        variant_index=0,
        reviewer_context="The reviewer requests a concise revised synthesis.",
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        timestamp="2026-08-14T12:00:00Z",
    )
    second = request_revised_variant_interpretation(
        first,
        variant_index=0,
        reviewer_context="The reviewer records a second scoped clarification.",
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        timestamp="2026-08-14T12:01:00Z",
    )

    assert [
        item["revision_number"] for item in second["revised_interpretations"]
    ] == [1, 2]
    assert second["variant_interpretation_results"] == result[
        "variant_interpretation_results"
    ]
    with pytest.raises(PipelineError, match="must be selected"):
        confirm_reviewed_evidence(
            second,
            [second["evidence_review_reports"][0]],
        )


def test_revised_context_rejects_personal_data_before_model_request() -> None:
    adapter = FakeLLMAdapter(_variant_interpretation_response())

    with pytest.raises(PipelineError, match="prohibited clinical data"):
        request_revised_variant_interpretation(
            DraftFactory._draft_result(),
            variant_index=0,
            reviewer_context="Patient name: Prohibited Person",
            client=LLMClient(adapter),
        )

    assert adapter.requests == []


def test_reviewer_resolved_material_conflict_can_be_reinterpreted_without_new_sources() -> None:
    evidence = InterpretationFactory._conflicting_evidence()
    adapter = FakeLLMAdapter(_variant_interpretation_response())

    result = interpret_variant(
        evidence,
        client=LLMClient(adapter),
        reviewer_context=(
            "The reviewer resolved the retained-source hierarchy and requests "
            "a revised preliminary synthesis."
        ),
        timestamp="2026-08-14T12:00:00Z",
    )

    assert result["preliminary_classification_status"] == "classified"
    assert result["preliminary_classification"] == "Pathogenic"
    assert "new source" in adapter.requests[0].messages[1].content
