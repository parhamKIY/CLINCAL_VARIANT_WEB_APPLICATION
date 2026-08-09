"""Stage 63 provider resilience contract regression tests."""

from __future__ import annotations

import io
import logging

import pytest
import requests

from backend.provider_resilience import (
    PROVIDER_STATUSES,
    ProviderCircuitState,
    ProviderInvalidResponseError,
    ProviderRetryPolicy,
    ProviderTimeouts,
    ProviderContractError,
    build_provider_provenance,
    call_provider_with_policy,
    classify_http_status,
    classify_request_exception,
    is_operational_failure,
    is_retryable_failure,
    should_trigger_fallback,
    validate_provider_provenance,
    validate_provider_status,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}


@pytest.mark.parametrize(
    "status",
    sorted(PROVIDER_STATUSES),
)
def test_all_declared_provider_statuses_validate(status: str) -> None:
    assert validate_provider_status(status) == status


@pytest.mark.parametrize("status", ["error", "not_found", "", None])
def test_legacy_or_ambiguous_statuses_do_not_enter_operational_contract(
    status: object,
) -> None:
    with pytest.raises(ProviderContractError):
        validate_provider_status(status)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, "success"),
        (204, "success"),
        (401, "configuration_error"),
        (403, "forbidden"),
        (408, "timeout"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
        (404, "unavailable"),
    ],
)
def test_http_status_taxonomy(status: int, expected: str) -> None:
    assert classify_http_status(status) == expected


def test_request_exceptions_are_classified_without_message_copying() -> None:
    assert classify_request_exception(
        requests.Timeout("sensitive timeout detail")
    ) == "timeout"
    assert classify_request_exception(
        requests.ConnectionError("sensitive DNS detail")
    ) == "unavailable"


@pytest.mark.parametrize(
    "status",
    [
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
    ],
)
def test_operational_failures_can_trigger_fallback(status: str) -> None:
    assert is_operational_failure(status) is True
    assert should_trigger_fallback(status) is True


@pytest.mark.parametrize(
    "status",
    ["success", "no_match", "configuration_error"],
)
def test_non_operational_statuses_do_not_trigger_fallback(status: str) -> None:
    assert is_operational_failure(status) is False
    assert should_trigger_fallback(status) is False


def test_no_match_is_neither_retryable_nor_a_fallback_trigger() -> None:
    assert is_retryable_failure("no_match") is False
    assert should_trigger_fallback("no_match") is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("timeout", True),
        ("rate_limited", True),
        ("server_error", True),
        ("invalid_response", True),
        ("unavailable", True),
        ("forbidden", False),
        ("configuration_error", False),
        ("success", False),
    ],
)
def test_retry_policy_is_centralized(status: str, expected: bool) -> None:
    assert is_retryable_failure(status) is expected


def test_other_client_errors_are_not_retried_blindly() -> None:
    assert is_retryable_failure("unavailable", http_status=404) is False
    assert is_retryable_failure("timeout", http_status=408) is True
    assert is_retryable_failure("rate_limited", http_status=429) is True


def test_primary_provenance_has_no_fallback_claim() -> None:
    provenance = build_provider_provenance(
        capability="population_frequency",
        provider="gnomad",
    )
    assert provenance == {
        "capability": "population_frequency",
        "provider": "gnomad",
        "provider_role": "primary",
        "fallback_used": False,
        "primary_provider": "gnomad",
        "primary_failure": None,
    }
    assert validate_provider_provenance(provenance) == provenance


def test_fallback_provenance_preserves_primary_failure() -> None:
    provenance = build_provider_provenance(
        capability="population_frequency",
        provider="ensembl_variation",
        provider_role="fallback",
        primary_provider="gnomad",
        primary_failure="forbidden",
    )
    assert provenance == {
        "capability": "population_frequency",
        "provider": "ensembl_variation",
        "provider_role": "fallback",
        "fallback_used": True,
        "primary_provider": "gnomad",
        "primary_failure": "forbidden",
    }
    assert validate_provider_provenance(provenance) == provenance


