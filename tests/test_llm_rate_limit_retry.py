"""Bounded provider-directed retry behavior for the LLM boundary."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from backend.llm import (
    LLMClient,
    LLMMessage,
    LLMRequest,
    LLMRequestError,
    OpenAICompatibleAdapter,
    call_llm,
)


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = dict(headers or {})

    def json(self) -> object:
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.captured_posts: list[dict] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        self.captured_posts.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def get(self, _url: str, **_kwargs: object) -> _Response:
        raise AssertionError("Model discovery is not part of this test.")


def _success() -> _Response:
    return _Response(
        200,
        {
            "choices": [
                {
                    "message": {"content": "{}"},
                    "finish_reason": "stop",
                }
            ],
            "model": "test-model",
        },
    )


def _client(session: _Session) -> LLMClient:
    return LLMClient(
        OpenAICompatibleAdapter(
            base_url="https://llm.example/v1",
            api_key="test-key-not-a-secret",
            model="test-model",
            timeout=30,
            session=session,
        )
    )


def test_rate_limit_retry_honors_provider_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("backend.llm.sleep", delays.append)
    session = _Session(
        [
            _Response(429, {"error": {}}, headers={"Retry-After": "7"}),
            _success(),
        ]
    )

    response = call_llm(
        "system",
        "user",
        client=_client(session),
        max_retries=1,
    )

    assert response.model == "test-model"
    assert delays == [7.0]


def test_rate_limit_retry_after_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("backend.llm.sleep", delays.append)
    session = _Session(
        [
            _Response(429, {"error": {}}, headers={"Retry-After": "600"}),
            _success(),
        ]
    )

    call_llm(
        "system",
        "user",
        client=_client(session),
        max_retries=1,
    )

    assert delays == [30.0]


def test_invalid_retry_after_uses_existing_exponential_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("backend.llm.sleep", delays.append)
    session = _Session(
        [
            _Response(429, {"error": {}}, headers={"Retry-After": "later"}),
            _success(),
        ]
    )

    call_llm(
        "system",
        "user",
        client=_client(session),
        max_retries=1,
    )

    assert delays == [1.0]


def test_temperature_fallback_retries_with_temp_1_on_400() -> None:
    """When a model rejects temperature=0.0 with 400, adapter falls back to temperature=1.0."""
    session = _Session(
        [
            _Response(
                400,
                {
                    "error": {
                        "message": "Unsupported value: 'temperature' does not support 0.0 with this model. Only the default (1) value is supported.",
                        "type": "invalid_request",
                        "param": None,
                        "code": "invalid_request",
                    }
                },
            ),
            _success(),
        ]
    )

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.avalai.ir/v1",
        api_key="test-key",
        model="gpt-5.6-luna",
        timeout=10.0,
        session=session,
    )

    request = LLMRequest(
        messages=(LLMMessage("user", "ready"),),
        temperature=0.0,
        max_tokens=30,
    )

    response = adapter.generate(request)

    assert response.content == "{}"
    assert len(session.captured_posts) == 2
    assert session.captured_posts[0]["json"]["temperature"] == 0.0
    assert session.captured_posts[1]["json"]["temperature"] == 1.0


def test_temperature_fallback_omits_temp_when_param_unsupported() -> None:
    """When a model rejects temperature parameter completely, adapter omits it."""
    session = _Session(
        [
            _Response(
                400,
                {
                    "error": {
                        "message": "Unsupported value: 'temperature' does not support 0.0.",
                        "param": "temperature",
                    }
                },
            ),
            _Response(
                400,
                {
                    "error": {
                        "message": "Unsupported parameter: 'temperature' is not supported with this model.",
                        "param": "temperature",
                    }
                },
            ),
            _success(),
        ]
    )

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="o1-preview",
        timeout=10.0,
        session=session,
    )

    request = LLMRequest(
        messages=(LLMMessage("user", "ready"),),
        temperature=0.0,
        max_tokens=30,
    )

    response = adapter.generate(request)

    assert response.content == "{}"
    assert len(session.captured_posts) == 3
    assert session.captured_posts[0]["json"]["temperature"] == 0.0
    assert session.captured_posts[1]["json"]["temperature"] == 1.0
    assert "temperature" not in session.captured_posts[2]["json"]


def test_non_temperature_400_does_not_trigger_temperature_fallback() -> None:
    """Other 400 errors (e.g. invalid model) do not trigger temperature retry."""
    session = _Session(
        [
            _Response(
                400,
                {
                    "error": {
                        "message": "Model 'invalid-model' not found.",
                        "type": "invalid_request_error",
                    }
                },
            ),
        ]
    )

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.avalai.ir/v1",
        api_key="test-key",
        model="invalid-model",
        timeout=10.0,
        session=session,
    )

    request = LLMRequest(
        messages=(LLMMessage("user", "ready"),),
        temperature=0.0,
        max_tokens=30,
    )

    with pytest.raises(LLMRequestError) as exc_info:
        adapter.generate(request)

    assert exc_info.value.http_status == 400
    assert len(session.captured_posts) == 1

