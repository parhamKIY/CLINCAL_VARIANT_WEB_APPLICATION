"""Stage 10.2 execution-trace UI acceptance tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from backend.execution_trace import AnalysisExecutionTrace
from backend.pipeline import PipelineResult, create_pipeline_result
from frontend.execution import AnalysisJob, execute_analysis


pytestmark = [
    pytest.mark.regression,
    pytest.mark.stage59_testing_v3,
]


def _snapshot(*events: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "run_id": f"run-{'1' * 32}",
        "status": "completed",
        "max_events": 500,
        "event_count": len(events),
        "events_dropped": 0,
        "events": list(events),
    }


def _event(
    sequence: int,
    event_type: str,
    *,
    scope: str = "analysis",
    **fields: object,
) -> dict[str, object]:
    event: dict[str, object] = {
        "sequence": sequence,
        "occurred_at": f"2026-08-24T00:00:0{sequence}Z",
        "event_type": event_type,
        "scope": scope,
    }
    event.update(fields)
    return event


SUCCESS_SNAPSHOT = _snapshot(
    _event(1, "analysis_started", status="running"),
    _event(2, "input_validated", scope="stage", status="success"),
    _event(
        3,
        "variant_processing_completed",
        scope="variant",
        variant_index=0,
        status="success",
    ),
    _event(
        4,
        "evidence_collection_completed",
        scope="stage",
        status="success",
    ),
    _event(
        5,
        "llm_output_classification_generated",
        scope="llm",
        variant_index=0,
        status="success",
    ),
    _event(
        6,
        "llm_output_interpretation_generated",
        scope="llm",
        variant_index=0,
        status="success",
    ),
    _event(7, "report_generated", scope="report", status="success"),
    _event(
        8,
        "analysis_completed",
        status="success",
        variant_count=1,
        succeeded_count=1,
        failed_count=0,
    ),
)


def test_successful_trace_renders_timeline_and_variant_summary() -> None:
    from frontend.execution_trace import build_execution_trace_view

    view = build_execution_trace_view(SUCCESS_SNAPSHOT)
    rendered = "\n".join(item["message"] for item in view["timeline"])

    assert view["available"] is True
    assert "Input validation completed" in rendered
    assert "Evidence collection completed" in rendered
    assert "Report generated" in rendered
    assert view["variants"] == {
        "processed": 1,
        "completed": 1,
        "failed": 0,
        "interpreted": 1,
    }


def test_provider_fallback_and_source_modes_are_visible() -> None:
    snapshot = _snapshot(
        _event(
            1,
            "provider_attempt_completed",
            scope="provider",
            provider="ensembl_vep",
            capability="annotation",
            attempt=1,
            status="timeout",
            reason_category="timeout",
            source_mode="live_provider",
        ),
        _event(
            2,
            "fallback_activated",
            scope="provider",
            variant_index=0,
            provider="VariantValidator",
            capability="vep",
            status="success",
            fallback_for="ensembl_vep",
            reason_category="timeout",
            source_mode="live_provider",
        ),
        _event(
            3,
            "provider_source_selected",
            scope="provider",
            provider="clinvar",
            capability="clinvar_clinical_evidence",
            status="success",
            source_mode="repository_cache",
        ),
    )

    from frontend.execution_trace import build_execution_trace_view

    view = build_execution_trace_view(snapshot)
    rendered = "\n".join(item["message"] for item in view["providers"])

    assert "Ensembl VEP" in rendered
    assert "Provider timed out" in rendered
    assert "Fallback activated: VariantValidator" in rendered
    assert "Primary source: Ensembl VEP" in rendered
    assert "Source: Live provider" in rendered
    assert "Source: Repository cache" in rendered


def test_llm_success_retry_and_safe_failure_states_render() -> None:
    snapshot = _snapshot(
        _event(
            1,
            "llm_retry_scheduled",
            scope="llm",
            attempt=2,
            status="scheduled",
            reason_category="request_timeout",
        ),
        _event(
            2,
            "llm_output_classification_generated",
            scope="llm",
            variant_index=0,
            status="success",
        ),
        _event(
            3,
            "llm_output_interpretation_generated",
            scope="llm",
            variant_index=0,
            status="success",
        ),
        _event(
            4,
            "variant_interpretation_failed",
            scope="variant",
            variant_index=1,
            status="failed",
            reason_category="request_timeout",
        ),
    )

    from frontend.execution_trace import build_execution_trace_view

    view = build_execution_trace_view(snapshot)
    rendered = "\n".join(item["message"] for item in view["llm"])

    assert "Interpretation retry scheduled" in rendered
    assert "AI classification generated" in rendered
    assert "Interpretation generated" in rendered
    assert "AI service timed out" in rendered
    assert "request_timeout" not in rendered


def test_analysis_job_exposes_detached_runtime_trace_snapshot() -> None:
    trace = AnalysisExecutionTrace(max_events=10)
    trace.bind_run_id(f"run-{'2' * 32}")
    trace.record("analysis_started", scope="analysis", status="running")
    result = create_pipeline_result()
    job = AnalysisJob(lambda _: deepcopy(result), execution_trace=trace)

    view = job.view()
    assert view.execution_trace is not None
    view.execution_trace["events"].clear()

    assert trace.snapshot()["event_count"] == 1


def test_frontend_execution_forwards_trace_without_pipeline_result_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = AnalysisExecutionTrace(max_events=10)
    observed: list[object] = []

    def fake_run_analysis(**kwargs: object) -> PipelineResult:
        observed.append(kwargs.get("execution_trace"))
        return create_pipeline_result()

    monkeypatch.setattr("frontend.execution.run_analysis", fake_run_analysis)
    result = execute_analysis(
        uploaded_vcf=None,
        manual_variants=[
            {"chrom": "1", "pos": 100, "ref": "A", "alt": "G"}
        ],
        phenotypes=[],
        execution_trace=trace,
    )

    assert observed == [trace]
    assert "execution_trace" not in result


def test_runtime_progress_remains_with_authoritative_trace_panel() -> None:
    def app_body() -> None:
        from backend.pipeline import create_pipeline_result
        from frontend.ui import (
            _render_pipeline_status,
            _render_trace_snapshot,
        )

        result = create_pipeline_result()
        snapshot = {
            "schema_version": "1.0",
            "run_id": f"run-{'1' * 32}",
            "status": "completed",
            "max_events": 500,
            "event_count": 1,
            "events_dropped": 0,
            "events": [
                {
                    "sequence": 1,
                    "occurred_at": "2026-08-24T00:00:00Z",
                    "event_type": "analysis_completed",
                    "scope": "analysis",
                    "status": "success",
                    "variant_count": 1,
                    "succeeded_count": 1,
                    "failed_count": 0,
                }
            ],
        }
        _render_pipeline_status(result)
        _render_trace_snapshot(snapshot)

    app = AppTest.from_function(app_body).run(timeout=10)

    assert not app.exception
    assert "Analysis status" in [item.value for item in app.subheader]
    assert "Analysis execution trace" in [
        item.value for item in app.subheader
    ]
    assert len(app.get("progress")) == 1
    assert any(
        item.label == "Analysis in progress"
        for item in app.get("status")
    )
    assert all(
        item.label != "Technical provider details"
        for item in app.get("status")
    )
