"""Stage 9.2.3 draft AI-classification contract and report integration."""

import hashlib
import json
from io import BytesIO
from zipfile import ZipFile

from backend.llm import LLMClient, LLMResponse
from backend.report_data_projection import build_report_data_from_draft
from backend.report_docx import render_report_data_docx
from backend.variant_interpretation import (
    interpret_variant,
    interpret_variants,
    validate_variant_interpretation_result,
)
from backend.variant_report import (
    build_draft_variant_report,
    validate_draft_variant_report,
)
from frontend.report_preview import render_draft_report_preview_pages
from test_pipeline import FakeLLMAdapter, TestEvidenceObject as EvidenceFactory


def _response(*, ai_classification: object = "Likely pathogenic") -> LLMResponse:
    payload: dict[str, object] = {
        "interpretation": "The collected evidence supports a cautious draft synthesis.",
        "conflict_assessment": "Source disagreements remain visible for review.",
        "phenotype_conclusion": "partially supported",
        "warnings": [],
    }
    if ai_classification is not _MISSING:
        payload["ai_classification"] = ai_classification
    return LLMResponse(
        content=json.dumps(payload),
        model="stage9-2-3-model",
        finish_reason="stop",
    )


_MISSING = object()


def test_valid_ai_classification_reaches_existing_report_surfaces() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    interpretation = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(_response())),
        timestamp="2026-08-24T10:00:00Z",
    )
    draft = build_draft_variant_report(evidence, interpretation, variant_index=0)
    report_data = build_report_data_from_draft(
        draft,
        analysis_id=f"analysis-{'a' * 32}",
    )
    preview = render_draft_report_preview_pages(draft)
    with ZipFile(BytesIO(render_report_data_docx(report_data))) as document:
        document_xml = document.read("word/document.xml").decode("utf-8")

    assert interpretation["ai_classification"] == "Likely pathogenic"
    assert interpretation["interpretation"] == (
        "The collected evidence supports a cautious draft synthesis."
    )
    assert draft["reviewed_report"]["variant_interpretation"][
        "ai_classification"
    ] == "Likely pathogenic"
    assert report_data["conclusive_result"] == {
        "gene": "SCN1A",
        "hgvs_c": None,
        "hgvs_p": "ENSP00000303540:p.Arg1645Cys",
        "zygosity": None,
        "classification": "Likely pathogenic",
        "classification_source": "LLM draft classification",
        "status": "available",
    }
    assert "System classification: Likely pathogenic" in preview[0]
    assert "Source classification context:" in preview[0]
    assert "ClinVar" in preview[0]
    assert "Brief Interpretation(s)" in preview[0]
    assert "The collected evidence supports a cautious draft synthesis." in preview[0]
    assert "Likely pathogenic" in document_xml
    assert "LLM draft classification" in document_xml


def test_invalid_ai_classification_fails_only_the_classification_field() -> None:
    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(
            FakeLLMAdapter(_response(ai_classification="Probably pathogenic"))
        ),
        timestamp="2026-08-24T10:00:00Z",
    )

    assert result["status"] == "success"
    assert result["ai_classification"] is None
    assert result["interpretation"] == (
        "The collected evidence supports a cautious draft synthesis."
    )
    assert result["warnings"] == [
        "AI draft classification was unavailable because the model returned "
        "an unsupported value."
    ]


def test_missing_ai_classification_preserves_valid_interpretation() -> None:
    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(FakeLLMAdapter(_response(ai_classification=_MISSING))),
        timestamp="2026-08-24T10:00:00Z",
    )

    assert result["status"] == "success"
    assert result["ai_classification"] is None
    assert result["interpretation"] == (
        "The collected evidence supports a cautious draft synthesis."
    )
    assert result["warnings"] == [
        "AI draft classification was unavailable because the model response "
        "did not include it."
    ]


def test_historical_interpretation_and_report_without_ai_classification_load() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    legacy_interpretation = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(_response(ai_classification=_MISSING))),
        timestamp="2026-08-24T10:00:00Z",
    )
    legacy_interpretation.pop("ai_classification", None)
    legacy_interpretation["schema_version"] = "1.1"

    normalized = validate_variant_interpretation_result(
        legacy_interpretation,
        evidence=evidence,
    )
    draft = build_draft_variant_report(evidence, normalized, variant_index=0)
    draft["schema_version"] = "2.2"
    for content_key in ("machine_original_report", "reviewed_report"):
        draft[content_key]["variant_interpretation"].pop(
            "ai_classification",
            None,
        )
    original_digest = hashlib.sha256(
        json.dumps(
            draft["machine_original_report"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    draft["report_id"] = f"dvr-0-{original_digest}"
    legacy_draft = validate_draft_variant_report(draft)

    assert normalized["ai_classification"] is None
    assert normalized["status"] == "success"
    assert "Not independently determined" in render_draft_report_preview_pages(
        legacy_draft
    )[0]


def test_interpretation_failure_keeps_ai_classification_empty() -> None:
    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(
            FakeLLMAdapter(
                LLMResponse(
                    content="not-json",
                    model="stage9-2-3-model",
                    finish_reason="stop",
                )
            )
        ),
        timestamp="2026-08-24T10:00:00Z",
    )[0]

    assert result["status"] == "failed"
    assert result["ai_classification"] is None
    assert result["interpretation"] is None