@pytest.mark.parametrize("primary_failure", [None, "success", "no_match"])
def test_fallback_rejects_non_failure_primary_status(
    primary_failure: str | None,
) -> None:
    with pytest.raises(ProviderContractError):
        build_provider_provenance(
            capability="population_frequency",
            provider="ensembl_variation",
            provider_role="fallback",
            primary_provider="gnomad",
            primary_failure=primary_failure,
        )


def test_provenance_rejects_inconsistent_fallback_flag() -> None:
    provenance = build_provider_provenance(
        capability="population_frequency",
        provider="gnomad",
    )
    provenance["fallback_used"] = True
    with pytest.raises(ProviderContractError):
        validate_provider_provenance(provenance)


def test_provenance_rejects_unknown_fields() -> None:
    provenance = dict(
        build_provider_provenance(
            capability="population_frequency",
            provider="gnomad",
        )
    )
    provenance["unexpected"] = "value"
    with pytest.raises(ProviderContractError):
        validate_provider_provenance(provenance)


@pytest.mark.parametrize("invalid_role", ["secondary", None, []])
def test_provenance_rejects_invalid_roles(invalid_role: object) -> None:
    provenance = dict(
        build_provider_provenance(
            capability="population_frequency",
            provider="gnomad",
        )
    )
    provenance["provider_role"] = invalid_role
    with pytest.raises(ProviderContractError):
        validate_provider_provenance(provenance)


def test_shared_policy_passes_strict_connect_read_timeouts() -> None:
    calls: list[tuple[tuple[float, float], int]] = []

    def operation(
        timeout: tuple[float, float],
        attempt: int,
    ) -> FakeResponse:
        calls.append((timeout, attempt))
        return FakeResponse(200)

    result = call_provider_with_policy(
        provider="gnomad",
        operation_name="lookup_variant",
        operation=operation,
        timeouts=ProviderTimeouts(connect=2, read=7),
    )
    assert result.status == "success"
    assert result.attempts == 1
    assert result.value is not None
    assert calls == [((2.0, 7.0), 1)]


def test_timeout_retries_once_then_opens_analysis_circuit() -> None:
    calls = 0
    sleeps: list[float] = []
    circuits = ProviderCircuitState()

    def operation(
        _timeout: tuple[float, float],
        _attempt: int,
    ) -> FakeResponse:
        nonlocal calls
        calls += 1
        raise requests.Timeout("private timeout detail")

    result = call_provider_with_policy(
        provider="gnomad",
        operation_name="lookup_variant",
        operation=operation,
        timeouts=ProviderTimeouts(connect=1, read=2),
        circuit_state=circuits,
        sleep=sleeps.append,
    )
    assert result.status == "timeout"
    assert result.attempts == 2
    assert result.circuit_open is True
    assert calls == 2
    assert sleeps == [1.0]
    assert circuits.get("gnomad") is not None

    skipped = call_provider_with_policy(
        provider="gnomad",
        operation_name="lookup_variant",
        operation=operation,
        timeouts=ProviderTimeouts(connect=1, read=2),
        circuit_state=circuits,
    )
    assert skipped.status == "timeout"
    assert skipped.attempts == 0
    assert skipped.circuit_open is True
    assert calls == 2


def test_forbidden_opens_circuit_without_retry() -> None:
    sleeps: list[float] = []
    circuits = ProviderCircuitState()
    result = call_provider_with_policy(
        provider="gnomad",
        operation_name="lookup_variant",
        operation=lambda _timeout, _attempt: FakeResponse(403),
        timeouts=ProviderTimeouts(connect=1, read=2),
        circuit_state=circuits,
        sleep=sleeps.append,
    )
    assert result.status == "forbidden"
    assert result.attempts == 1
    assert result.circuit_open is True
    assert sleeps == []


