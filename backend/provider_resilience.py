"""Central provider status, retry, fallback, and provenance contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, TypedDict, cast

import requests


ProviderStatus = Literal[
    "success",
    "no_match",
    "unavailable",
    "timeout",
    "forbidden",
    "rate_limited",
    "server_error",
    "invalid_response",
    "configuration_error",
]
ProviderRole = Literal["primary", "fallback"]

PROVIDER_STATUSES = frozenset(
    {
        "success",
        "no_match",
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
        "configuration_error",
    }
)
PROVIDER_ROLES = frozenset({"primary", "fallback"})
OPERATIONAL_FAILURE_STATUSES = frozenset(
    {
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
    }
)
RETRYABLE_FAILURE_STATUSES = frozenset(
    {
        "unavailable",
        "timeout",
        "rate_limited",
        "server_error",
        "invalid_response",
    }
)
PROVIDER_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
PROVIDER_PROVENANCE_FIELDS = frozenset(
    {
        "capability",
        "provider",
        "provider_role",
        "fallback_used",
        "primary_provider",
        "primary_failure",
    }
)


class ProviderContractError(ValueError):
    """Raised when provider status or provenance violates the contract."""


class ProviderProvenance(TypedDict):
    """Operational provenance retained beside provider-specific evidence."""

    capability: str
    provider: str
    provider_role: ProviderRole
    fallback_used: bool
    primary_provider: str
    primary_failure: ProviderStatus | None


def validate_provider_status(value: object) -> ProviderStatus:
    """Return one exact normalized provider operational status."""

    if not isinstance(value, str) or value not in PROVIDER_STATUSES:
        raise ProviderContractError("Unsupported provider operational status.")
    return cast(ProviderStatus, value)


def is_operational_failure(status: object) -> bool:
    """Return whether a status represents an operational provider failure."""

    return validate_provider_status(status) in OPERATIONAL_FAILURE_STATUSES


def should_trigger_fallback(status: object) -> bool:
    """Allow fallback only for operational failure, never valid no-match."""

    return is_operational_failure(status)


def is_retryable_failure(
    status: object,
    *,
    http_status: int | None = None,
) -> bool:
    """Apply the shared retry taxonomy without starting a retry loop."""

    normalized = validate_provider_status(status)
    if http_status is not None:
        _validate_http_status(http_status)
        if 400 <= http_status < 500 and http_status not in {408, 429}:
            return False
    return normalized in RETRYABLE_FAILURE_STATUSES


def classify_http_status(status_code: int) -> ProviderStatus:
    """Classify HTTP outcome; response content decides valid no-match."""

    _validate_http_status(status_code)
    if 200 <= status_code < 300:
        return "success"
    if status_code == 401:
        return "configuration_error"
    if status_code == 403:
        return "forbidden"
    if status_code == 408:
        return "timeout"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "unavailable"


def classify_request_exception(
    exception: requests.RequestException,
) -> ProviderStatus:
    """Classify bounded request failures without retaining exception text."""

    if not isinstance(exception, requests.RequestException):
        raise TypeError("exception must be a requests.RequestException.")
    if isinstance(exception, requests.Timeout):
        return "timeout"
    response = getattr(exception, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return classify_http_status(status_code)
    return "unavailable"


def build_provider_provenance(
    *,
    capability: str,
    provider: str,
    provider_role: ProviderRole = "primary",
    primary_provider: str | None = None,
    primary_failure: ProviderStatus | None = None,
) -> ProviderProvenance:
    """Build strict primary/fallback provenance without evidence relabeling."""

    _validate_identifier(capability, "capability")
    _validate_identifier(provider, "provider")
    if (
        not isinstance(provider_role, str)
        or provider_role not in PROVIDER_ROLES
    ):
        raise ProviderContractError("Unsupported provider role.")

    if provider_role == "primary":
        if primary_provider is not None and primary_provider != provider:
            raise ProviderContractError(
                "Primary provenance cannot name a different primary provider."
            )
        if primary_failure is not None:
            raise ProviderContractError(
                "Primary provenance cannot record a primary failure."
            )
        return {
            "capability": capability,
            "provider": provider,
            "provider_role": "primary",
            "fallback_used": False,
            "primary_provider": provider,
            "primary_failure": None,
        }

    if primary_provider is None:
        raise ProviderContractError(
            "Fallback provenance requires the primary provider."
        )
    _validate_identifier(primary_provider, "primary_provider")
    if primary_provider == provider:
        raise ProviderContractError(
            "Fallback provider must differ from the primary provider."
        )
    if primary_failure is None or not should_trigger_fallback(primary_failure):
        raise ProviderContractError(
            "Fallback provenance requires an operational primary failure."
        )
    return {
        "capability": capability,
        "provider": provider,
        "provider_role": "fallback",
        "fallback_used": True,
        "primary_provider": primary_provider,
        "primary_failure": primary_failure,
    }


def validate_provider_provenance(value: object) -> ProviderProvenance:
    """Validate persisted provider provenance and return a safe copy."""

    if not isinstance(value, Mapping):
        raise ProviderContractError("Provider provenance must be a mapping.")
    if set(value) != PROVIDER_PROVENANCE_FIELDS:
        raise ProviderContractError(
            "Provider provenance fields do not match the contract."
        )
    fallback_used = value["fallback_used"]
    if not isinstance(fallback_used, bool):
        raise ProviderContractError("fallback_used must be a boolean.")
    role = value["provider_role"]
    if not isinstance(role, str) or role not in PROVIDER_ROLES:
        raise ProviderContractError("Unsupported provider role.")
    expected = build_provider_provenance(
        capability=value["capability"],
        provider=value["provider"],
        provider_role=cast(ProviderRole, role),
        primary_provider=value["primary_provider"],
        primary_failure=value["primary_failure"],
    )
    if fallback_used != expected["fallback_used"]:
        raise ProviderContractError(
            "fallback_used is inconsistent with provider_role."
        )
    return expected


def _validate_identifier(value: object, field: str) -> None:
    if (
        not isinstance(value, str)
        or PROVIDER_IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ProviderContractError(
            f"{field} must be a lowercase provider identifier."
        )


def _validate_http_status(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 100 <= value <= 599
    ):
        raise ProviderContractError(
            "HTTP status must be an integer from 100 to 599."
        )
