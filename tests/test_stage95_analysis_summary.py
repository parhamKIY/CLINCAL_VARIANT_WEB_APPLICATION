"""Stage 95 concise user-facing analysis summary acceptance checks."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import frontend.ui as frontend_ui
from backend.pipeline import create_pipeline_result
from frontend.analysis_summary import (
    build_analysis_summary,
    input_validation_message,
)


pytestmark = pytest.mark.stage95_analysis_summary
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _evidence(status: str = "success") -> dict[str, object]:
    return {
        "source_statuses": {
            "vep": "success",
            "myvariant": "success",
            "clinvar": status,
            "clingen": "success",
        }
    }


def test_summary_uses_exact_variant_report_and_attention_counts() -> None:
    evidence = [_evidence() for _ in range(7)]
    evidence[1] = _evidence("no_match")
    evidence[3] = _evidence("unavailable")
    evidence[6] = _evidence("unsupported")
    result = {
        "status": "partial",
        "current_stage": "completed",
        "analysis_context": {"input_type": "excel"},
        "variant_count": 7,
        "evidence_objects": evidence,
        "draft_variant_reports": [{} for _ in range(7)],
        "variant_interpretation_results": [
            *({"status": "success"} for _ in range(6)),
            {"status": "failed"},
        ],
        "api_statuses": [{"status": "success"} for _ in range(3)],
    }

    assert build_analysis_summary(result) == {
        "headline": "Analysis complete",
        "input_message": "Excel input validated",
        "variants_analyzed": 7,
        "draft_reports_prepared": 7,
        "reports_ready": 0,
        "partial_source_coverage": 3,
        "interpretations_requiring_attention": 1,
    }


@pytest.mark.parametrize(
    ("input_type", "expected"),
    [
        ("excel", "Excel input validated"),
        ("manual", "Manual variant input validated"),
        ("vcf", "VCF input validated"),
        ("vcf_gz", "Compressed VCF input validated"),
        (None, "Input validated"),
    ],
)
def test_input_message_uses_actual_input_terminology(
    input_type: object,
    expected: str,
) -> None:
    assert input_validation_message(input_type) == expected


def test_untriggered_optional_capability_is_not_partial_coverage() -> None:
    evidence = _evidence()
    evidence["capability_results"] = {
        "population_frequency": {"status": "not_triggered"},
        "literature": {"status": "not_assessed"},
    }
    result = {
        "status": "success",
        "current_stage": "completed",
        "analysis_context": {"input_type": "vcf"},
        "evidence_objects": [evidence],
        "draft_variant_reports": [{}],
        "variant_interpretation_results": [{"status": "success"}],
    }

    assert build_analysis_summary(result)["partial_source_coverage"] == 0


def test_completed_ui_leads_with_summary_and_collapses_provider_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = create_pipeline_result()
    result["status"] = "partial"
    result["current_stage"] = "completed"
    result["progress_percent"] = 100
    result["analysis_context"]["input_type"] = "excel"
    result["variant_count"] = 2
    result["evidence_objects"] = [_evidence(), _evidence("no_match")]
    result["draft_variant_reports"] = [{}, {}]
    result["variant_interpretation_results"] = [
        {"status": "success"},
        {"status": "failed"},
    ]
    monkeypatch.setattr(
        frontend_ui,
        "render_evidence_review",
        lambda *_args, **_kwargs: st.subheader("STAGE95_REPORT_SURFACE"),
    )
    monkeypatch.setattr(
        frontend_ui,
        "render_analysis_results",
        lambda *_args, **_kwargs: st.subheader("STAGE95_TECHNICAL_SURFACE"),
    )

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = result
    app.run(timeout=10)

    assert not app.exception
    subheaders = [item.value for item in app.subheader]
    assert subheaders.index("Analysis complete") < subheaders.index(
        "STAGE95_REPORT_SURFACE"
    )
    assert any(item.value == "Excel input validated" for item in app.caption)
    rendered = [item.value for item in app.markdown]
    assert "**2** variants analyzed" in rendered
    assert "**2** draft reports prepared" in rendered
    assert "**0** reports ready" in rendered
    assert "**1** variant has partial source coverage" in rendered
    assert "**1** interpretation requires attention" in rendered
    assert "External API status" not in rendered


def test_provider_details_are_collapsed_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    @contextmanager
    def fake_expander(
        label: str,
        *,
        expanded: bool,
        icon: str,
    ) -> Iterator[None]:
        observed.update(label=label, expanded=expanded, icon=icon)
        yield

    monkeypatch.setattr(frontend_ui.st, "expander", fake_expander)
    monkeypatch.setattr(frontend_ui.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(frontend_ui.st, "write", lambda *_args, **_kwargs: None)

    frontend_ui._render_api_statuses(create_pipeline_result())

    assert observed == {
        "label": "Technical provider details",
        "expanded": False,
        "icon": ":material/api:",
    }


def test_excel_stage_records_do_not_claim_vcf_or_manual_input() -> None:
    result = create_pipeline_result()
    result["analysis_context"]["input_type"] = "excel"
    result["stages"][0].update(status="success", message="Validated vcf input.")
    result["stages"][1].update(
        status="success",
        message="Loaded all 7 filtered variants.",
    )
    messages: list[str] = []

    class Target:
        @staticmethod
        def write(message: str) -> None:
            messages.append(message)

    frontend_ui._write_stage_records(Target(), result)

    rendered = "\n".join(messages)
    assert "Excel input validated" in rendered
    assert "**Excel input:**" in rendered
    assert "VCF processing" not in rendered
    assert "manual input" not in rendered.casefold()
