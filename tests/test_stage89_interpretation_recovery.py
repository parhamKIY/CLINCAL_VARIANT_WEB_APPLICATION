"""Stage 89 bounded interpretation recovery policy checks."""

import json
import logging

import pytest

from backend.llm import (
    LLMAuthenticationError,
    LLMClient,
    LLMRequestError,
    LLMResponse,
    LLMTimeoutError,
)
from backend.logging_config import APP_LOGGER_NAME
from backend.variant_interpretation import interpret_variant, interpret_variants
from config import settings
from test_pipeline import (
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    TestStage50SingleModelInterpretation as InterpretationFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage89_interpretation_recovery


def _invalid_schema_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps({"interpretation": "incomplete"}),
        model="primary-response-model",
        finish_reason="stop",
    )


def test_transient_primary_failure_retries_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.llm.sleep", lambda _: None)
    adapter = SequenceLLMAdapter(
        [
            LLMTimeoutError("temporary timeout"),
            _variant_interpretation_response(model="primary-response-model"),
        ]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        max_retries=1,
        timestamp="2026-08-11T12:00:00Z",
    )

    assert result["status"] == "success"
    assert result["configured_model"] == settings.VARIANT_INTERPRETATION_MODEL
    assert result["response_model"] == "primary-response-model"
    assert len(adapter.requests) == 2


def test_invalid_schema_gets_one_constrained_repair() -> None:
    adapter = SequenceLLMAdapter(
        [
            _invalid_schema_response(),
            _variant_interpretation_response(model="primary-response-model"),
        ]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-11T12:00:00Z",
    )

    assert result["status"] == "success"
    assert len(adapter.requests) == 2
    first_prompt = adapter.requests[0].messages[1].content
    repaired_prompt = adapter.requests[1].messages[1].content
    assert repaired_prompt.startswith(first_prompt)
    assert "RECOVERY INSTRUCTION" not in first_prompt
    assert "RECOVERY INSTRUCTION" in repaired_prompt
    assert "BEGIN_VALIDATED_EVIDENCE_OBJECT" in repaired_prompt


