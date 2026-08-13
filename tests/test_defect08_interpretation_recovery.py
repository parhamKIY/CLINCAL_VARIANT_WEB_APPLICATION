"""Defect 08 model-failure recovery and reviewer retry tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.llm import (
    LLMClient,
    LLMConfigurationError,
    LLMTimeoutError,
)
from backend.pipeline import PipelineError, retry_failed_variant_interpretation
from backend.variant_interpretation import (
    classify_interpretation_failure,
    interpret_variants,
)
from frontend.evidence_review import interpretation_failure_message
from tests.test_pipeline import (
    FakeLLMAdapter,
    TestStage40FrontendReviewWorkflow as _FrontendWorkflow,
    _variant_interpretation_response,
)


def _failed_draft_result() -> dict[str, object]:
    result = _FrontendWorkflow._draft_result()
    evidence = result["evidence_objects"][0]
    failed = interpret_variants(
        [evidence],
        client=LLMClient(FakeLLMAdapter(LLMTimeoutError("timeout"))),
        max_retries=0,
        timestamp="2026-08-13T09:00:00Z",
        readiness_audits=result["evidence_readiness"],
    )[0]
    result["variant_interpretation_results"] = [dict(failed)]
    from backend.variant_report import build_draft_variant_report

    result["draft_variant_reports"] = [
        dict(
            build_draft_variant_report(
                evidence,
                failed,
                variant_index=0,
            )
        )
    ]
    result["variant_report_records"] = []
    result["warnings"] = [
        "Interpretation completed with 1 explicit variant failure(s); "
        "collected evidence was preserved."
    ]
    result["status"] = "partial"
    return result


def test_configuration_failure_has_its_own_persisted_category() -> None:
    assert classify_interpretation_failure(
        LLMConfigurationError("missing configuration")
    ) == "configuration_error"


@pytest.mark.parametrize(
    ("failure_type", "expected"),
    [
        ("request_timeout", "temporarily unavailable"),
        ("output_schema_failure", "structured response"),
        ("configuration_error", "configuration requires attention"),
        ("safety_or_finish_failure", "stopped before"),
        ("internal_conversion_failure", "workflow requires attention"),
    ],
)
def test_failure_message_distinguishes_recovery_category(
    failure_type: str,
    expected: str,
) -> None:
    assert expected in interpretation_failure_message(failure_type)


def test_retry_reuses_persisted_evidence_and_rebuilds_only_failed_report() -> None:
    result = _failed_draft_result()
    evidence_before = deepcopy(result["evidence_objects"])
    annotations_before = deepcopy(result["annotations"])

    retried = retry_failed_variant_interpretation(
        result,
        variant_index=0,
        client=LLMClient(
            FakeLLMAdapter(
                _variant_interpretation_response(model="retry-response-model")
            )
        ),
        timestamp="2026-08-13T10:00:00Z",
    )

    assert retried["evidence_objects"] == evidence_before
    assert retried["annotations"] == annotations_before
    assert retried["variant_interpretation_results"][0]["status"] == "success"
    assert retried["variant_interpretation_results"][0]["response_model"] == (
        "retry-response-model"
    )
    assert retried["draft_variant_reports"][0]["reviewed_report"][
        "variant_interpretation"
    ]["status"] == "success"
    assert len(retried["variant_report_records"]) == 1
    assert retried["workflow_state"] == "awaiting_final_review"
    assert retried["status"] == "success"
    assert retried["warnings"] == []


def test_retry_failure_remains_explicit_without_losing_evidence() -> None:
    result = _failed_draft_result()
    evidence_before = deepcopy(result["evidence_objects"])

    retried = retry_failed_variant_interpretation(
        result,
        variant_index=0,
        client=LLMClient(FakeLLMAdapter(LLMTimeoutError("retry timeout"))),
        max_retries=0,
        timestamp="2026-08-13T10:00:00Z",
    )

    assert retried["evidence_objects"] == evidence_before
    assert retried["variant_interpretation_results"][0]["status"] == "failed"
    assert retried["variant_interpretation_results"][0]["error_type"] == (
        "request_timeout"
    )
    assert retried["status"] == "partial"


def test_later_success_clears_stale_retry_failure_warning() -> None:
    first_retry = retry_failed_variant_interpretation(
        _failed_draft_result(),
        variant_index=0,
        client=LLMClient(FakeLLMAdapter(LLMTimeoutError("retry timeout"))),
        max_retries=0,
        timestamp="2026-08-13T10:00:00Z",
    )

    recovered = retry_failed_variant_interpretation(
        first_retry,
        variant_index=0,
        client=LLMClient(
            FakeLLMAdapter(_variant_interpretation_response())
        ),
        timestamp="2026-08-13T10:05:00Z",
    )

    assert recovered["status"] == "success"
    assert recovered["warnings"] == []


def test_retry_refuses_to_overwrite_reviewer_edits() -> None:
    result = _failed_draft_result()
    from backend.variant_report import save_draft_variant_report

    report = result["draft_variant_reports"][0]
    content = report["reviewed_report"]
    result["draft_variant_reports"][0] = dict(
        save_draft_variant_report(
            report,
            reviewer_summary="Reviewed summary",
            interpretation_narrative=content["variant_interpretation"][
                "narrative"
            ],
            conflict_assessment=content["variant_interpretation"][
                "conflict_assessment"
            ],
            reviewer_notes=content["reviewer_notes"],
            timestamp="2026-08-13T09:30:00Z",
        )
    )

    with pytest.raises(PipelineError, match="reviewer decisions"):
        retry_failed_variant_interpretation(
            result,
            variant_index=0,
            client=LLMClient(
                FakeLLMAdapter(_variant_interpretation_response())
            ),
        )
