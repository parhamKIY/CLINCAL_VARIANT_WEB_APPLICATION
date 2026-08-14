"""Stage 117 acceptance: call quality remains visible through reporting."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO

import pytest
from docx import Document

from backend.call_quality import CallQualityError
from backend.llm import LLMClient
from backend.report import build_evidence_object
from backend.report_data_projection import build_report_data_from_draft
from backend.report_docx import render_report_data_docx
from backend.report_lifecycle import build_variant_report_record
from backend.variant_interpretation import interpret_variant
from backend.variant_report import (
    build_draft_variant_report,
    set_draft_variant_report_inclusion,
)
from frontend.report_preview import render_draft_report_preview_pages
from frontend.warning_semantics import build_warning_notices
from tests.test_pipeline import (
    FakeLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage117_call_quality
_TIMESTAMP = "2026-08-14T08:00:00Z"


def _artifacts(*, override: bool) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    candidate = EvidenceFactory._pipeline_candidate()
    variant = candidate["variant"]
    assert isinstance(variant, dict)
    variant.update(
        {
            "qual": 42.5,
            "filter": "q10;LowQual",
            "call_quality": {
                "schema_version": "1.0",
                "status": "failed",
                "acknowledged_at": None,
                "override": (
                    {
                        "reason": "Verified against the upstream assay record.",
                        "timestamp": _TIMESTAMP,
                    }
                    if override
                    else None
                ),
            },
        }
    )
    evidence = build_evidence_object(candidate)
    interpretation = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        timestamp=_TIMESTAMP,
    )
    draft = build_draft_variant_report(evidence, interpretation, variant_index=0)
    return evidence, draft, build_report_data_from_draft(draft, analysis_id="stage117")


def test_quality_is_immutable_and_visible_in_report_channels() -> None:
    evidence, draft, report_data = _artifacts(override=True)

    assert evidence["call_quality"]["filter"] == "q10;LowQual"
    assert draft["machine_original_report"]["call_quality"] == evidence["call_quality"]
    assert draft["reviewed_report"]["call_quality"] == evidence["call_quality"]
    assert report_data["call_quality"] == {
        "qual": 42.5,
        "filter": "q10;LowQual",
        "status": "failed",
        "acknowledged_at": None,
        "override_reason": "Verified against the upstream assay record.",
        "override_timestamp": _TIMESTAMP,
    }

    pages = "".join(render_draft_report_preview_pages(draft))
    assert "QUAL: 42.5; FILTER: q10;LowQual; State: failed" in pages
    assert "PARTIAL: overridden by reviewer" in pages
    document = Document(BytesIO(render_report_data_docx(report_data)))
    assert "Call quality — QUAL: 42.5; FILTER: q10;LowQual; state: failed." in "\n".join(
        paragraph.text for paragraph in document.paragraphs
    )
    assert build_warning_notices(draft)[0]["severity"] == "PARTIAL"

    excluded = set_draft_variant_report_inclusion(
        draft,
        include_in_final_report=False,
        timestamp="2026-08-14T08:05:00Z",
    )
    assert excluded["reviewed_report"]["call_quality"] == evidence["call_quality"]
    record = build_variant_report_record(
        excluded,
        analysis_id="stage117",
        confirmed_at="2026-08-14T08:06:00Z",
    )
    assert record["report_data"]["call_quality"] == report_data["call_quality"]


def test_unoverridden_failure_requires_action_and_filter_is_validated() -> None:
    _evidence, draft, _report_data = _artifacts(override=False)

    notices = build_warning_notices(draft)
    assert notices[0]["severity"] == "ACTION REQUIRED"
    assert "override" in notices[0]["message"]

    malformed = deepcopy(EvidenceFactory._pipeline_candidate())
    variant = malformed["variant"]
    assert isinstance(variant, dict)
    variant.update({"qual": 12.0, "filter": "PASS\x00<script>"})
    with pytest.raises(CallQualityError):
        build_evidence_object(malformed)
