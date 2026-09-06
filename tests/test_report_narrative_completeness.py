"""Clinical summaries retain conclusions and caveats without display truncation."""
from copy import deepcopy
from html import escape
from io import BytesIO

import pytest
from docx import Document

from backend.report_data_projection import build_report_data_from_draft
from backend.report_docx import render_report_data_docx
from backend.variant_interpretation import VARIANT_INTERPRETATION_SYSTEM_PROMPT
from frontend.report_preview import render_draft_report_preview_pages
from tests.test_stage84_report_preview import _draft_report, _accept_machine_evidence_change


pytestmark = [pytest.mark.stage59_testing_v3, pytest.mark.testing_v3_interpretation]


@pytest.mark.parametrize("paragraph_break", [" ", "\n\n"])
@pytest.mark.parametrize("reviewer_summary", [False, True])
@pytest.mark.parametrize("surface", ["preview", "docx"])
def test_preview_and_export_preserve_full_narrative(paragraph_break, reviewer_summary, surface):
    narrative = paragraph_break.join([
        "The available evidence remains inconclusive for this variant.",
        "Source findings need interpretation in their disease context. " * 25,
        "Phenotype association alone does not establish causality.",
        "Final limitation: conflicting assertions remain unresolved (A < B).",
    ])
    report = _draft_report()
    content = report["reviewed_report"]
    content["variant_interpretation"]["narrative"] = narrative
    if reviewer_summary:
        content["reviewer_summary"] = narrative + " Reviewer assessment takes precedence."
        expected = content["reviewer_summary"]
    else:
        expected = narrative
    _accept_machine_evidence_change(report)
    original = deepcopy(report)

    if surface == "preview":
        page = render_draft_report_preview_pages(report)[0]
        assert escape(expected, quote=True) in page
    else:
        projected = build_report_data_from_draft(report, analysis_id="analysis-" + "c" * 32)
        document = Document(BytesIO(render_report_data_docx(projected)))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        brief_heading = paragraphs.index("Brief Interpretation(s):")
        assert paragraphs[brief_heading + 1] == expected
    assert report == original


def test_prompt_requests_complete_concise_narrative_without_hard_cutoff():
    prompt = VARIANT_INTERPRETATION_SYSTEM_PROMPT
    assert "3-5 complete sentences" in prompt
    assert "main evidence rationale" in prompt
    assert "material limitations and unresolved conflicts" in prompt
    assert "never omit a material caveat to meet this target" in prompt
