"""Reviewer-facing phenotype status vocabulary consistency."""

from __future__ import annotations

import pytest

from frontend.source_status import build_reviewer_source_status
from frontend.variant_status import build_variant_status_card


pytestmark = pytest.mark.stage97_warning_semantics


def _report(phenotype_status: str) -> dict[str, object]:
    return {
        "variant_index": 0,
        "reviewed_report": {
            "variant_summary": {
                "display_label": "ERCC8 — GRCh38 5:60944943 C>T",
                "gene": "ERCC8",
                "hgvs_c": "NM_000082.4:c.66G>A",
            },
            "phenotype_context": {"phenotype_status": phenotype_status},
            "variant_interpretation": {"status": "success"},
            "data_sources": [],
            "limitations": [],
        },
    }


def _reviewer_status(phenotype_status: str) -> dict[str, str | None]:
    return build_reviewer_source_status(
        {
            "source": "Phenotype evidence",
            "capability": "phenotype_gene",
            "status": phenotype_status,
            "operational_status": phenotype_status,
            "provider_role": "primary",
            "fallback_used": False,
        }
    )


def test_exact_match_is_supported_consistently_across_review_surfaces() -> None:
    card = build_variant_status_card(
        _report("exact_match"),
        variant_index=0,
        total=1,
    )
    source_status = _reviewer_status("exact_match")

    assert card["phenotype"] == "Phenotype relationship supported"
    assert source_status["category"] == "Evidence available"
    assert source_status["message"] == (
        "Phenotype evidence supplied usable source-attributed evidence."
    )


def test_no_exact_match_is_no_supported_association_everywhere() -> None:
    card = build_variant_status_card(
        _report("no_exact_match"),
        variant_index=0,
        total=1,
    )
    source_status = _reviewer_status("no_exact_match")

    assert card["phenotype"] == (
        "Phenotype relationship: no supported association"
    )
    assert card["status"] == "Report ready with partial evidence"
    assert source_status["category"] == "Expected absence"
    assert source_status["message"] == (
        "No supported phenotype association was identified."
    )


@pytest.mark.parametrize("status", ["partial_match", "partially_supported"])
def test_partial_match_remains_supported(status: str) -> None:
    card = build_variant_status_card(
        _report(status),
        variant_index=0,
        total=1,
    )

    assert card["phenotype"] == "Phenotype relationship supported"


def test_not_applicable_remains_not_assessed() -> None:
    card = build_variant_status_card(
        _report("not_applicable"),
        variant_index=0,
        total=1,
    )
    source_status = _reviewer_status("not_applicable")

    assert card["phenotype"] == "Phenotype relationship: not assessed"
    assert source_status["category"] == "Not queried"


def test_unavailable_remains_distinct_from_not_assessed_and_no_match() -> None:
    card = build_variant_status_card(
        _report("unavailable"),
        variant_index=0,
        total=1,
    )
    source_status = _reviewer_status("unavailable")

    assert card["phenotype"] == "Phenotype relationship: unavailable"
    assert card["status"] == "Report ready with partial evidence"
    assert source_status["category"] == "Provider unavailable"
