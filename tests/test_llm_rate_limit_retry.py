"""Bounded provider-directed retry behavior for the LLM boundary."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from backend.llm import LLMClient, OpenAICompatibleAdapter, call_llm


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

    def post(self, _url: str, **_kwargs: object) -> _Response:
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
