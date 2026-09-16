"""Offline unit tests for backend.llm_preflight.

Covers:
- Successful model check
- Authentication failure (401 / 403)
- Quota / rate-limit failure
- Timeout
- Generic request failure
- Result dataclass contract
- Invalid model argument guard
"""

from __future__ import annotations

import json
from time import perf_counter

import pytest
import requests

from backend.llm import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
)
from backend.llm_preflight import (
    LLMPreflightResult,
    check_llm_connectivity,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers / fakes
# ──────────────────────────────────────────────────────────────────────────────


def _ok_response_body() -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "test-model-1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"status": "ready"}),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 5,
            "total_tokens": 10,
        },
    }


class _FakeHTTPResponse:
    """Minimal fake for requests.Response used by OpenAICompatibleAdapter."""

    def __init__(
        self,
        status_code: int,
        body: object = None,
    ) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body

    def raise_for_status(self) -> None:
        pass


class _FakeHTTPSession:
    """Fake HTTP session injected via OpenAICompatibleAdapter's session arg."""

    def __init__(self, response_or_exc: object) -> None:
        self._response_or_exc = response_or_exc
        self.post_calls: list[tuple] = []

    def post(self, url: str, **kwargs: object) -> _FakeHTTPResponse:
        self.post_calls.append((url, kwargs))
        if isinstance(self._response_or_exc, BaseException):
            raise self._response_or_exc
        assert isinstance(self._response_or_exc, _FakeHTTPResponse)
        return self._response_or_exc

    def get(self, url: str, **kwargs: object) -> _FakeHTTPResponse:
        # Not needed for preflight (no models endpoint call).
        raise AssertionError("Unexpected GET during preflight test")


# ──────────────────────────────────────────────────────────────────────────────
# Monkey-patch helper
# ──────────────────────────────────────────────────────────────────────────────


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, fake_session: _FakeHTTPSession) -> None:
    """Replace OpenAICompatibleAdapter.__init__ to inject our fake session."""

    import backend.llm_preflight as preflight_module
    from backend.llm import OpenAICompatibleAdapter

    original_init = OpenAICompatibleAdapter.__init__

    def patched_init(self, *, base_url, api_key, model, timeout, session=None):  # noqa: ANN001
        original_init(
            self,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            session=fake_session,
        )

    monkeypatch.setattr(OpenAICompatibleAdapter, "__init__", patched_init)


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_preflight_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid 200 response produces ok=True with a non-negative response_ms."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(200, _ok_response_body())
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o-mini")

    assert isinstance(result, LLMPreflightResult)
    assert result.ok is True
    assert result.model == "gpt-4o-mini"
    assert result.failure_category is None
    assert result.response_ms is not None
    assert result.response_ms >= 0
    assert result.checked_at  # non-empty ISO string


def test_preflight_auth_failure_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 response maps to failure_category='Invalid API key'."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(401, {"error": {"code": "invalid_api_key", "message": "x"}})
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o-mini")

    assert result.ok is False
    assert result.failure_category == "Invalid API key"


def test_preflight_auth_failure_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 response maps to failure_category='Invalid API key'."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(403, {"error": {"code": "permission_denied", "message": "x"}})
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o")

    assert result.ok is False
    assert result.failure_category == "Invalid API key"


def test_preflight_quota_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quota error response maps to failure_category='Quota exhausted'."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(
            402,
            {"error": {"code": "insufficient_quota", "message": "x"}},
        )
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o")

    assert result.ok is False
    assert result.failure_category == "Quota exhausted"


def test_preflight_provider_specific_quota_403_is_not_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(
            403,
            {
                "error": {
                    "code": "insufficient_user_quota",
                    "type": "gap_api_error",
                    "message": "private account balance",
                }
            },
        )
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-5.4-mini")

    assert result.ok is False
    assert result.failure_category == "Quota exhausted"


def test_preflight_rate_limit_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 rate-limit maps to failure_category='Quota exhausted'."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(429, {"error": {"message": "rate limit"}})
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o-mini")

    assert result.ok is False
    assert result.failure_category == "Quota exhausted"


