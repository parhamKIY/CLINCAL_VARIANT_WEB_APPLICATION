"""Stage 104 primary status and warning UX acceptance suite."""

from __future__ import annotations

import inspect

from streamlit.testing.v1 import AppTest

import pytest

from frontend.analysis_summary import build_analysis_summary
from frontend.evidence_review import (
    _render_technical_diagnostics,
    _render_variant_status_cards,
)
from frontend.variant_status import build_variant_status_cards


pytestmark = pytest.mark.stage104_status_warning_acceptance


def _source(
    source: str,
    capability: str,
    status: str = "available",
) -> dict[str, object]:
    return {
        "source": source,
        "capability": capability,
        "status": status,
        "operational_status": status,
        "method": "exact_allele_lookup",
        "fallback_used": False,
        "primary_failure": None,
    }


def _report(
    index: int,
    *,
    clinvar_status: str = "available",
    interpretation_status: str = "success",
) -> dict[str, object]:
    return {
        "variant_index": index,
        "reviewed_report": {
            "variant_summary": {
                "display_label": f"GENE{index + 1} - GRCh38 2:{100 + index} C>T",
                "assembly": "GRCh38",
                "chrom": "2",
                "pos": 100 + index,
                "ref": "C",
                "alt": "T",
                "gene": f"GENE{index + 1}",
                "hgvs_c": f"c.{100 + index}C>T",
            },
            "phenotype_context": {"phenotype_status": "supported"},
            "variant_interpretation": {
                "status": interpretation_status,
                "warnings": (
                    ["ModelProviderTimeout: request exhausted"]
                    if interpretation_status == "failed"
                    else []
                ),
                "failure_type": (
                    "ModelProviderTimeout"
                    if interpretation_status == "failed"
                    else None
                ),
            },
            "data_sources": [
                _source("Ensembl", "variant_annotation"),
                _source("gnomAD", "population_frequency"),
                _source("ClinVar", "clinvar_evidence", clinvar_status),
            ],
            "limitations": [
                "Decision-support report only; qualified review is required."
            ],
        },
    }


def _ux_result() -> dict[str, object]:
    return {
        "status": "partial",
        "current_stage": "completed",
        "analysis_context": {"input_type": "excel"},
        "variant_count": 3,
        "evidence_objects": [
            {
                "source_statuses": {
                    "vep": "success",
                    "myvariant": "success",
                    "clinvar": "success",
                    "clingen": "success",
                }
            },
            {
                "source_statuses": {
                    "vep": "success",
                    "myvariant": "success",
                    "clinvar": "unavailable",
                    "clingen": "success",
                }
            },
            {
                "source_statuses": {
                    "vep": "success",
                    "myvariant": "success",
                    "clinvar": "success",
                    "clingen": "success",
                }
            },
        ],
        "draft_variant_reports": [
            _report(0),
            _report(1, clinvar_status="unavailable"),
            _report(2, interpretation_status="failed"),
        ],
        "variant_interpretation_results": [
            {"status": "success"},
            {"status": "success"},
            {"status": "failed"},
        ],
    }


def _acceptance_app() -> None:
    from frontend.evidence_review import _render_variant_status_cards
    from frontend.ui import _render_analysis_summary
    from tests.test_stage104_status_warning_acceptance import _ux_result

    result = _ux_result()
    _render_analysis_summary(result)  # type: ignore[arg-type]
    _render_variant_status_cards(result)  # type: ignore[arg-type]


def test_analysis_counts_answer_variant_and_ready_report_questions() -> None:
    summary = build_analysis_summary(_ux_result())

    assert summary["variants_analyzed"] == 3
    assert summary["draft_reports_prepared"] == 3
    assert summary["reports_ready"] == 2
    assert summary["partial_source_coverage"] == 1
    assert summary["interpretations_requiring_attention"] == 1


def test_attention_variant_and_missing_source_are_explicit() -> None:
    cards = build_variant_status_cards(_ux_result())

    assert [card["status"] for card in cards] == [
        "Report ready",
        "Report ready with partial evidence",
        "Interpretation requires attention",
    ]
    assert cards[1]["heading"].startswith("Variant 2 of 3")
    assert cards[1]["clinvar"] == "ClinVar: unavailable"
    assert cards[2]["heading"].startswith("Variant 3 of 3")
    assert cards[2]["interpretation"] == "Interpretation requires attention"


def test_missing_evidence_and_application_failure_are_distinct() -> None:
    cards = build_variant_status_cards(_ux_result())

    assert cards[1]["notices"] == [
        {
            "severity": "PARTIAL",
            "message": (
                "ClinVar evidence could not be included. The report uses the "
                "evidence that remains available."
            ),
        }
    ]
    assert cards[2]["notices"][0] == {
        "severity": "ACTION REQUIRED",
        "message": (
            "Interpretation could not be produced after recovery attempts. "
            "Review the collected evidence and retry interpretation."
        ),
    }
    assert "ModelProviderTimeout" not in str(cards[2])


def test_primary_ui_answers_questions_with_drawers_closed() -> None:
    app = AppTest.from_function(_acceptance_app).run(timeout=10)

    assert not app.exception
    rendered = [item.value for item in app.markdown]
    assert "**3** variants analyzed" in rendered
    assert "**2** reports ready" in rendered
    assert "**Variant 2 of 3 \u2014 GENE2 c.101C>T**" in rendered
    assert "Status: **Report ready with partial evidence**" in rendered
    assert "ClinVar: unavailable" in rendered
    assert "**Variant 3 of 3 \u2014 GENE3 c.102C>T**" in rendered
    assert "Status: **Interpretation requires attention**" in rendered
    card_renderer = inspect.getsource(_render_variant_status_cards)
    drawer_renderer = inspect.getsource(_render_technical_diagnostics)
    assert card_renderer.index("card['status']") < card_renderer.index(
        "_render_technical_diagnostics"
    )
    assert '"Show technical details"' in drawer_renderer
    assert "expanded=False" in drawer_renderer
