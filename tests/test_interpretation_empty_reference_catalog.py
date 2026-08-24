"""Regression for valid interpretation when no citation catalog exists."""

from __future__ import annotations

import json
from copy import deepcopy

from backend.llm import LLMClient, LLMResponse
from backend.variant_interpretation import interpret_variant
from backend.variant_report import build_draft_variant_report
from frontend.report_preview import render_draft_report_preview_pages
from test_pipeline import (
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
)


def _response(*, citation: str = "") -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Likely pathogenic",
                "interpretation": (
                    "The truncating variant supports a cautious likely "
                    f"pathogenic draft assessment.{citation}"
                ),
                "conflict_assessment": "No material source conflict was identified.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="empty-catalog-model",
        finish_reason="stop",
    )


def test_empty_catalog_unknown_citation_repairs_to_classification_and_narrative() -> None:
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    evidence["references"] = []
    evidence["clingen_curations"] = []
    evidence["pathogenicity"]["clingen_context"] = []
    adapter = SequenceLLMAdapter(
        [_response(citation=" [R1]"), _response()]
    )

    result = interpret_variant(
        evidence,
        client=LLMClient(adapter),
        timestamp="2026-08-24T21:00:00Z",
    )
    report = build_draft_variant_report(evidence, result, variant_index=0)
    preview = render_draft_report_preview_pages(report)

    assert "allowed reference catalog is empty" in (
        adapter.requests[0].messages[1].content
    )
    assert "unknown citation" in adapter.requests[1].messages[1].content
    assert result["status"] == "success"
    assert result["ai_classification"] == "Likely pathogenic"
    assert result["interpretation"] == (
        "The truncating variant supports a cautious likely pathogenic draft "
        "assessment."
    )
    assert result["cited_reference_ids"] == []
    assert "System classification: Likely pathogenic" in preview[0]
    assert "The truncating variant supports" in preview[0]
