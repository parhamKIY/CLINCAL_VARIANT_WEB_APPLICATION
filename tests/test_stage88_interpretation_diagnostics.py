"""Stage 88 interpretation failure taxonomy and observability checks."""

import logging
from pathlib import Path

import pytest

from backend.llm import (
    LLMAuthenticationError,
    LLMClient,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
    LLMValidationError,
)
from backend.logging_config import APP_LOGGER_NAME
from backend.variant_interpretation import (
    VARIANT_INTERPRETATION_PROMPT_VERSION,
    VariantInterpretationError,
    build_interpretation_failure_diagnostic,
    classify_interpretation_failure,
    interpret_variants,
)
from test_pipeline import (
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.stage88_interpretation_diagnostics


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMTimeoutError("timeout"), "request_timeout"),
        (LLMRequestError("connection"), "connection_error"),
        (LLMRateLimitError("limited", http_status=429), "http_429"),
        (LLMRequestError("server", http_status=503), "http_5xx"),
        (
            LLMAuthenticationError("auth", http_status=401),
            "authentication_error",
        ),
        (LLMValidationError("invalid"), "invalid_request"),
        (LLMResponseError("empty"), "empty_response"),
        (
            VariantInterpretationError(
                "schema", failure_type="output_schema_failure"
            ),
            "output_schema_failure",
        ),
        (
            VariantInterpretationError(
                "parse", failure_type="output_parse_failure"
            ),
            "output_parse_failure",
        ),
        (
            VariantInterpretationError(
                "finish", failure_type="safety_or_finish_failure"
            ),
            "safety_or_finish_failure",
        ),
        (TypeError("conversion"), "internal_conversion_failure"),
        (RuntimeError("unknown"), "unknown_failure"),
    ],
)
def test_failure_taxonomy_is_complete(
    error: Exception,
    expected: str,
) -> None:
    assert classify_interpretation_failure(error) == expected


def test_failed_variant_emits_actionable_secret_free_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "stage88-secret-value"
    error = LLMRequestError(
        f"provider failed with api_key={secret}",
        http_status=503,
        attempt=3,
        schema_error="provider_unavailable",
    )
    evidence = EvidenceFactory._complete_evidence_object()
    diagnostic = build_interpretation_failure_diagnostic(
        evidence,
        model="variant-model",
        error=error,
    )

    assert diagnostic == {
        "variant_id": diagnostic["variant_id"],
        "model": "variant-model",
        "prompt_version": VARIANT_INTERPRETATION_PROMPT_VERSION,
        "attempt": 3,
        "failure_type": "http_5xx",
        "http_status": 503,
        "finish_reason": None,
        "schema_error": "provider_unavailable",
        "fallback_used": False,
    }
    assert diagnostic["variant_id"].startswith("allele-sha256:")

    adapter = SequenceLLMAdapter([error])
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER_NAME):
        result = interpret_variants(
            [evidence],
            client=LLMClient(adapter),
            timestamp="2026-08-11T10:00:00Z",
        )[0]

    assert result["error_type"] == "http_5xx"
    log_text = caplog.text
    for field in diagnostic:
        assert f"{field}=" in log_text
    assert secret not in log_text
    assert "api_key=" not in log_text


def test_parse_and_finish_failures_keep_safe_metadata() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    parse_error = VariantInterpretationError(
        "invalid provider output",
        failure_type="output_parse_failure",
        schema_error="invalid_json",
    )
    finish_error = VariantInterpretationError(
        "unsafe finish",
        failure_type="safety_or_finish_failure",
        finish_reason="content_filter",
    )

    parse_diagnostic = build_interpretation_failure_diagnostic(
        evidence,
        model="variant-model",
        error=parse_error,
    )
    finish_diagnostic = build_interpretation_failure_diagnostic(
        evidence,
        model="variant-model",
        error=finish_error,
    )

    assert parse_diagnostic["schema_error"] == "invalid_json"
    assert finish_diagnostic["finish_reason"] == "content_filter"

    unsafe_error = VariantInterpretationError(
        "unsafe metadata",
        failure_type="output_schema_failure",
        schema_error="secret-looking-provider-value",
        finish_reason="provider-specific-secret",
    )
    unsafe_diagnostic = build_interpretation_failure_diagnostic(
        evidence,
        model="variant-model",
        error=unsafe_error,
    )
    assert unsafe_diagnostic["schema_error"] == "unrecognized"
    assert unsafe_diagnostic["finish_reason"] == "unrecognized"


def test_user_message_does_not_expose_internal_failure_type() -> None:
    source = (PROJECT_ROOT / "frontend" / "evidence_review.py").read_text(
        encoding="utf-8"
    )
    assert "failed ({interpretation['failure_type']" not in source
    assert "Interpretation is unavailable" in source
