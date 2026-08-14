"""Stage 98 technical diagnostics drawer acceptance tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest

import frontend.evidence_review as evidence_review
from frontend.technical_diagnostics import (
    ProviderDiagnostic,
    build_provider_diagnostics,
)


pytestmark = pytest.mark.stage98_technical_diagnostics


def _source(
    provider: str,
    capability: str,
    status: str,
    *,
    fallback_used: bool = False,
    primary_failure: str | None = None,
) -> dict[str, object]:
    return {
        "source": provider,
        "capability": capability,
        "status": "available" if status == "success" else status,
        "operational_status": status,
        "fallback_used": fallback_used,
        "primary_failure": primary_failure,
    }


def _result(
    sources: list[dict[str, object]],
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "draft_variant_reports": [
            {
                "variant_index": 0,
                "reviewed_report": {
                    "variant_summary": {
                        "assembly": "GRCh38",
                        "chrom": "2",
                        "pos": 100001,
                        "ref": "C",
                        "alt": "T",
                        "display_label": "PAX3 — GRCh38 2:100001 C>T",
                    },
                    "data_sources": sources,
                },
            }
        ],
        "evidence_objects": [evidence or {}],
    }


def test_diagnostics_include_every_required_field_and_safe_telemetry() -> None:
    result = _result(
        [_source("gnomAD", "population_frequency", "success")],
        {
            "conditional_enrichment": {
                "population_frequency": {
                    "provider": "gnomad",
                    "capability": "population_frequency",
                    "request_attempts": 2,
                    "duration_ms": 125.25,
                    "failure_reason": "secret provider payload",
                }
            }
        },
    )

    row = build_provider_diagnostics(result)[0][0]

    assert row == {
        "provider": "gnomAD",
        "variant_identity": "GRCh38 2:100001 C>T",
        "status": "success",
        "attempt_count": 2,
        "latency_ms": 125.25,
        "fallback_used": False,
        "failure_category": "none",
        "provider_note": (
            "gnomAD evidence was retained for this capability."
        ),
    }
    assert "secret provider payload" not in str(row)


def test_provider_rows_preserve_report_order_and_match_own_telemetry() -> None:
    result = _result(
        [
            _source("gnomAD", "population_frequency", "success"),
            _source(
                "Ensembl",
                "variant_annotation",
                "success",
            ),
        ],
        {
            "capability_results": {
                "population_frequency": {
                    "provider": "gnomad",
                    "capability": "population_frequency",
                    "request_attempts": 2,
                },
                "variant_annotation": {
                    "provider": "ensembl",
                    "capability": "variant_annotation",
                    "request_attempts": 1,
                },
            }
        },
    )

    rows = build_provider_diagnostics(result)[0]

    assert [row["provider"] for row in rows] == ["gnomAD", "Ensembl"]
    assert [row["attempt_count"] for row in rows] == [2, 1]


def test_fallback_row_retains_failure_category_and_provider_note() -> None:
    row = build_provider_diagnostics(
        _result(
            [
                _source(
                    "Ensembl Variation",
                    "population_frequency",
                    "success",
                    fallback_used=True,
                    primary_failure="timeout",
                )
            ]
        )
    )[0][0]

    assert row["fallback_used"] is True
    assert row["failure_category"] == "timeout"
    assert row["provider_note"] == (
        "Ensembl Variation supplied fallback evidence after the primary source "
        "reported timeout."
    )


def test_no_match_is_diagnostic_missingness_not_failure() -> None:
    row = build_provider_diagnostics(
        _result([_source("ClinVar", "clinvar_evidence", "no_match")])
    )[0][0]

    assert row["status"] == "no_match"
    assert row["failure_category"] == "none"
    assert row["provider_note"] == (
        "ClinVar returned a valid no-match result."
    )


def test_unavailable_provider_has_operational_failure_category() -> None:
    row = build_provider_diagnostics(
        _result([_source("ClinVar", "clinvar_evidence", "timeout")])
    )[0][0]

    assert row["status"] == "timeout"
    assert row["failure_category"] == "timeout"
    assert "remaining evidence was retained" in row["provider_note"]


def test_missing_attempt_and_latency_telemetry_are_explicitly_empty() -> None:
    row = build_provider_diagnostics(
        _result([_source("ClinVar", "clinvar_evidence", "success")])
    )[0][0]

    assert row["attempt_count"] is None
    assert row["latency_ms"] is None


def test_invalid_telemetry_is_not_displayed() -> None:
    row = build_provider_diagnostics(
        _result(
            [_source("ClinVar", "clinvar_evidence", "success")],
            {
                "provider": "clinvar",
                "capability": "clinvar_evidence",
                "request_attempts": -1,
                "latency_ms": "fast",
            },
        )
    )[0][0]

    assert row["attempt_count"] is None
    assert row["latency_ms"] is None


def test_drawer_is_collapsed_and_uses_all_required_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    rows: list[ProviderDiagnostic] = [
        {
            "provider": "ClinVar",
            "variant_identity": "GRCh38 2:100001 C>T",
            "status": "success",
            "attempt_count": 1,
            "latency_ms": 10.0,
            "fallback_used": False,
            "failure_category": "none",
            "provider_note": "ClinVar evidence was retained for this capability.",
        }
    ]

    @contextmanager
    def fake_expander(label: str, **kwargs: object) -> Iterator[None]:
        observed["expander"] = (label, kwargs)
        yield

    def fake_dataframe(data: object, **kwargs: object) -> None:
        observed["dataframe"] = (data, kwargs)

    monkeypatch.setattr(evidence_review.st, "expander", fake_expander)
    monkeypatch.setattr(evidence_review.st, "dataframe", fake_dataframe)

    evidence_review._render_technical_diagnostics(0, rows)

    assert observed["expander"] == (
        "Show technical details",
        {"expanded": False, "icon": ":material/monitoring:"},
    )
    rendered_rows, options = observed["dataframe"]  # type: ignore[misc]
    assert rendered_rows == rows
    assert options["column_order"] == (
        "provider",
        "variant_identity",
        "status",
        "attempt_count",
        "latency_ms",
        "fallback_used",
        "failure_category",
        "provider_note",
    )
    assert options["hide_index"] is True
    assert options["width"] == "stretch"
    assert options["key"] == "variant_provider_diagnostics_0"


def test_drawer_marks_unavailable_telemetry_as_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    rows: list[ProviderDiagnostic] = [
        {
            "provider": "ClinVar",
            "variant_identity": "GRCh38 2:100001 C>T",
            "status": "no_match",
            "attempt_count": None,
            "latency_ms": None,
            "fallback_used": False,
            "failure_category": "none",
            "provider_note": "ClinVar returned a valid no-match result.",
        }
    ]

    @contextmanager
    def fake_expander(_label: str, **_kwargs: object) -> Iterator[None]:
        yield

    def fake_dataframe(data: object, **_kwargs: object) -> None:
        observed["data"] = data

    monkeypatch.setattr(evidence_review.st, "expander", fake_expander)
    monkeypatch.setattr(evidence_review.st, "dataframe", fake_dataframe)

    evidence_review._render_technical_diagnostics(0, rows)

    rendered = observed["data"]
    assert isinstance(rendered, list)
    assert rendered[0]["attempt_count"] == "Not recorded"
    assert rendered[0]["latency_ms"] == "Not recorded"
