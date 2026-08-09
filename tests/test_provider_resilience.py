"""Stage 63 provider resilience contract regression tests."""

from __future__ import annotations

import pytest
import requests

from backend.provider_resilience import (
    PROVIDER_STATUSES,
    ProviderContractError,
    build_provider_provenance,
    classify_http_status,
    classify_request_exception,
    is_operational_failure,
    is_retryable_failure,
    should_trigger_fallback,
    validate_provider_provenance,
    validate_provider_status,
)


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