def test_preflight_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A requests.Timeout maps to failure_category='Timeout'."""

    fake_session = _FakeHTTPSession(requests.Timeout("timed out"))
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o")

    assert result.ok is False
    assert result.failure_category == "Timeout"


def test_preflight_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection error maps to failure_category='Request failed'."""

    fake_session = _FakeHTTPSession(
        requests.ConnectionError("connection refused")
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o-mini")

    assert result.ok is False
    assert result.failure_category == "Request failed"


def test_preflight_server_error_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 server error maps to failure_category='Request failed'."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(500, {"error": {"message": "internal server error"}})
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gpt-4o-mini")

    assert result.ok is False
    assert result.failure_category == "Request failed"


def test_preflight_empty_model_guard() -> None:
    """An empty model string returns a safe failed result without network call."""

    result = check_llm_connectivity("")

    assert result.ok is False
    assert result.failure_category == "Model unavailable"
    assert result.response_ms is None


def test_preflight_whitespace_model_guard() -> None:
    """A whitespace-only model string returns a safe failed result."""

    result = check_llm_connectivity("   ")

    assert result.ok is False
    assert result.failure_category == "Model unavailable"


def test_preflight_result_is_frozen() -> None:
    """LLMPreflightResult is an immutable frozen dataclass."""

    result = LLMPreflightResult(
        ok=True,
        model="test",
        failure_category=None,
        response_ms=50,
        checked_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises((AttributeError, TypeError)):
        result.ok = False  # type: ignore[misc]


def test_preflight_checked_at_is_iso_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """checked_at is a non-empty ISO-8601 UTC string on both success and failure."""

    # Success case
    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(200, _ok_response_body())
    )
    _patch_adapter(monkeypatch, fake_session)
    result = check_llm_connectivity("gpt-4o-mini")

    assert result.checked_at.endswith("Z") or "+" in result.checked_at
    assert len(result.checked_at) >= 20


def test_preflight_no_clinical_data_in_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preflight request body must not contain patient or clinical data."""

    captured: list[dict] = []

    class _CapturingSession:
        def post(self, url: str, **kwargs: object) -> _FakeHTTPResponse:
            captured.append(dict(kwargs))
            return _FakeHTTPResponse(200, _ok_response_body())

        def get(self, url: str, **kwargs: object) -> _FakeHTTPResponse:
            raise AssertionError("Unexpected GET")

    from backend.llm import OpenAICompatibleAdapter
    original_init = OpenAICompatibleAdapter.__init__

    def patched_init(self, *, base_url, api_key, model, timeout, session=None):  # noqa: ANN001
        original_init(
            self,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            session=_CapturingSession(),
        )

    monkeypatch.setattr(OpenAICompatibleAdapter, "__init__", patched_init)

    check_llm_connectivity("gpt-4o-mini")

    assert len(captured) == 1
    payload = captured[0].get("json", {})
    messages = payload.get("messages", [])
    all_content = " ".join(m.get("content", "") for m in messages).lower()

    # Must not contain patient-identifiable clinical vocabulary
    clinical_terms = [
        "patient", "variant", "gene", "acmg", "pathogenic", "phenotype",
        "diagnosis", "clinical", "hpo", "clinvar", "gnomad",
    ]
    for term in clinical_terms:
        assert term not in all_content, (
            f"Preflight request unexpectedly contained clinical term: {term!r}"
        )

    # Token budget must be minimal (bounded check)
    assert payload.get("max_tokens", 9999) <= 64
    assert payload.get("response_format") == {"type": "json_object"}


def test_preflight_structured_output_rejected_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 rejection on response_format maps to 'Structured output unsupported'."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(
            400,
            {"error": {"message": "response_format json_object is not supported by model"}},
        )
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("gemini-3.1-flash-lite")

    assert result.ok is False
    assert result.failure_category == "Structured output unsupported"


def test_preflight_structured_output_rejected_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 422 rejection on response_format maps to 'Structured output unsupported'."""

    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(
            422,
            {"error": {"message": "Unprocessable response_format"}},
        )
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("some-legacy-model")

    assert result.ok is False
    assert result.failure_category == "Structured output unsupported"


def test_preflight_structured_output_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response with non-JSON content maps to 'Structured output unsupported'."""

    non_json_body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "I am ready!"},
                "finish_reason": "stop",
            }
        ],
    }
    fake_session = _FakeHTTPSession(_FakeHTTPResponse(200, non_json_body))
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("test-model")

    assert result.ok is False
    assert result.failure_category == "Structured output unsupported"


def test_preflight_structured_output_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response with JSON array rather than object maps to 'Structured output unsupported'."""

    array_json_body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '["ready"]'},
                "finish_reason": "stop",
            }
        ],
    }
    fake_session = _FakeHTTPSession(_FakeHTTPResponse(200, array_json_body))
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("test-model")

    assert result.ok is False
    assert result.failure_category == "Structured output unsupported"


def test_preflight_generic_400_maps_to_bad_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic 400 error maps to 'Bad request (HTTP 400)' and not 'Structured output unsupported'."""
    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(
            400,
            {"error": {"message": "Invalid request body syntax"}},
        )
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("test-model")

    assert result.ok is False
    assert result.failure_category == "Bad request (HTTP 400)"


def test_preflight_unsupported_param_maps_to_param_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported parameter 400 error maps to 'Model parameter unsupported'."""
    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(
            400,
            {
                "error": {
                    "message": "Unsupported parameter: 'temperature' is not supported.",
                    "param": "temperature",
                }
            },
        )
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("test-model")

    assert result.ok is False
    assert result.failure_category == "Model parameter unsupported"


def test_preflight_context_length_exceeded_maps_to_context_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context length exceeded error maps to 'Context length exceeded'."""
    fake_session = _FakeHTTPSession(
        _FakeHTTPResponse(
            400,
            {
                "error": {
                    "message": "This model's maximum context length is 8192 tokens.",
                    "code": "context_length_exceeded",
                }
            },
        )
    )
    _patch_adapter(monkeypatch, fake_session)

    result = check_llm_connectivity("test-model")

    assert result.ok is False
    assert result.failure_category == "Context length exceeded"

