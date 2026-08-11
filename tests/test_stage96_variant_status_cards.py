"""Stage 96 variant-first status-card acceptance tests."""

from __future__ import annotations

from copy import deepcopy
import inspect

import pytest

from frontend.evidence_review import (
    _render_technical_diagnostics,
    _render_variant_status_cards,
)
from frontend.variant_status import (
    build_variant_status_card,
    build_variant_status_cards,
)


pytestmark = pytest.mark.stage96_variant_status_cards


def _source(capability: str, status: str = "available") -> dict[str, object]:
    return {
        "source": {
            "variant_annotation": "Ensembl",
            "population_frequency": "gnomAD",
            "clinvar_evidence": "ClinVar",
        }[capability],
        "capability": capability,
        "status": status,
        "method": "exact_allele_lookup",
    }


def _report(index: int = 0) -> dict[str, object]:
    return {
        "variant_index": index,
        "reviewed_report": {
            "variant_summary": {
                "display_label": "PAX3 — GRCh38 2:100 C>T",
                "gene": "PAX3",
                "hgvs_c": "c.100C>T",
            },
            "phenotype_context": {"phenotype_status": "supported"},
            "variant_interpretation": {
                "status": "success",
                "warnings": [],
                "failure_type": None,
            },
            "data_sources": [
                _source("variant_annotation"),
                _source("population_frequency"),
                _source("clinvar_evidence"),
            ],
            "limitations": [
                "Decision-support report only; qualified review is required."
            ],
        },
    }


def test_complete_variant_has_report_ready_status_and_required_lines() -> None:
    card = build_variant_status_card(_report(), variant_index=0, total=7)

    assert card["heading"] == "Variant 1 of 7 — PAX3 c.100C>T"
    assert card["status"] == "Report ready"
    assert card["annotation"] == "Annotation available"
    assert card["population"] == "Population evidence available"
    assert card["clinvar"] == "ClinVar available"
    assert card["phenotype"] == "Phenotype relationship supported"
    assert card["interpretation"] == "Interpretation completed"


def test_legitimate_missingness_produces_partial_evidence_status() -> None:
    report = _report()
    content = report["reviewed_report"]
    assert isinstance(content, dict)
    content["data_sources"] = [
        _source("variant_annotation"),
        _source("population_frequency"),
        _source("clinvar_evidence", "no_match"),
    ]
    content["phenotype_context"] = {"phenotype_status": "not_supported"}

    card = build_variant_status_card(report, variant_index=3, total=7)

    assert card["status"] == "Report ready with partial evidence"
    assert card["clinvar"] == "ClinVar: no exact record"
    assert card["phenotype"] == (
        "Phenotype relationship: no supported association"
    )
    assert card["interpretation"] == "Interpretation completed"


def test_failed_interpretation_uses_stable_attention_label() -> None:
    report = _report()
    content = report["reviewed_report"]
    assert isinstance(content, dict)
    content["variant_interpretation"] = {
        "status": "failed",
        "warnings": ["The interpretation could not be completed."],
        "failure_type": "DraftVariantReportError",
    }

    card = build_variant_status_card(report, variant_index=0, total=1)

    assert card["status"] == "Interpretation requires attention"
    assert "DraftVariantReportError" not in card["status"]
    assert card["interpretation"] == "Interpretation requires attention"


def test_missing_report_uses_input_attention_status() -> None:
    card = build_variant_status_card(None, variant_index=1, total=2)

    assert card["status"] == "Input requires attention"
    assert card["notices"] == [
        {
            "severity": "BLOCKING",
            "message": (
                "A valid normalized variant report is unavailable. Correct the "
                "input before continuing."
            ),
        }
    ]


def test_all_declared_variants_receive_an_ordered_card() -> None:
    cards = build_variant_status_cards(
        {"variant_count": 3, "draft_variant_reports": [_report(0), _report(2)]}
    )

    assert [card["variant_index"] for card in cards] == [0, 1, 2]
    assert cards[1]["status"] == "Input requires attention"


def test_notices_remain_attached_to_their_own_variant() -> None:
    first = _report(0)
    second = deepcopy(_report(1))
    first_content = first["reviewed_report"]
    second_content = second["reviewed_report"]
    assert isinstance(first_content, dict)
    assert isinstance(second_content, dict)
    first_content["limitations"] = ["Variant one warning."]
    second_content["limitations"] = ["Variant two warning."]

    cards = build_variant_status_cards(
        {"variant_count": 2, "draft_variant_reports": [first, second]}
    )

    assert cards[0]["notices"][0]["message"].startswith("Some evidence")
    assert cards[1]["notices"][0]["message"].startswith("Some evidence")


def test_provider_details_are_variant_local_and_expandable_by_contract() -> None:
    card = build_variant_status_card(_report(), variant_index=0, total=1)
    renderer = inspect.getsource(_render_variant_status_cards)
    diagnostics_renderer = inspect.getsource(_render_technical_diagnostics)

    assert len(card["technical_details"]) == 3
    assert "st.container(border=True)" in renderer
    assert '"Show technical details"' in diagnostics_renderer
    assert "expanded=False" in diagnostics_renderer
    assert "_render_variant_notice(card[\"variant_index\"], notice)" in renderer


def test_primary_status_vocabulary_is_exact_and_bounded() -> None:
    expected = {
        "Report ready",
        "Report ready with partial evidence",
        "Interpretation requires attention",
        "Input requires attention",
    }
    observed = {
        build_variant_status_card(_report(), variant_index=0, total=1)["status"],
        build_variant_status_card(None, variant_index=0, total=1)["status"],
    }
    partial = _report()
    partial_content = partial["reviewed_report"]
    assert isinstance(partial_content, dict)
    partial_content["phenotype_context"] = {"phenotype_status": "no_match"}
    observed.add(
        build_variant_status_card(partial, variant_index=0, total=1)["status"]
    )
    failed = _report()
    failed_content = failed["reviewed_report"]
    assert isinstance(failed_content, dict)
    failed_content["variant_interpretation"] = {"status": "failed"}
    observed.add(
        build_variant_status_card(failed, variant_index=0, total=1)["status"]
    )

    assert observed == expected
