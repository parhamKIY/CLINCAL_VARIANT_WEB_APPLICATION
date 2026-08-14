"""Stage 127 selected preliminary-interpretation finalization tests."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from streamlit.testing.v1 import AppTest

from backend.final_clinical_report import compose_final_clinical_report
from backend.database import load_pipeline_state, save_complete_analysis, save_pipeline_state
from backend.llm import LLMClient
from backend.pipeline import (
    confirm_reviewed_evidence,
    request_revised_variant_interpretation,
    select_final_interpretation_version,
    validate_pipeline_result,
)
from backend.report_docx import render_report_data_docx
from backend.report_lifecycle import build_variant_report_records
from test_pipeline import (
    FakeLLMAdapter,
    TestStage40FrontendReviewWorkflow as DraftFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage127_preliminary_finalization


def _selection_app() -> None:
    from backend.llm import LLMClient
    from backend.pipeline import request_revised_variant_interpretation
    from frontend.evidence_review import _render_interpretation_version_selection
    from test_pipeline import (
        FakeLLMAdapter,
        TestStage40FrontendReviewWorkflow,
        _variant_interpretation_response,
    )

    result = request_revised_variant_interpretation(
        TestStage40FrontendReviewWorkflow._draft_result(),
        variant_index=0,
        reviewer_context="Reviewer requests a versioned final-report synthesis.",
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        timestamp="2026-08-14T12:00:00Z",
    )
    _render_interpretation_version_selection(
        result["draft_variant_reports"][0],
        result,
    )


def _revision() -> dict[str, object]:
    return request_revised_variant_interpretation(
        DraftFactory._draft_result(),
        variant_index=0,
        reviewer_context="Reviewer resolves the retained-source hierarchy.",
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        timestamp="2026-08-14T12:00:00Z",
    )


def test_review_surface_offers_initial_and_revised_version_selection() -> None:
    app = AppTest.from_function(_selection_app).run(timeout=10)

    assert not app.exception
    assert any(
        item.label == "Interpretation version for final output"
        for item in app.selectbox
    )
    assert any(item.label == "Save interpretation version" for item in app.button)


def test_selected_revision_is_audited_and_drives_docx_output(tmp_path: Path) -> None:
    database_path = tmp_path / "stage127.sqlite3"
    base = DraftFactory._draft_result()
    persisted = save_complete_analysis(
        status="partial",
        candidates=base["variants"],
        evidence_objects=base["evidence_objects"],
        database_path=database_path,
    )
    base["analysis_id"] = persisted["analysis_id"]
    base["variant_report_records"] = build_variant_report_records(
        base["draft_variant_reports"],
        analysis_id=base["analysis_id"],
    )
    revised = request_revised_variant_interpretation(
        base,
        variant_index=0,
        reviewer_context="Reviewer resolves the retained-source hierarchy.",
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        timestamp="2026-08-14T12:00:00Z",
    )
    selected = select_final_interpretation_version(
        revised,
        variant_index=0,
        selected_revision_number=1,
        reviewer_context="local_streamlit_session",
        timestamp="2026-08-14T12:01:00Z",
    )
    record = selected["variant_report_records"][0]["report_data"]
    text = "\n".join(
        paragraph.text
        for paragraph in Document(BytesIO(render_report_data_docx(record))).paragraphs
    )

    assert record["schema_version"] == "6.0"
    assert record["interpretation_version_selection"]["selected_revision_number"] == 1
    assert len(record["interpretation_version_selection"]["selection_history"]) == 1
    assert "revised interpretation version 1" in text
    assert "Interpretation selection history" in text
    save_pipeline_state(selected, database_path=database_path)
    restored = load_pipeline_state(
        str(selected["analysis_id"]),
        database_path=database_path,
    )
    assert restored["interpretation_version_selection_history"] == (
        selected["interpretation_version_selection_history"]
    )
    assert validate_pipeline_result(selected) == selected


def test_selection_is_required_before_confirmation_and_retained_in_final_report() -> None:
    revised = _revision()
    selected = select_final_interpretation_version(
        revised,
        variant_index=0,
        selected_revision_number=0,
        reviewer_context="Reviewer keeps the initial interpretation.",
        timestamp="2026-08-14T12:01:00Z",
    )
    confirmed = confirm_reviewed_evidence(
        selected,
        [selected["evidence_review_reports"][0]],
        timestamp="2026-08-14T12:02:00Z",
    )
    final = compose_final_clinical_report(
        confirmed,
        timestamp="2026-08-14T12:03:00Z",
    )

    notes = final["variant_sections"][0]["reviewed_report"]["reviewer_notes"]
    assert "Final report uses the initial interpretation." in notes
    assert any("Interpretation selection history" in note for note in notes)
    assert final["main_findings"][0]["interpretation_narrative"] == (
        selected["variant_interpretation_results"][0]["interpretation"]
    )