def test_operational_fallback_preserves_normalized_evidence(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("backend.llm.sleep", lambda _: None)
    primary = SequenceLLMAdapter(
        [
            LLMTimeoutError("first timeout"),
            LLMTimeoutError("second timeout"),
        ]
    )
    fallback = SequenceLLMAdapter(
        [_variant_interpretation_response(model="fallback-response-model")]
    )

    with caplog.at_level(logging.WARNING, logger=APP_LOGGER_NAME):
        result = interpret_variant(
            EvidenceFactory._complete_evidence_object(),
            client=LLMClient(primary),
            fallback_model="backup-interpretation-model",
            fallback_client=LLMClient(fallback),
            max_retries=1,
            timestamp="2026-08-11T12:00:00Z",
        )

    assert result["status"] == "success"
    assert result["configured_model"] == "backup-interpretation-model"
    assert result["response_model"] == "fallback-response-model"
    assert any("Operational recovery" in item for item in result["warnings"])
    assert len(primary.requests) == 2
    assert len(fallback.requests) == 1
    assert (
        primary.requests[0].messages[1].content
        == fallback.requests[0].messages[1].content
    )
    assert "action=fallback" in caplog.text
    assert "fallback_used=true" in caplog.text


def test_schema_repair_exhaustion_can_use_operational_fallback() -> None:
    primary = SequenceLLMAdapter(
        [_invalid_schema_response(), _invalid_schema_response()]
    )
    fallback = SequenceLLMAdapter(
        [_variant_interpretation_response(model="fallback-response-model")]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(primary),
        fallback_model="backup-interpretation-model",
        fallback_client=LLMClient(fallback),
        timestamp="2026-08-11T12:00:00Z",
    )

    assert result["status"] == "success"
    assert len(primary.requests) == 2
    assert len(fallback.requests) == 1
    assert (
        primary.requests[0].messages[1].content
        == fallback.requests[0].messages[1].content
    )


def test_authentication_failure_does_not_activate_model_fallback() -> None:
    primary = SequenceLLMAdapter(
        [LLMAuthenticationError("invalid credentials", http_status=401)]
    )
    fallback = SequenceLLMAdapter(
        [_variant_interpretation_response(model="must-not-be-used")]
    )

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(primary),
        fallback_model="backup-interpretation-model",
        fallback_client=LLMClient(fallback),
        timestamp="2026-08-11T12:00:00Z",
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "authentication_error"
    assert len(primary.requests) == 1
    assert fallback.requests == []


def test_nontransient_http_failure_is_not_retried_or_fallback_routed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.llm.sleep", lambda _: None)
    primary = SequenceLLMAdapter(
        [LLMRequestError("bad request", http_status=400)]
    )
    fallback = SequenceLLMAdapter(
        [_variant_interpretation_response(model="must-not-be-used")]
    )

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(primary),
        fallback_model="backup-interpretation-model",
        fallback_client=LLMClient(fallback),
        max_retries=1,
        timestamp="2026-08-11T12:00:00Z",
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "invalid_request"
    assert len(primary.requests) == 1
    assert fallback.requests == []


def test_failed_fallback_retains_diagnostic_recovery_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = SequenceLLMAdapter([LLMTimeoutError("primary timeout")])
    fallback = SequenceLLMAdapter([LLMTimeoutError("fallback timeout")])

    with caplog.at_level(logging.WARNING, logger=APP_LOGGER_NAME):
        result = interpret_variants(
            [EvidenceFactory._complete_evidence_object()],
            client=LLMClient(primary),
            fallback_model="backup-interpretation-model",
            fallback_client=LLMClient(fallback),
            max_retries=0,
            timestamp="2026-08-11T12:00:00Z",
        )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "request_timeout"
    assert "model=backup-interpretation-model" in caplog.text
    assert "fallback_used=True" in caplog.text


def test_conflict_never_selects_fallback_without_operational_failure() -> None:
    primary = SequenceLLMAdapter(
        [
            _variant_interpretation_response(model="primary-model"),
            _variant_interpretation_response(
                model="primary-model",
                conflict_assessment="The conflict remains unresolved.",
                preliminary_classification_status="ambiguous",
                preliminary_classification=None,
            ),
        ]
    )
    fallback = SequenceLLMAdapter(
        [_variant_interpretation_response(model="must-not-be-used")]
    )

    results = interpret_variants(
        [
            EvidenceFactory._complete_evidence_object(),
            InterpretationFactory._conflicting_evidence(),
        ],
        client=LLMClient(primary),
        fallback_model="backup-interpretation-model",
        fallback_client=LLMClient(fallback),
        timestamp="2026-08-11T12:00:00Z",
    )

    assert [item["status"] for item in results] == ["success", "success"]
    assert {item["configured_model"] for item in results} == {
        settings.VARIANT_INTERPRETATION_MODEL
    }
    assert fallback.requests == []


def test_configured_fallback_is_automatic_for_normal_application_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        settings,
        "VARIANT_INTERPRETATION_FALLBACK_MODEL",
        "configured-backup-model",
    )

    def fake_call_llm(*_: object, **kwargs: object) -> LLMResponse:
        observed.append(dict(kwargs))
        if len(observed) == 1:
            raise LLMTimeoutError("primary timeout", attempt=2)
        return _variant_interpretation_response(model="fallback-response-model")

    monkeypatch.setattr(
        "backend.variant_interpretation.call_llm",
        fake_call_llm,
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        model="user-selected-primary-model",
        timestamp="2026-08-11T12:00:00Z",
    )

    assert result["status"] == "success"
    assert [item["model"] for item in observed] == [
        "user-selected-primary-model",
        "configured-backup-model",
    ]
    assert all(item["max_retries"] == 1 for item in observed)