def test_small_retry_after_is_honored() -> None:
    responses = iter(
        [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(200),
        ]
    )
    sleeps: list[float] = []
    result = call_provider_with_policy(
        provider="pubmed",
        operation_name="search_literature",
        operation=lambda _timeout, _attempt: next(responses),
        timeouts=ProviderTimeouts(connect=1, read=2),
        sleep=sleeps.append,
    )
    assert result.status == "success"
    assert result.attempts == 2
    assert sleeps == [2.0]


def test_impractical_retry_after_fails_without_waiting() -> None:
    sleeps: list[float] = []
    result = call_provider_with_policy(
        provider="pubmed",
        operation_name="search_literature",
        operation=lambda _timeout, _attempt: FakeResponse(
            429,
            headers={"Retry-After": "60"},
        ),
        timeouts=ProviderTimeouts(connect=1, read=2),
        sleep=sleeps.append,
    )
    assert result.status == "rate_limited"
    assert result.attempts == 1
    assert sleeps == []


def test_server_error_retries_then_succeeds() -> None:
    responses = iter([FakeResponse(503), FakeResponse(200)])
    sleeps: list[float] = []
    result = call_provider_with_policy(
        provider="ensembl_vep",
        operation_name="annotate_batch",
        operation=lambda _timeout, _attempt: next(responses),
        timeouts=ProviderTimeouts(connect=1, read=2),
        sleep=sleeps.append,
    )
    assert result.status == "success"
    assert result.attempts == 2
    assert sleeps == [1.0]


def test_capability_specific_404_remains_terminal_no_match() -> None:
    circuits = ProviderCircuitState()
    result = call_provider_with_policy(
        provider="myvariant",
        operation_name="lookup_variant",
        operation=lambda _timeout, _attempt: FakeResponse(404),
        timeouts=ProviderTimeouts(connect=1, read=2),
        circuit_state=circuits,
        no_match_http_statuses=frozenset({404}),
    )
    assert result.status == "no_match"
    assert result.attempts == 1
    assert result.circuit_open is False
    assert circuits.snapshot() == {}


def test_invalid_response_can_be_retried_by_shared_policy() -> None:
    attempts = 0

    def operation(
        _timeout: tuple[float, float],
        _attempt: int,
    ) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderInvalidResponseError("malformed body")
        return {"usable": True}

    result = call_provider_with_policy(
        provider="clingen_cspec",
        operation_name="lookup_gene",
        operation=operation,
        timeouts=ProviderTimeouts(connect=1, read=2),
        sleep=lambda _delay: None,
    )
    assert result.status == "success"
    assert result.value == {"usable": True}
    assert result.attempts == 2


def test_circuits_are_scoped_to_their_analysis_instance() -> None:
    first = ProviderCircuitState()
    second = ProviderCircuitState()
    first.open("gnomad", "forbidden", http_status=403)
    assert first.get("gnomad") is not None
    assert second.get("gnomad") is None


@pytest.mark.parametrize(
    "timeouts",
    [
        (0, 1),
        (-1, 1),
        (1, 0),
        (1, 301),
        (True, 1),
    ],
)
def test_timeout_contract_rejects_unbounded_values(
    timeouts: tuple[object, object],
) -> None:
    with pytest.raises(ProviderContractError):
        ProviderTimeouts(connect=timeouts[0], read=timeouts[1])


def test_retry_policy_is_bounded_to_three_attempts() -> None:
    with pytest.raises(ProviderContractError):
        ProviderRetryPolicy(max_attempts=4)


def test_policy_logging_is_bounded_and_provenance_aware() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("clinical_variant_app.provider_resilience")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        call_provider_with_policy(
            provider="gnomad",
            operation_name="lookup_variant",
            operation=lambda _timeout, _attempt: FakeResponse(403),
            timeouts=ProviderTimeouts(connect=1, read=2),
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    contents = stream.getvalue()
    assert "provider=gnomad" in contents
    assert "attempt=1" in contents
    assert "status=forbidden" in contents
    assert "circuit_state=CLOSED" in contents
    assert "fallback_transition=eligible" in contents
