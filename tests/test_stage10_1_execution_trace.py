"""Stage 10.1 bounded backend execution-trace acceptance tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from backend.evidence_repository import EvidenceRepository
from backend.execution_trace import (
    AnalysisExecutionTrace,
    bind_execution_trace,
    reset_execution_trace,
)
from backend.llm import (
    LLMClient,
    LLMRequestError,
    LLMTimeoutError,
)
from backend.mydisease import clear_mydisease_cache
from backend.pipeline import run_analysis
from backend.provider_repository import (
    ProviderRepositoryContext,
    execute_provider_with_repository,
)
from backend.provider_resilience import (
    ProviderRetryPolicy,
    ProviderTimeouts,
    call_provider_with_policy,
)
from backend.variant_interpretation import interpret_variant, interpret_variants
from test_pipeline import (
    FakeLLMAdapter,
    FakeMyDiseaseSession,
    FakeResponse,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _manual_rows,
    _successful_phen2gene_session,
    _variant_interpretation_response,
)


pytestmark = [
    pytest.mark.regression,
    pytest.mark.stage59_testing_v3,
]


def _fake_pipeline_collaborators(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transform: Callable[[dict[str, object]], None] | None = None,
) -> None:
    def fake_annotate(
        variants: object,
        **_: object,
    ) -> list[dict[str, object]]:
        annotations: list[dict[str, object]] = []
        for variant in variants:  # type: ignore[union-attr]
            candidate = EvidenceFactory._pipeline_candidate()
            candidate["variant"] = dict(variant)
            if transform is not None:
                transform(candidate)
            annotations.append(candidate)
        return annotations

    monkeypatch.setattr("backend.pipeline.annotate_variants", fake_annotate)

    def fake_match(
        annotations: object,
        _: object,
        **__: object,
    ) -> list[dict[str, object]]:
        return [
            dict(annotation)
            for annotation in annotations  # type: ignore[union-attr]
        ]

    monkeypatch.setattr("backend.pipeline.match_phenotypes", fake_match)


def _run_traced_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transform: Callable[[dict[str, object]], None] | None = None,
) -> tuple[dict[str, object], AnalysisExecutionTrace]:
    _fake_pipeline_collaborators(monkeypatch, transform=transform)
    clear_mydisease_cache()
    monkeypatch.setattr("config.settings.MYDISEASE_MAX_RETRIES", 0)
    trace = AnalysisExecutionTrace(max_events=300)
    result = run_analysis(
        vcf_path=None,
        manual_variants=_manual_rows("2:166848215:C:T"),
        phenotypes=["HP:0001250", "HP:0001263"],
        annotation_max_retries=0,
        phen2gene_max_retries=0,
        phen2gene_session=_successful_phen2gene_session(),  # type: ignore[arg-type]
        phen2gene_use_cache=False,
        mydisease_session=FakeMyDiseaseSession(  # type: ignore[arg-type]
            [
                FakeResponse(200, {"build_version": "trace-test"}),
                FakeResponse(200, {"total": 0, "hits": []}),
            ]
        ),
        llm_client=LLMClient(
            FakeLLMAdapter(_variant_interpretation_response())
        ),
        report_dir=tmp_path / "reports",
        persist_analysis=False,
        execution_trace=trace,
    )
    return result, trace


def test_successful_analysis_trace_covers_lifecycle_and_variant_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, recorder = _run_traced_analysis(tmp_path, monkeypatch)
    snapshot = recorder.snapshot()
    event_types = [event["event_type"] for event in snapshot["events"]]

    assert "execution_trace" not in result
    assert snapshot["status"] == "completed"
    assert snapshot["events_dropped"] == 0
    assert event_types.index("analysis_started") < event_types.index(
        "input_validated"
    )
    assert event_types.index("input_validated") < event_types.index(
        "evidence_collection_started"
    )
    assert event_types.index("evidence_collection_completed") < (
        event_types.index("interpretation_started")
    )
    assert event_types.index("interpretation_completed") < event_types.index(
        "report_generated"
    )
    assert event_types[-1] == "analysis_completed"
    assert {
        "variant_processing_started",
        "variant_processing_completed",
        "annotation_completed",
        "evidence_created",
        "variant_interpretation_started",
        "variant_interpretation_completed",
    }.issubset(event_types)
    completed = snapshot["events"][-1]
    assert completed["variant_count"] == 1
    assert completed["succeeded_count"] == 1
    assert completed["failed_count"] == 0


def test_provider_fallback_route_preserves_primary_failure_and_final_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def use_fallback(candidate: dict[str, object]) -> None:
        sources = candidate["sources"]
        assert isinstance(sources, dict)
        sources["vep"] = {
            "status": "partial",
            "provider": "VariantValidator",
            "provider_role": "fallback",
            "fallback_used": True,
            "fallback_for": "ensembl_vep",
            "primary_failure": "timeout",
            "source_mode": "live_provider",
        }

    _, recorder = _run_traced_analysis(
        tmp_path,
        monkeypatch,
        transform=use_fallback,
    )
    fallback_events = [
        event
        for event in recorder.snapshot()["events"]
        if event["event_type"] == "fallback_activated"
    ]

    assert len(fallback_events) == 1
    fallback = fallback_events[0]
    assert fallback["scope"] == "provider"
    assert fallback["variant_index"] == 0
    assert fallback["provider"] == "VariantValidator"
    assert fallback["capability"] == "vep"
    assert fallback["status"] == "partial"
    assert fallback["outcome_category"] == "success"
    assert fallback["source_mode"] == "live_provider"
    assert fallback["provider_role"] == "fallback"
    assert fallback["fallback_for"] == "ensembl_vep"
    assert fallback["reason_category"] == "timeout"


def test_repository_cache_hit_is_distinct_from_live_provider(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    trace = AnalysisExecutionTrace(max_events=50)
    token = bind_execution_trace(trace, f"run-{'1' * 32}")
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")
    context = ProviderRepositoryContext(
        canonical_variant={"chrom": "2", "pos": 100, "ref": "C", "alt": "T"},
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="test_provider",
        provider_role="primary",
        source="test_provider",
        query_identity={"variant": "2-100-C-T"},
    )

    def validate(value: object) -> dict[str, object]:
        assert isinstance(value, Mapping)
        return dict(value)

    try:
        live = execute_provider_with_repository(
            repository=repository,
            context=context,
            live_call=lambda: {
                "status": "success",
                "retrieved_at": "2026-08-24T12:00:00Z",
                "frequency": 0.001,
            },
            validate_result=validate,
            clock=lambda: now,
        )
        cached = execute_provider_with_repository(
            repository=repository,
            context=context,
            live_call=lambda: pytest.fail("cache hit called the provider"),
            validate_result=validate,
            clock=lambda: now,
        )
    finally:
        reset_execution_trace(token)

    assert live["source_mode"] == "live_provider"
    assert cached["source_mode"] == "repository_cache"
    source_modes = [
        event["source_mode"]
        for event in trace.snapshot()["events"]
        if event["event_type"] == "provider_source_selected"
    ]
    assert source_modes == ["live_provider", "repository_cache"]
    assert any(
        event["event_type"] == "repository_lookup_hit"
        for event in trace.snapshot()["events"]
    )


def test_provider_attempts_capture_retry_outcomes_without_error_text() -> None:
    trace = AnalysisExecutionTrace(max_events=20)
    token = bind_execution_trace(trace, f"run-{'5' * 32}")

    def operation(_: tuple[float, float], attempt: int) -> dict[str, bool]:
        if attempt == 1:
            raise requests.Timeout("private provider timeout detail")
        return {"available": True}

    try:
        result = call_provider_with_policy(
            provider="trace_provider",
            operation_name="lookup_variant",
            operation=operation,
            timeouts=ProviderTimeouts(connect=1, read=1),
            retry_policy=ProviderRetryPolicy(
                max_attempts=2,
                backoff_base_seconds=0,
                backoff_max_seconds=0,
            ),
            sleep=lambda _: None,
        )
    finally:
        reset_execution_trace(token)

    attempts = [
        event
        for event in trace.snapshot()["events"]
        if event["event_type"] == "provider_attempt_completed"
    ]
    assert result.status == "success"
    assert [event["attempt"] for event in attempts] == [1, 2]
    assert [event["status"] for event in attempts] == ["timeout", "success"]
    assert attempts[0]["retry_scheduled"] is True
    assert attempts[0]["reason_category"] == "timeout"
    assert attempts[1]["source_mode"] == "live_provider"
    assert "private provider timeout detail" not in json.dumps(
        trace.snapshot()
    )


def test_llm_retry_and_failure_are_bounded_and_payload_free() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    trace = AnalysisExecutionTrace(max_events=100)
    token = bind_execution_trace(trace, f"run-{'2' * 32}")
    try:
        recovered = interpret_variant(
            evidence,
            client=LLMClient(
                SequenceLLMAdapter(
                    [
                        LLMRequestError("transient provider failure"),
                        _variant_interpretation_response(),
                    ]
                )
            ),
            max_retries=1,
            timestamp="2026-08-24T00:00:00Z",
        )
        failed = interpret_variants(
            [evidence],
            client=LLMClient(
                FakeLLMAdapter(LLMTimeoutError("secret timeout detail"))
            ),
            max_retries=0,
            timestamp="2026-08-24T00:00:00Z",
        )[0]
    finally:
        reset_execution_trace(token)

    snapshot = trace.snapshot()
    event_types = [event["event_type"] for event in snapshot["events"]]
    serialized = json.dumps(snapshot).casefold()

    assert recovered["status"] == "success"
    assert failed["status"] == "failed"
    assert "llm_retry_scheduled" in event_types
    assert "llm_output_classification_generated" in event_types
    assert "llm_output_interpretation_generated" in event_types
    assert any(
        event["event_type"] == "variant_interpretation_failed"
        and event["reason_category"] == "request_timeout"
        for event in snapshot["events"]
    )
    assert "transient provider failure" not in serialized
    assert "secret timeout detail" not in serialized
    assert "begin_validated_evidence_object" not in serialized
    assert "prompt" not in serialized
    assert "api_key" not in serialized


def test_trace_event_limit_is_deterministic() -> None:
    trace = AnalysisExecutionTrace(max_events=2)
    token = bind_execution_trace(trace, f"run-{'3' * 32}")
    try:
        for _ in range(4):
            trace.record(
                "provider_attempt_completed",
                scope="provider",
                provider="bounded_provider",
                capability="lookup",
                status="success",
            )
        trace.record(
            "analysis_completed",
            scope="analysis",
            status="success",
        )
    finally:
        reset_execution_trace(token)

    snapshot = trace.snapshot()
    assert snapshot["event_count"] == 2
    assert snapshot["events_dropped"] == 3
    assert snapshot["status"] == "completed"
    assert [event["sequence"] for event in snapshot["events"]] == [1, 2]
