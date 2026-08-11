"""Stage 86 per-variant report lifecycle acceptance tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from backend.pipeline import (
    confirm_reviewed_evidence,
    finalize_reviewed_analysis,
    update_draft_variant_report,
    validate_pipeline_result,
)
from backend.report_docx import render_report_data_docx
from backend.report_lifecycle import (
    ReportLifecycleError,
    build_variant_report_records,
    validate_variant_report_record,
)
from backend.variant_report import (
    save_draft_variant_report,
    set_draft_variant_report_inclusion,
)
from tests.test_pipeline import (
    TestStage40FrontendReviewWorkflow as _Stage40FrontendReviewWorkflow,
)
from tests.test_stage84_report_preview import _draft_report


pytestmark = pytest.mark.stage86_report_lifecycle


def _ordered_drafts() -> list[dict[str, object]]:
    first = _draft_report()
    second = deepcopy(first)
    second["variant_index"] = 1
    for field in ("machine_original_report", "reviewed_report"):
        content = second[field]
        content["variant_summary"]["pos"] = 100002
        content["variant_summary"]["display_label"] = (
            "GENE<1> — GRCh38 2:100002 C>G"
        )
        content["variant_summary"]["alt"] = "G"
    digest = hashlib.sha256(
        json.dumps(
            second["machine_original_report"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    second["report_id"] = f"dvr-1-{digest}"
    return [first, second]


def test_every_variant_has_one_ordered_primary_report_and_docx_draft() -> None:
    records = build_variant_report_records(
        _ordered_drafts(),
        analysis_id=f"analysis-{'8' * 32}",
    )

    assert [record["variant_index"] for record in records] == [0, 1]
    assert [record["lifecycle_state"] for record in records] == [
        "draft",
        "draft",
    ]
    for record in records:
        data = render_report_data_docx(record["report_data"])
        assert record["docx_artifact"] == {
            "filename": f"variant_{record['variant_index'] + 1:03d}_report.docx",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "template_version": "professor-report-v1",
        }


def test_exclusion_is_reporting_only_and_preserves_original_order() -> None:
    drafts = _ordered_drafts()
    drafts[0] = set_draft_variant_report_inclusion(
        drafts[0],
        False,
        timestamp="2026-08-11T09:00:00Z",
        reviewer_context="stage86_test",
    )
    records = build_variant_report_records(
        drafts,
        analysis_id=f"analysis-{'8' * 32}",
    )

    assert len(records) == 2
    assert [record["variant_index"] for record in records] == [0, 1]
    assert records[0]["report_data"]["review_state"][
        "include_in_final_report"
    ] is False
    assert records[1]["report_data"]["review_state"][
        "include_in_final_report"
    ] is True
    assert records[0]["report_data"]["variant_identity"]["position"] == 100001


def test_edit_confirmation_and_finalization_advance_same_report_record() -> None:
    result = _Stage40FrontendReviewWorkflow._draft_result()
    result["variant_report_records"] = build_variant_report_records(
        result["draft_variant_reports"],
        analysis_id=result["analysis_id"],
    )
    result = validate_pipeline_result(result)
    original_id = result["variant_report_records"][0]["report_id"]
    original_hash = result["variant_report_records"][0]["docx_artifact"][
        "sha256"
    ]
    draft = result["draft_variant_reports"][0]
    reviewed = draft["reviewed_report"]
    edited = save_draft_variant_report(
        draft,
        reviewer_summary="Stage 86 reviewed summary.",
        interpretation_narrative=reviewed["variant_interpretation"]["narrative"],
        conflict_assessment=reviewed["variant_interpretation"][
            "conflict_assessment"
        ],
        reviewer_notes=reviewed["reviewer_notes"],
        timestamp="2026-08-11T09:00:00Z",
        reviewer_context="stage86_test",
    )
    edited_result = update_draft_variant_report(result, edited)
    edited_record = edited_result["variant_report_records"][0]

    assert edited_record["report_id"] == original_id
    assert edited_record["docx_artifact"]["sha256"] != original_hash
    assert edited_record["report_data"]["review_state"]["reviewer_summary"] == (
        "Stage 86 reviewed summary."
    )

    confirmed = confirm_reviewed_evidence(
        edited_result,
        edited_result["evidence_review_reports"],
        timestamp="2026-08-11T09:05:00Z",
    )
    assert confirmed["variant_report_records"][0]["lifecycle_state"] == (
        "confirmed"
    )
    assert confirmed["variant_report_records"][0]["confirmed_at"] == (
        "2026-08-11T09:05:00Z"
    )

    finalized = finalize_reviewed_analysis(
        confirmed,
        timestamp="2026-08-11T09:10:00Z",
    )
    record = finalized["variant_report_records"][0]
    assert record["report_id"] == original_id
    assert record["lifecycle_state"] == "finalized"
    assert record["finalized_at"] == "2026-08-11T09:10:00Z"


def test_tampered_docx_metadata_is_rejected() -> None:
    record = build_variant_report_records(
        [_draft_report()],
        analysis_id=f"analysis-{'8' * 32}",
    )[0]
    record["docx_artifact"]["sha256"] = "0" * 64

    with pytest.raises(ReportLifecycleError, match="DOCX metadata"):
        validate_variant_report_record(record)
