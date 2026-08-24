"""Stage 11.4 legacy provider-progress UI removal acceptance tests."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import frontend.ui as frontend_ui


pytestmark = pytest.mark.stage11_4_provider_ui_cleanup


def test_active_analysis_keeps_progress_and_trace_without_legacy_panel() -> None:
    def app_body() -> None:
        from backend.pipeline import create_pipeline_result
        import frontend.ui as frontend_ui

        result = create_pipeline_result()
        result["status"] = "running"
        result["current_stage"] = "annotation"
        result["progress_percent"] = 40
        result["stages"][2].update(
            status="running",
            progress_percent=40,
            message="Annotating canonical variants.",
        )
        frontend_ui._render_pipeline_status(result)
        frontend_ui._render_trace_snapshot(
            {
                "schema_version": "1.0",
                "run_id": f"run-{'4' * 32}",
                "status": "running",
                "max_events": 500,
                "event_count": 2,
                "events_dropped": 0,
                "events": [
                    {
                        "sequence": 1,
                        "occurred_at": "2026-08-24T00:00:00Z",
                        "event_type": "analysis_started",
                        "scope": "analysis",
                        "status": "running",
                    },
                    {
                        "sequence": 2,
                        "occurred_at": "2026-08-24T00:00:01Z",
                        "event_type": "provider_attempt_completed",
                        "scope": "provider",
                        "provider": "clinvar",
                        "capability": "clinvar_clinical_evidence",
                        "attempt": 1,
                        "status": "success",
                        "source_mode": "live_provider",
                    },
                ],
            }
        )

    app = AppTest.from_function(app_body).run(timeout=10)

    assert not app.exception
    assert len(app.get("progress")) == 1
    assert any(item.label == "Analysis in progress" for item in app.get("status"))
    assert "Analysis execution trace" in [item.value for item in app.subheader]
    assert all(
        item.label != "Technical provider details" for item in app.get("status")
    )
    assert any("ClinVar" in item.value for item in app.markdown)


def test_historical_analysis_without_trace_degrades_gracefully() -> None:
    def app_body() -> None:
        from backend.pipeline import create_pipeline_result
        import frontend.ui as frontend_ui

        result = create_pipeline_result()
        result["status"] = "partial"
        result["current_stage"] = "completed"
        result["progress_percent"] = 100
        frontend_ui._render_pipeline_status(result)
        frontend_ui._render_trace_snapshot(None)

    app = AppTest.from_function(app_body).run(timeout=10)

    assert not app.exception
    assert len(app.get("progress")) == 1
    assert "Analysis execution trace" in [item.value for item in app.subheader]
    assert any(
        "Older restored analyses may not include a runtime trace" in item.value
        for item in app.info
    )
    assert all(
        item.label != "Technical provider details" for item in app.get("status")
    )


def test_legacy_provider_renderer_is_not_part_of_frontend_contract() -> None:
    assert not hasattr(frontend_ui, "_render_api_statuses")
