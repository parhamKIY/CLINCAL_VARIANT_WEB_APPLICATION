"""Tests for the 'Start new analysis' button and workspace reset after finalization."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from backend.pipeline import (
    confirm_reviewed_evidence,
    finalize_reviewed_analysis,
    validate_pipeline_result,
)
from frontend.evidence_review import (
    _REVIEW_WIDGET_PREFIX,
    _render_finalization_action,
    render_evidence_review,
)
from frontend.ui import (
    ANALYSIS_EXECUTION_TRACE_KEY,
    CLINICAL_ENTITIES_KEY,
    CLINICAL_ENTITY_DRAFTS_KEY,
    CLINICAL_ENTITY_REVIEW_COMPLETE_KEY,
    HPO_MODEL_CANDIDATES_KEY,
    PIPELINE_RESULT_KEY,
    SELECTED_HPO_KEY,
    reset_analysis_workspace,
)
from tests.test_pipeline import (
    PROJECT_ROOT,
    TestStage40FrontendReviewWorkflow,
)


def _completed_result() -> dict[str, object]:
    draft = TestStage40FrontendReviewWorkflow()._draft_result()
    confirmed = confirm_reviewed_evidence(draft, draft["evidence_review_reports"])
    finalized = finalize_reviewed_analysis(confirmed)
    return validate_pipeline_result(finalized)


def _draft_result() -> dict[str, object]:
    return TestStage40FrontendReviewWorkflow()._draft_result()


def test_button_states_before_and_after_finalization() -> None:
    """Ensure Start new analysis is only available after finalization."""

    # 1. Before finalization:
    def _unfinalized_app() -> None:
        from frontend.evidence_review import _render_finalization_action
        from tests.test_pipeline import TestStage40FrontendReviewWorkflow

        draft = TestStage40FrontendReviewWorkflow()._draft_result()
        reports = draft.get("evidence_review_reports", [])
        _render_finalization_action(draft, reports)

    app_draft = AppTest.from_function(_unfinalized_app).run(timeout=10)
    assert not app_draft.exception
    button_labels_draft = [b.label for b in app_draft.button]
    assert "Finalize report" in button_labels_draft
    assert "Start new analysis" not in button_labels_draft

    # 2. After finalization:
    def _finalized_app() -> None:
        from backend.pipeline import (
            confirm_reviewed_evidence,
            finalize_reviewed_analysis,
            validate_pipeline_result,
        )
        from frontend.evidence_review import _render_finalization_action
        from tests.test_pipeline import TestStage40FrontendReviewWorkflow

        draft = TestStage40FrontendReviewWorkflow()._draft_result()
        confirmed = confirm_reviewed_evidence(draft, draft["evidence_review_reports"])
        completed = validate_pipeline_result(finalize_reviewed_analysis(confirmed))
        reports = completed.get("evidence_review_reports", [])
        _render_finalization_action(completed, reports)

    app_completed = AppTest.from_function(_finalized_app).run(timeout=10)
    assert not app_completed.exception
    button_labels_completed = [b.label for b in app_completed.button]
    assert "Finalize report" not in button_labels_completed
    assert "Start new analysis" in button_labels_completed


def test_reset_analysis_workspace_clears_all_patient_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify reset_analysis_workspace resets all patient inputs, widgets, and results."""

    st.session_state[PIPELINE_RESULT_KEY] = _completed_result()
    st.session_state[ANALYSIS_EXECUTION_TRACE_KEY] = {"sample": "trace"}
    st.session_state[SELECTED_HPO_KEY] = [{"id": "HP:0001250", "name": "Seizures"}]
    st.session_state[CLINICAL_ENTITIES_KEY] = [{"text": "seizure"}]
    st.session_state[CLINICAL_ENTITY_DRAFTS_KEY] = [{"text": "seizure"}]
    st.session_state[HPO_MODEL_CANDIDATES_KEY] = [{"hpo_id": "HP:0001250"}]
    st.session_state["phenotype_clinical_text_fa"] = "بیمار با سابقه تشنج مکرر"
    st.session_state["manual_variant_chrom_0"] = "1"
    st.session_state["manual_variant_pos_0"] = 100_000
    st.session_state["vcf_upload"] = "dummy_file"
    st.session_state["vcf_uploader_version"] = 0
    st.session_state["xlsx_upload_identity"] = "dummy_hash"
    st.session_state["xlsx_selected_worksheet"] = "Sheet1"
    st.session_state["xlsx_selected_source_rows"] = [1, 2]

    reset_analysis_workspace(rerun=False)

    assert st.session_state[PIPELINE_RESULT_KEY] is None
    assert st.session_state[ANALYSIS_EXECUTION_TRACE_KEY] is None
    assert st.session_state[SELECTED_HPO_KEY] == []
    assert st.session_state[CLINICAL_ENTITIES_KEY] == []
    assert st.session_state[CLINICAL_ENTITY_DRAFTS_KEY] == []
    assert st.session_state[HPO_MODEL_CANDIDATES_KEY] == []
    assert st.session_state["phenotype_input_version"] == 1
    assert st.session_state["vcf_uploader_version"] == 1
    assert st.session_state["manual_input_version"] == 1
    assert st.session_state["variant_input_mode_version"] == 1
    assert "xlsx_upload_identity" not in st.session_state
    assert "xlsx_selected_worksheet" not in st.session_state
    assert "xlsx_selected_source_rows" not in st.session_state


def test_start_new_analysis_in_full_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test clicking 'Start new analysis' in the live AppTest resets pipeline_result to None."""

    result = _completed_result()
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = result
    app.session_state["selected_hpo_terms"] = [{"id": "HP:0001251", "name": "Ataxia"}]
    app.run(timeout=10)

    assert not app.exception
    assert app.session_state["pipeline_result"] is not None

    start_new_btn = next(
        b for b in app.button
        if b.label == "Start new analysis"
    )
    start_new_btn.click().run(timeout=10)

    assert not app.exception
    assert app.session_state["pipeline_result"] is None
    assert app.session_state["selected_hpo_terms"] == []
