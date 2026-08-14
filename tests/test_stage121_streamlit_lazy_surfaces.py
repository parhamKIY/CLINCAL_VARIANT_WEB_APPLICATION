"""Stage 121 on-demand Streamlit review-surface acceptance tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import frontend.evidence_review as evidence_review
import frontend.results as results


pytestmark = pytest.mark.stage121_streamlit_lazy_surfaces


def _button_group(app: AppTest, label: str):
    return next(control for control in app.button_group if control.label == label)


def _draft_result() -> dict[str, object]:
    from tests.test_pipeline import TestStage40FrontendReviewWorkflow

    return TestStage40FrontendReviewWorkflow._draft_result()


def _app_with_result(result: dict[str, object]) -> AppTest:
    project_root = Path(__file__).resolve().parents[1]
    app = AppTest.from_file(str(project_root / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = result
    return app.run(timeout=10)


def test_review_technical_details_are_rendered_only_on_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _draft_result()
    calls: list[int] = []
    render_diagnostics = evidence_review._render_selected_provider_diagnostics

    def counted_diagnostics(*args, **kwargs):
        calls.append(args[1])
        return render_diagnostics(*args, **kwargs)

    monkeypatch.setattr(
        evidence_review,
        "_render_selected_provider_diagnostics",
        counted_diagnostics,
    )

    app = _app_with_result(result)

    assert not app.exception
    assert calls == []
    assert not app.json
    _button_group(app, "Review surface").select("Technical details").run(
        timeout=10
    )

    assert not app.exception
    assert calls == [0]
    assert _button_group(app, "Technical detail").value == "Provider diagnostics"


def test_view_changes_preserve_review_state_and_hide_evidence_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _draft_result()
    persisted: list[dict[str, object]] = []
    monkeypatch.setattr(
        evidence_review,
        "save_pipeline_state",
        lambda saved: persisted.append(deepcopy(saved)) or saved,
    )

    app = _app_with_result(result)
    draft = app.session_state["evidence_review_drafts"][0]
    draft["reviewed_user_report"]["manual_evidence"] = {
        "laboratory": "Retained reviewer draft."
    }
    app.session_state["evidence_review_packages"] = {"retained": {}}
    app.session_state["selected_evidence_review_variant"] = 0
    app.session_state["evidence_review_include_final_" + draft["report_id"]] = (
        True
    )

    _button_group(app, "Review surface").select("Technical details").run(
        timeout=10
    )
    _button_group(app, "Technical detail").select("Original evidence").run(
        timeout=10
    )

    assert app.json
    _button_group(app, "Review surface").select("Clinical report review").run(
        timeout=10
    )

    assert app.session_state["selected_evidence_review_variant"] == 0
    assert app.session_state["evidence_review_drafts"][0][
        "reviewed_user_report"
    ]["manual_evidence"] == {"laboratory": "Retained reviewer draft."}
    assert app.session_state["evidence_review_packages"] == {"retained": {}}
    assert app.session_state[
        "evidence_review_include_final_" + draft["report_id"]
    ] is True
    assert persisted == []


def test_analysis_evidence_view_is_not_rendered_before_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _draft_result()
    calls = 0
    render_evidence_view = results._render_evidence_view

    def counted_evidence_view(*args, **kwargs):
        nonlocal calls
        calls += 1
        return render_evidence_view(*args, **kwargs)

    monkeypatch.setattr(results, "_render_evidence_view", counted_evidence_view)

    app = _app_with_result(result)

    assert not app.exception
    assert calls == 0
    _button_group(app, "Analysis result view").select(
        "Analysis and provider details"
    ).run(timeout=10)
    _button_group(app, "Analysis view").select("Evidence").run(timeout=10)

    assert not app.exception
    assert calls == 1
    assert any(field.label == "Evidence Object details" for field in app.selectbox)
    assert not app.json


def test_changed_streamlit_surfaces_do_not_use_deprecated_apis() -> None:
    frontend_paths = (
        "frontend/evidence_review.py",
        "frontend/results.py",
        "frontend/ui.py",
        "frontend/report_viewer.py",
    )
    for path in frontend_paths:
        source = open(path, encoding="utf-8").read()
        assert "use_container_width" not in source
        assert "st.components.v1" not in source
