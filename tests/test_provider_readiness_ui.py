"""Offline tests for pre-upload provider-readiness behavior."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
import requests

import frontend.provider_readiness as provider_readiness_ui
from backend.provider_readiness import (
    ProviderReadinessResult,
    ProviderReadinessTarget,
    probe_provider_readiness,
)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: object) -> None:
        self.response = response
        self.head_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[tuple[str, dict[str, object]]] = []

    def head(self, url: str, **kwargs: object) -> FakeResponse:
        self.head_calls.append((url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        assert isinstance(self.response, FakeResponse)
        return self.response

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        assert isinstance(self.response, FakeResponse)
        return self.response


def _resolved(*_args: object, **_kwargs: object) -> object:
    return [(None, None, None, None, ("127.0.0.1", 443))]


def _clock(values: list[float]):
    iterator = iter(values)
    return lambda: next(iterator)


def _result(
    provider: str,
    *,
    state: str = "reachable",
    probe_kind: str = "transport",
    failure_category: str = "none",
) -> ProviderReadinessResult:
    return ProviderReadinessResult(
        provider=provider,
        state=state,  # type: ignore[arg-type]
        dns_status="resolved",
        http_status=200 if state == "reachable" else None,
        latency_ms=12.5,
        failure_category=failure_category,
        probe_kind=probe_kind,  # type: ignore[arg-type]
    )


def test_transport_probe_accepts_head_not_supported_as_reachable() -> None:
    response = FakeResponse(405)
    target = ProviderReadinessTarget(
        "example",
        "Example",
        "https://example.org/api",
        ("example_capability",),
    )

    result = probe_provider_readiness(
        target,
        session=FakeSession(response),
        resolver=_resolved,
        clock=_clock([1.0, 1.025]),
    )

    assert result.state == "reachable"
    assert result.failure_category == "none"
    assert response.closed is True


def test_authenticated_probe_uses_configured_key_without_retaining_it() -> None:
    session = FakeSession(FakeResponse(200))
    target = ProviderReadinessTarget(
        "llm",
        "LLM",
        "https://provider.example/v1",
        ("variant_interpretation",),
        credential_mode="required",
        probe_kind="authenticated",
    )

    result = probe_provider_readiness(
        target,
        session=session,
        resolver=_resolved,
        clock=_clock([1.0, 1.025]),
    )

    assert result.state == "reachable"
    assert session.get_calls[0][0] == "https://provider.example/v1/models"
    assert "Authorization" in session.get_calls[0][1]["headers"]  # type: ignore[index]
    assert "Authorization" not in repr(result)


def test_dns_failure_does_not_issue_an_http_request() -> None:
    session = FakeSession(FakeResponse(200))
    target = ProviderReadinessTarget(
        "example",
        "Example",
        "https://example.org/api",
        ("example_capability",),
    )

    def failed_resolver(*_args: object, **_kwargs: object) -> object:
        raise OSError("unresolvable")

    result = probe_provider_readiness(
        target,
        session=session,
        resolver=failed_resolver,
    )

    assert result.state == "unreachable"
    assert result.failure_category == "dns_failure"
    assert session.head_calls == []


def test_rows_expose_safe_status_latency_and_fallback_role() -> None:
    targets = (
        ProviderReadinessTarget(
            "vep",
            "Ensembl VEP",
            "https://example.org/vep",
            ("variant_annotation",),
        ),
        ProviderReadinessTarget(
            "variantvalidator",
            "VariantValidator",
            "https://example.org/validator",
            ("variant_annotation",),
            ("variant_annotation",),
        ),
    )

    rows = provider_readiness_ui.build_provider_readiness_rows(
        (_result("vep"), _result("variantvalidator", state="unreachable", failure_category="timeout")),
        targets,
    )

    assert rows == [
        {
            "Provider": "Ensembl VEP",
            "Use": "Variant annotation",
            "Fallback role": "Primary or independent source",
            "Status": "Reachable",
            "Check": "Endpoint",
            "Latency": "12 ms",
            "Details": "Endpoint responded.",
        },
        {
            "Provider": "VariantValidator",
            "Use": "Variant annotation",
            "Fallback role": "Fallback for Variant annotation",
            "Status": "Unreachable",
            "Check": "Endpoint",
            "Latency": "12 ms",
            "Details": "The provider did not respond before the deadline.",
        },
    ]


def test_recommendation_rows_keep_quality_and_latency_reasoning_visible() -> None:
    rows = provider_readiness_ui.build_provider_recommendation_rows(
        (),
        recommendations=(
            provider_readiness_ui.ProviderReadinessRecommendation(
                capability="variant_annotation",
                provider="variantvalidator",
                state="preferred",
                reason="Higher-quality source is unreachable; selected the lowest-latency reachable alternative.",
                latency_ms=12.5,
            ),
        ),
    )

    assert rows == [
        {
            "Use": "Variant annotation",
            "Recommended source": "VariantValidator",
            "Status": "Preferred",
            "Latency": "12 ms",
            "Basis": "Higher-quality source is unreachable; selected the lowest-latency reachable alternative.",
        }
    ]


def test_population_frequency_labels_distinguish_normal_and_verification() -> None:
    rows = provider_readiness_ui.build_provider_readiness_rows(
        (),
        (
            ProviderReadinessTarget(
                "myvariant",
                "MyVariant.info",
                "https://example.org/myvariant",
                ("normal_population_frequency",),
            ),
            ProviderReadinessTarget(
                "gnomad",
                "gnomAD",
                "https://example.org/gnomad",
                ("population_frequency",),
            ),
        ),
    )

    assert [row["Use"] for row in rows] == [
        "Normal population frequency",
        "Population-frequency verification",
    ]


def test_renderer_runs_checks_only_after_explicit_user_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    state: dict[str, object] = {}

    @contextmanager
    def fake_container(**_kwargs: object):
        yield

    @contextmanager
    def fake_spinner(*_args: object, **_kwargs: object):
        yield

    def fake_dataframe(data: object, **kwargs: object) -> None:
        observed["dataframe"] = (data, kwargs)

    monkeypatch.setattr(provider_readiness_ui.st, "session_state", state)
    monkeypatch.setattr(provider_readiness_ui.st, "container", fake_container)
    monkeypatch.setattr(provider_readiness_ui.st, "spinner", fake_spinner)
    monkeypatch.setattr(provider_readiness_ui.st, "subheader", lambda *_args: None)
    monkeypatch.setattr(provider_readiness_ui.st, "caption", lambda *_args: None)
    monkeypatch.setattr(provider_readiness_ui.st, "info", lambda *_args: None)
    monkeypatch.setattr(provider_readiness_ui.st, "dataframe", fake_dataframe)
    monkeypatch.setattr(provider_readiness_ui.st, "button", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        provider_readiness_ui.st,
        "selectbox",
        lambda *_args, **_kwargs: "variantvalidator",
    )

    provider_readiness_ui.initialize_provider_readiness_state()
    provider_readiness_ui.render_provider_readiness(
        job_active=False,
        checker=lambda: (_result("vep"),),
        single_checker=lambda target: _result(target.provider),
    )

    assert state[provider_readiness_ui.PROVIDER_READINESS_RESULTS_KEY] == (
        _result("vep"),
        _result("variantvalidator"),
    )
    assert state[provider_readiness_ui.PROVIDER_READINESS_CHECKED_AT_KEY]
    assert observed["dataframe"]
