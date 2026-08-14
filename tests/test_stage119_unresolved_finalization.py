"""Stage 119 unresolved-interpretation finalization acceptance tests."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
from zipfile import ZipFile

import pytest
from streamlit.testing.v1 import AppTest

from backend.final_clinical_report import render_final_clinical_report_markdown
from backend.final_docx_package import build_final_docx_package
from backend.llm import LLMClient
from backend.pipeline import (
    PipelineError,
    acknowledge_unresolved_interpretation_inclusion,
    confirm_reviewed_evidence,
    finalize_reviewed_analysis,
    retry_failed_variant_interpretation,
    update_draft_variant_report,
)
from backend.variant_report import set_draft_variant_report_inclusion
from tests.test_defect08_interpretation_recovery import _failed_draft_result
from tests.test_pipeline import (
    PROJECT_ROOT,
    FakeLLMAdapter,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage119_unresolved_finalization


def _selected_failed_result() -> dict[str, object]:
    result = _failed_draft_result()
    report = result["draft_variant_reports"][0]
    assert report["include_in_final_report"] is False
    selected = set_draft_variant_report_inclusion(
        report,
        True,
        timestamp="2026-08-13T09:10:00Z",
        reviewer_context="stage119_test",
    )
    return update_draft_variant_report(result, selected)


def _confirmed_selected_failed_result() -> dict[str, object]:
    selected = _selected_failed_result()
    return confirm_reviewed_evidence(
        selected,
        selected["evidence_review_reports"],
        timestamp="2026-08-13T09:20:00Z",
    )


def test_exhausted_interpretation_is_deselected_by_default() -> None:
    result = _failed_draft_result()

    assert result["draft_variant_reports"][0]["include_in_final_report"] is False
    assert result["variant_report_records"] == []


def test_finalization_rejects_selected_unresolved_without_acknowledgement() -> None:
    with pytest.raises(PipelineError, match="explicit reviewer acknowledgement"):
        finalize_reviewed_analysis(
            _confirmed_selected_failed_result(),
            timestamp="2026-08-13T09:30:00Z",
        )


def test_acknowledged_unresolved_inclusion_is_audited_and_manifested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _selected_failed_result()
    acknowledged = acknowledge_unresolved_interpretation_inclusion(
        selected,
        variant_index=0,
        reason="Evidence is retained for qualified reviewer follow-up.",
        timestamp="2026-08-13T09:15:00Z",
    )
    confirmed = confirm_reviewed_evidence(
        acknowledged,
        acknowledged["evidence_review_reports"],
        timestamp="2026-08-13T09:20:00Z",
    )

    monkeypatch.setattr(
        "backend.variant_interpretation.interpret_variant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Finalization must not call an LLM.")
        ),
    )
    finalized = finalize_reviewed_analysis(
        confirmed,
        timestamp="2026-08-13T09:30:00Z",
    )

    record = finalized["variant_report_records"][0]
    report = finalized["final_clinical_report"]
    assert record["lifecycle_state"] == "finalized_with_unresolved_interpretation"
    assert record["unresolved_interpretation_acknowledgement"]["failure_type"] == "request_timeout"
    assert report["metadata"]["finalization_state"] == "Finalized with unresolved variants"
    assert report["main_findings"][0]["interpretation_failure_type"] == "request_timeout"
    assert "llmtimeouterror" not in render_final_clinical_report_markdown(report).casefold()

    package = build_final_docx_package(finalized)
    with ZipFile(BytesIO(package["data"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["finalization_state"] == "Finalized with unresolved variants"
    assert manifest["reports"][0]["lifecycle_state"] == (
        "finalized_with_unresolved_interpretation"
    )


def test_successful_retry_clears_acknowledgement_and_restores_ordinary_finalization() -> None:
    selected = _selected_failed_result()
    acknowledged = acknowledge_unresolved_interpretation_inclusion(
        selected,
        variant_index=0,
        reason="Retry requested before finalization.",
        timestamp="2026-08-13T09:15:00Z",
    )

    retried = retry_failed_variant_interpretation(
        deepcopy(acknowledged),
        variant_index=0,
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        timestamp="2026-08-13T09:25:00Z",
    )

    assert retried["variant_interpretation_results"][0]["status"] == "success"
    assert retried["unresolved_finalization_acknowledgements"] == []
    assert retried["draft_variant_reports"][0]["include_in_final_report"] is True
    confirmed = confirm_reviewed_evidence(
        retried,
        retried["evidence_review_reports"],
        timestamp="2026-08-13T09:30:00Z",
    )
    finalized = finalize_reviewed_analysis(
        confirmed,
        timestamp="2026-08-13T09:35:00Z",
    )

    assert finalized["variant_report_records"][0]["lifecycle_state"] == "finalized"
    assert finalized["final_clinical_report"]["metadata"]["finalization_state"] == "Finalized"


def test_ui_displays_unresolved_finalization_label() -> None:
    selected = _selected_failed_result()
    acknowledged = acknowledge_unresolved_interpretation_inclusion(
        selected,
        variant_index=0,
        reason="Evidence is retained for qualified reviewer follow-up.",
        timestamp="2026-08-13T09:15:00Z",
    )
    confirmed = confirm_reviewed_evidence(
        acknowledged,
        acknowledged["evidence_review_reports"],
        timestamp="2026-08-13T09:20:00Z",
    )
    finalized = finalize_reviewed_analysis(
        confirmed,
        timestamp="2026-08-13T09:30:00Z",
    )

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = finalized
    app.run(timeout=10)

    assert not app.exception
    assert any(
        "Finalized with unresolved variants" in element.value
        for element in [*app.get("caption"), *app.get("warning")]
    )
