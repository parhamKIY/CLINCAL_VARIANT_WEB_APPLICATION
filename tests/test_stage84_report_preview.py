"""Stage 84 report-first HTML preview acceptance tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from backend.pipeline import create_pipeline_result
import frontend.ui as frontend_ui
from frontend.report_preview import (
    render_draft_report_preview_pages,
    stable_allele_identity,
)


pytestmark = pytest.mark.stage84_report_preview
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _draft_report() -> dict[str, object]:
    content = {
        "variant_summary": {
            "display_label": "GENE<1> — GRCh38 2:100001 C>T",
            "assembly": "GRCh38",
            "chrom": "2",
            "pos": 100001,
            "ref": "C",
            "alt": "T",
            "gene": "GENE<1>",
            "gene_id": "HGNC:1",
            "transcript": "NM_000001.1",
            "hgvs_c": "c.101C>T",
            "hgvs_p": "p.(Arg34Trp)",
            "consequence": "missense_variant",
            "rsid": "rs123456",
        },
        "phenotype_context": {
            "accepted_hpo_terms": ["HP:0001250"],
            "matched_hpo_terms": ["HP:0001250"],
            "phenotype_score": 0.68,
            "phenotype_status": "partially_supported",
            "phenotype_to_gene_summary": ["Source-supported gene context."],
            "disease_context": ["Synthetic condition (MONDO:0000001)."],
        },
        "evidence_sections": [
            {
                "source": "Ensembl VEP",
                "status": "success",
                "items": [
                    {"label": "Consequence", "value": "missense_variant"},
                    {"label": "Coding HGVS", "value": "c.101C>T"},
                ],
            },
            {
                "source": "NCBI ClinVar",
                "status": "success",
                "items": [
                    {"label": "Significance", "value": "uncertain significance"},
                    {
                        "label": "Review status",
                        "value": "criteria provided, single submitter",
                    },
                ],
            },
        ],
        "conflict_summary": {
            "detected": False,
            "status": "none",
            "severity": "none",
            "findings": [],
        },
        "variant_interpretation": {
            "status": "success",
            "narrative": (
                "The exact allele has source-attributed evidence. "
                "The retained evidence remains insufficient for an independent "
                "classification and requires qualified human review."
            ),
            "conflict_assessment": "No material conflict was identified.",
            "warnings": [],
            "model": "synthetic-no-call",
            "prompt_version": "test-v1",
            "generated_at": "2026-08-11T08:00:00Z",
            "failure_type": None,
        },
        "reviewer_summary": None,
        "reviewer_notes": [],
        "references": [
            {
                "reference_id": "R1",
                "source": "PubMed",
                "identifier_type": "PMID",
                "identifier": "25741868",
                "title": "Sequence variant interpretation standards",
                "canonical_url": "https://pubmed.ncbi.nlm.nih.gov/25741868/",
                "url_status": "validated",
            }
        ],
        "provenance": {
            "evidence_schema_version": "3.0",
            "interpretation_schema_version": "3.0",
            "providers": ["Ensembl VEP: success", "NCBI ClinVar: success"],
            "upstream_sources": ["Ensembl VEP", "NCBI ClinVar"],
            "interpretation_model": "synthetic-no-call",
            "interpretation_prompt_version": "test-v1",
        },
        "limitations": [
            "Decision-support report only; qualified human review is required."
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "schema_version": "2.1",
        "report_id": f"dvr-0-{digest}",
        "variant_index": 0,
        "machine_original_report": deepcopy(content),
        "reviewed_report": deepcopy(content),
        "edit_history": [],
        "include_in_final_report": True,
        "selection_history": [],
        "review_status": "draft",
        "created_at": "2026-08-11T08:00:00Z",
        "updated_at": "2026-08-11T08:00:00Z",
    }


def test_preview_has_three_clear_professor_family_pages() -> None:
    pages = render_draft_report_preview_pages(_draft_report())

    assert len(pages) == 3
    assert "NGS Result Report" in pages[0]
    assert "Brief Interpretation(s)" in pages[0]
    assert "Main Finding(s) in Detail" in pages[1]
    assert "Variant interpretation" in pages[1]
    assert "Variant(s) classification" in pages[2]
    assert "Data Sources" in pages[2]
    assert all('class="cv-report-page"' in page for page in pages)


def test_preview_uses_assembly_qualified_allele_navigation_identity() -> None:
    report = _draft_report()

    assert stable_allele_identity(report) == "GRCh38 chr2:100001 C>T"
    assert "GRCh38 chr2:100001 C&gt;T" in render_draft_report_preview_pages(
        report
    )[0]


def test_preview_escapes_dynamic_content_and_has_no_executable_script() -> None:
    combined = "".join(render_draft_report_preview_pages(_draft_report()))

    assert "GENE&lt;1&gt;" in combined
    assert "GENE<1>" not in combined
    assert "<script" not in combined.lower()
    assert "javascript:" not in combined.lower()
    assert 'rel="noopener noreferrer"' in combined


def test_preview_exposes_source_findings_without_raw_json() -> None:
    page = render_draft_report_preview_pages(_draft_report())[1]

    assert "Source-attributed main findings" in page
    assert "Ensembl VEP" in page
    assert "NCBI ClinVar" in page
    assert "uncertain significance" in page
    assert "reviewed_user_report" not in page


def test_completed_analysis_opens_report_before_provider_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = create_pipeline_result()
    result["status"] = "success"
    result["current_stage"] = "completed"
    result["progress_percent"] = 100
    monkeypatch.setattr(
        frontend_ui,
        "render_evidence_review",
        lambda *_args, **_kwargs: st.markdown("STAGE84_REPORT_SURFACE"),
    )
    monkeypatch.setattr(
        frontend_ui,
        "render_analysis_results",
        lambda *_args, **_kwargs: st.markdown("STAGE84_TECHNICAL_SURFACE"),
    )

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = result
    app.run(timeout=10)

    assert not app.exception
    assert [tab.label for tab in app.tabs[-2:]] == [
        "Clinical report review",
        "Analysis and provider details",
    ]
    rendered = [item.value for item in app.markdown]
    assert rendered.index("STAGE84_REPORT_SURFACE") < rendered.index(
        "STAGE84_TECHNICAL_SURFACE"
    )
