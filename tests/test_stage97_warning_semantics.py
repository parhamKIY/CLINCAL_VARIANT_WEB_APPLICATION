"""Stage 97 warning-semantics acceptance tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest

import frontend.evidence_review as evidence_review
from frontend.variant_status import build_variant_status_card
from frontend.warning_semantics import build_warning_notices


pytestmark = pytest.mark.stage97_warning_semantics


def _source(
    capability: str,
    status: str,
    *,
    source: str = "Synthetic source",
) -> dict[str, object]:
    return {
        "source": source,
        "capability": capability,
        "status": status,
        "method": "synthetic_method",
    }


def _report(
    *,
    sources: list[dict[str, object]] | None = None,
    phenotype_status: str = "supported",
    interpretation_status: str = "success",
    interpretation_warnings: list[str] | None = None,
    limitations: list[str] | None = None,
) -> dict[str, object]:
    return {
        "variant_index": 0,
        "reviewed_report": {
            "variant_summary": {
                "display_label": "PAX3 — GRCh38 2:100 C>T",
            },
            "phenotype_context": {"phenotype_status": phenotype_status},
            "variant_interpretation": {
                "status": interpretation_status,
                "warnings": interpretation_warnings or [],
                "failure_type": (
                    "ModelProviderTimeout"
                    if interpretation_status == "failed"
                    else None
                ),
            },
            "data_sources": sources or [],
            "limitations": limitations or [],
        },
    }


def test_no_match_is_info_and_never_a_provider_failure() -> None:
    notices = build_warning_notices(
        _report(
            sources=[
                _source("clinvar_evidence", "no_match", source="ClinVar")
            ]
        )
    )

    assert notices == [
        {
            "severity": "INFO",
            "message": (
                "No exact ClinVar allele record was found by the primary lookup."
            ),
        }
    ]


def test_no_literature_is_expected_absence_info() -> None:
    notices = build_warning_notices(
        _report(sources=[_source("literature", "no_match")])
    )

    assert notices == [
        {
            "severity": "INFO",
            "message": (
                "No qualifying variant-specific literature was found after the "
                "configured search strategy."
            ),
        }
    ]


def test_no_supported_phenotype_association_is_info() -> None:
    notices = build_warning_notices(_report(phenotype_status="not_supported"))

    assert notices == [
        {
            "severity": "INFO",
            "message": (
                "No supported phenotype association was found for this variant."
            ),
        }
    ]


@pytest.mark.parametrize("status", ["unavailable", "unsupported"])
def test_optional_source_loss_is_partial(status: str) -> None:
    notices = build_warning_notices(
        _report(sources=[_source("population_frequency", status)])
    )

    assert notices == [
        {
            "severity": "PARTIAL",
            "message": (
                "Population evidence could not be included. The report uses the "
                "evidence that remains available."
            ),
        }
    ]


def test_successful_fallback_suppresses_primary_source_failure_warning() -> None:
    notices = build_warning_notices(
        _report(
            sources=[
                _source("population_frequency", "unavailable"),
                _source("population_frequency", "available"),
            ]
        )
    )

    assert notices == []


def test_failed_interpretation_is_action_required_without_exception_name() -> None:
    notices = build_warning_notices(
        _report(
            interpretation_status="failed",
            interpretation_warnings=["ModelProviderTimeout: request exhausted"],
        )
    )

    assert notices == [
        {
            "severity": "ACTION REQUIRED",
            "message": (
                "Interpretation could not be produced after recovery attempts. "
                "Review the collected evidence and retry interpretation."
            ),
        }
    ]
    assert "ModelProviderTimeout" not in notices[0]["message"]


def test_missing_normalized_report_is_blocking() -> None:
    notices = build_warning_notices(None)

    assert notices[0]["severity"] == "BLOCKING"
    assert "Correct the input" in notices[0]["message"]


@pytest.mark.parametrize(
    "technical_message",
    [
        "invalid allele identity",
        "unsupported input format",
        "corrupt input",
        "required normalization impossible",
    ],
)
def test_minimum_input_invariant_is_blocking(
    technical_message: str,
) -> None:
    report = _report(limitations=[technical_message])

    notices = build_warning_notices(report)
    card = build_variant_status_card(report, variant_index=0, total=1)

    assert notices[0]["severity"] == "BLOCKING"
    assert technical_message not in notices[0]["message"]
    assert card["status"] == "Input requires attention"


def test_unclassified_implementation_warning_becomes_consequence_copy() -> None:
    notices = build_warning_notices(
        _report(limitations=["HTTPAdapterError at provider endpoint /v1/query"])
    )

    assert notices == [
        {
            "severity": "PARTIAL",
            "message": (
                "Some evidence has limitations. Review the report details before "
                "confirmation."
            ),
        }
    ]
    assert "HTTPAdapterError" not in notices[0]["message"]


def test_renderer_uses_compact_severity_badge_and_variant_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    @contextmanager
    def fake_container(**kwargs: object) -> Iterator[None]:
        observed["container"] = kwargs
        yield

    def fake_badge(label: str, **kwargs: object) -> None:
        observed["badge"] = (label, kwargs)

    def fake_write(message: str) -> None:
        observed["message"] = message

    monkeypatch.setattr(evidence_review.st, "container", fake_container)
    monkeypatch.setattr(evidence_review.st, "badge", fake_badge)
    monkeypatch.setattr(evidence_review.st, "write", fake_write)

    evidence_review._render_variant_notice(
        3,
        {
            "severity": "INFO",
            "message": "No exact record was found for this variant.",
        },
    )

    assert observed == {
        "container": {"horizontal": True, "vertical_alignment": "center"},
        "badge": (
            "Info",
            {"color": "blue", "icon": ":material/info:"},
        ),
        "message": "Variant 4: No exact record was found for this variant.",
    }
