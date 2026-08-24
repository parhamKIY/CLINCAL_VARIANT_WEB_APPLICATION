"""Stage 11.2 analysis lifecycle stability acceptance tests."""

from __future__ import annotations

import json
import threading
from copy import deepcopy

import pytest

import backend.pipeline as pipeline_module
from backend.annotation import AnnotationError
from backend.llm import LLMClient, LLMRateLimitError, LLMResponse
from backend.pipeline import create_pipeline_result, run_analysis
from backend.variant_interpretation import interpret_variants
from frontend.execution import AnalysisJob
from test_pipeline import (
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _manual_rows,
)


pytestmark = pytest.mark.stage11_2_lifecycle


def _valid_interpretation_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Uncertain significance",
                "interpretation": (
                    "Available evidence supports a cautious draft assessment."
                ),
                "conflict_assessment": "No unresolved conflict was identified.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="stage11-2-test-model",
        finish_reason="stop",
    )


def test_analysis_job_deadline_forces_terminal_failure() -> None:
    release_worker = threading.Event()

    def blocked_runner(_progress: object) -> object:
        release_worker.wait(2)
        return create_pipeline_result()

    job = AnalysisJob(
        blocked_runner,  # type: ignore[arg-type]
        max_runtime_seconds=0.05,
    )
    job.start()
    try:
        assert threading.Event().wait(0.1) is False
        view = job.view()
        assert view.state == "error"
        assert view.error_message == (
            "The analysis exceeded its configured runtime limit. Review "
            "provider availability and try again."
        )
    finally:
        release_worker.set()
        job.join(2)

    assert job.view().state == "error"


def test_provider_timeout_path_returns_terminal_pipeline_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_annotation(*_args: object, **_kwargs: object) -> None:
        raise AnnotationError("bounded provider timeout")

    monkeypatch.setattr(
        pipeline_module,
        "_annotate_and_match",
        fail_annotation,
    )

    result = run_analysis(
        vcf_path=None,
        manual_variants=_manual_rows("1:100:A:G"),
        phenotypes=[],
        persist_analysis=False,
    )

    assert result["status"] == "partial"
    assert result["workflow_state"] == "failed"
    assert result["current_stage"] == "annotation"
    assert all(stage["status"] != "running" for stage in result["stages"])
    assert all(api["status"] != "running" for api in result["api_statuses"])


def test_llm_rate_limit_has_bounded_retry_and_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.llm.sleep", lambda _delay: None)
    adapter = SequenceLLMAdapter(
        [
            LLMRateLimitError("rate limited", http_status=429),
            LLMRateLimitError("rate limited", http_status=429),
        ]
    )

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        max_retries=1,
    )[0]

    assert len(adapter.requests) == 2
    assert result["status"] == "failed"
    assert result["error_type"] == "http_429"


def test_unexpected_variant_processing_exception_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_processing(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("unexpected processing defect")

    monkeypatch.setattr(
        pipeline_module,
        "_process_filtered_variants",
        fail_processing,
    )

    result = run_analysis(
        vcf_path=None,
        manual_variants=_manual_rows("1:100:A:G"),
        phenotypes=[],
        persist_analysis=False,
    )

    assert result["status"] == "error"
    assert result["workflow_state"] == "failed"
    assert result["errors"][0]["code"] == "unexpected_processing_error"
    assert all(stage["status"] != "running" for stage in result["stages"])


def test_one_llm_variant_failure_does_not_stop_sibling() -> None:
    first = EvidenceFactory._complete_evidence_object()
    second = deepcopy(first)
    second["variant"] = {"chrom": "1", "pos": 101, "ref": "C", "alt": "T"}
    adapter = SequenceLLMAdapter(
        [
            LLMRateLimitError("rate limited", http_status=429),
            _valid_interpretation_response(),
        ]
    )

    results = interpret_variants(
        [first, second],
        client=LLMClient(adapter),
        max_retries=0,
    )

    assert [result["status"] for result in results] == ["failed", "success"]
    assert results[1]["interpretation"] is not None


def test_complete_multi_variant_interpretation_run_stays_green() -> None:
    first = EvidenceFactory._complete_evidence_object()
    second = deepcopy(first)
    second["variant"] = {"chrom": "1", "pos": 101, "ref": "C", "alt": "T"}
    adapter = SequenceLLMAdapter(
        [_valid_interpretation_response(), _valid_interpretation_response()]
    )

    results = interpret_variants(
        [first, second],
        client=LLMClient(adapter),
        max_retries=0,
    )

    assert [result["status"] for result in results] == ["success", "success"]
    assert all(result["interpretation"] is not None for result in results)
