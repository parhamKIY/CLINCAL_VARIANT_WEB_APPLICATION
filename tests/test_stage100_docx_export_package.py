"""Stage 100 editable DOCX export and selected-package acceptance tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile

import pytest
from streamlit.testing.v1 import AppTest

import backend.variant_interpretation as variant_interpretation
from backend.final_clinical_report import compose_final_clinical_report
from backend.final_docx_package import (
    FinalDocxPackageError,
    build_final_docx_package,
)
from backend.llm import LLMClient
from backend.pipeline import PIPELINE_SCHEMA_VERSION
from backend.report_docx import render_report_data_docx
from backend.report_lifecycle import build_variant_report_records
from backend.variant_interpretation import interpret_variant
from backend.variant_report import (
    build_draft_variant_reports,
    save_draft_variant_report,
    set_draft_variant_report_inclusion,
)
from tests.test_pipeline import (
    PROJECT_ROOT,
    FakeLLMAdapter,
    TestStage52DraftVariantReportV2 as _Stage52,
    TestStage56FinalClinicalReport as _Stage56,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage100_docx_export_package


def _completed_pipeline(
    *, selected_indexes: set[int] = {1, 3}
) -> dict[str, object]:
    base_evidence, _ = _Stage52._success_inputs()
    evidence_objects: list[dict[str, object]] = []
    interpretations: list[dict[str, object]] = []
    for index in range(4):
        evidence = deepcopy(base_evidence)
        position = 166_848_215 + index
        evidence["variant"]["pos"] = position
        evidence["variant_context"]["input"]["pos"] = position
        evidence["variant_context"]["normalized"]["pos"] = position
        evidence_objects.append(evidence)
        interpretations.append(
            dict(
                interpret_variant(
                    evidence,
                    variant_index=index,
                    client=LLMClient(
                        FakeLLMAdapter(_variant_interpretation_response())
                    ),
                )
            )
        )
    drafts = build_draft_variant_reports(evidence_objects, interpretations)
    approved = drafts[1]
    reviewed = approved["reviewed_report"]
    drafts[1] = save_draft_variant_report(
        approved,
        reviewer_summary="Stage 100 reviewer-approved summary.",
        interpretation_narrative=(
            "Stage 100 reviewer-approved interpretation retained exactly."
        ),
        conflict_assessment=reviewed["variant_interpretation"][
            "conflict_assessment"
        ],
        reviewer_notes=["Approved for editable Word export."],
        timestamp="2099-01-01T00:00:00Z",
        reviewer_context="stage100_test",
    )
    for index, report in enumerate(drafts):
        if index not in selected_indexes:
            drafts[index] = set_draft_variant_report_inclusion(
                report,
                False,
                timestamp="2099-01-01T00:00:30Z",
                reviewer_context="stage100_test",
            )
    packages = [
        {
            "variant_index": index,
            "package_id": f"reviewed-package-{index:024x}",
            "confirmed_at": "2099-01-01T00:01:00Z",
        }
        for index in range(4)
    ]
    analysis_id = "analysis-stage100"
    records = build_variant_report_records(
        drafts,
        analysis_id=analysis_id,
        confirmed_packages=packages,
        finalized_at="2099-01-01T00:02:00Z",
    )
    pipeline: dict[str, object] = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "workflow_state": "completed",
        "variant_count": 4,
        "draft_variant_reports": drafts,
        "reviewed_evidence_packages": packages,
        "variant_report_records": records,
    }
    pipeline["final_clinical_report"] = compose_final_clinical_report(
        pipeline,
        timestamp="2099-01-01T00:02:00Z",
    )
    return pipeline


def test_selected_package_contains_only_approved_docx_in_original_order() -> None:
    pipeline = _completed_pipeline()
    package = build_final_docx_package(pipeline)

    with ZipFile(BytesIO(package["data"])) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "variant_002_report.docx",
            "variant_004_report.docx",
        ]
        manifest = json.loads(archive.read("manifest.json"))
        selected_docx = archive.read("variant_002_report.docx")

    records = pipeline["variant_report_records"]
    expected = render_report_data_docx(records[1]["report_data"])
    assert selected_docx == expected
    assert manifest["selected_variant_indexes"] == [1, 3]
    assert [item["variant_index"] for item in manifest["reports"]] == [1, 3]
    assert manifest["reports"][0]["sha256"] == hashlib.sha256(
        selected_docx
    ).hexdigest()
    with ZipFile(BytesIO(selected_docx)) as document:
        document_xml = document.read("word/document.xml").decode("utf-8")
    assert "Stage 100 reviewer-approved summary." in document_xml
    assert "Stage 100 reviewer-approved interpretation retained exactly." in (
        document_xml
    )


def test_final_docx_package_is_deterministic_and_does_not_call_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = _completed_pipeline()

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Export must not regenerate interpretation.")

    monkeypatch.setattr(
        variant_interpretation,
        "interpret_variant",
        fail_if_called,
    )
    first = build_final_docx_package(pipeline)
    second = build_final_docx_package(pipeline)

    assert first == second


def test_final_docx_package_requires_completed_finalized_state() -> None:
    pipeline = _completed_pipeline()
    pipeline["workflow_state"] = "awaiting_final_review"

    with pytest.raises(FinalDocxPackageError, match="completed analysis"):
        build_final_docx_package(pipeline)

    pipeline = _completed_pipeline()
    pipeline["variant_report_records"][1]["lifecycle_state"] = "confirmed"
    with pytest.raises(FinalDocxPackageError, match="approved final report state"):
        build_final_docx_package(pipeline)


def test_final_docx_package_rejects_empty_selection() -> None:
    with pytest.raises(FinalDocxPackageError, match="No reports are selected"):
        build_final_docx_package(_completed_pipeline(selected_indexes=set()))


def test_ui_offers_per_variant_docx_and_selected_final_package() -> None:
    completed = _Stage56._completed_result()
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = completed
    app.run(timeout=10)

    assert not app.exception
    labels = {button.label for button in app.get("download_button")}
    assert "Download editable DOCX" in labels
    assert "Download selected Word report package" in labels
