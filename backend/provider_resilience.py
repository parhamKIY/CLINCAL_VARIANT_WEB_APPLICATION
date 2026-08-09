"""Central provider status, retry, fallback, and provenance contract."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Generic, Literal, TypeVar, TypedDict, cast

import requests

from backend.logging_config import get_logger


LOGGER = get_logger("provider_resilience")
ResponseT = TypeVar("ResponseT")

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


class ProviderInvalidResponseError(ValueError):
    """Signal a retryable malformed or unusable provider response."""

    def __init__(
        self,
        message: str = "Provider returned an invalid response.",
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        if http_status is not None:
            _validate_http_status(http_status)
        self.http_status = http_status


@dataclass(frozen=True)
class ProviderTimeouts:
    """Strict positive connect/read deadlines for one provider attempt."""

    connect: float
    read: float

    def __post_init__(self) -> None:
        for field, value in (("connect", self.connect), ("read", self.read)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < float(value) <= 300
            ):
                raise ProviderContractError(
                    f"Provider {field} timeout must be greater than 0 "
                    "and at most 300 seconds."
                )

    def as_requests_timeout(self) -> tuple[float, float]:
        """Return the connect/read tuple accepted by requests."""

        return float(self.connect), float(self.read)


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Bounded retry settings shared by network provider clients."""

    max_attempts: int = 2
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 5.0
    max_retry_after_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 3
        ):
            raise ProviderContractError(
                "Provider max_attempts must be an integer from 1 to 3."
            )
        for field, value in (
            ("backoff_base_seconds", self.backoff_base_seconds),
            ("backoff_max_seconds", self.backoff_max_seconds),
            ("max_retry_after_seconds", self.max_retry_after_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= float(value) <= 30
            ):
                raise ProviderContractError(
                    f"{field} must be from 0 to 30 seconds."
                )
        if self.backoff_base_seconds > self.backoff_max_seconds:
            raise ProviderContractError(
                "Provider backoff base cannot exceed its maximum."
            )


@dataclass(frozen=True)
class ProviderCircuitRecord:
    """One analysis-scoped open-circuit reason."""

    status: ProviderStatus
    http_status: int | None


class ProviderCircuitState:
    """Thread-safe provider circuits owned by one analysis execution."""

    def __init__(self) -> None:
        self._records: dict[str, ProviderCircuitRecord] = {}
        self._lock = Lock()

    def get(self, provider: str) -> ProviderCircuitRecord | None:
        _validate_identifier(provider, "provider")
        with self._lock:
            return self._records.get(provider)

    def open(
        self,
        provider: str,
        status: ProviderStatus,
        *,
        http_status: int | None = None,
    ) -> None:
        _validate_identifier(provider, "provider")
        normalized = validate_provider_status(status)
        if normalized in {"success", "no_match"}:
            raise ProviderContractError(
                "A provider circuit requires a failure status."
            )
        if http_status is not None:
            _validate_http_status(http_status)
        with self._lock:
            self._records.setdefault(
                provider,
                ProviderCircuitRecord(normalized, http_status),
            )

    def snapshot(self) -> dict[str, ProviderCircuitRecord]:
        """Return a detached, non-sensitive circuit snapshot."""

        with self._lock:
            return dict(self._records)


@dataclass(frozen=True)
class ProviderCallResult(Generic[ResponseT]):
    """Bounded call outcome consumed by provider-specific parsing."""

    status: ProviderStatus
    value: ResponseT | None
    attempts: int
    http_status: int | None
    circuit_open: bool


ProviderOperation = Callable[[tuple[float, float], int], ResponseT]


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


def call_provider_with_policy(
    *,
    provider: str,
    operation_name: str,
    operation: ProviderOperation[ResponseT],
    timeouts: ProviderTimeouts,
    retry_policy: ProviderRetryPolicy | None = None,
    circuit_state: ProviderCircuitState | None = None,
    no_match_http_statuses: frozenset[int] = frozenset(),
    sleep: Callable[[float], None] | None = None,
) -> ProviderCallResult[ResponseT]:
    """Execute one provider operation under bounded shared network policy."""

    _validate_identifier(provider, "provider")
    _validate_identifier(operation_name, "operation_name")
    if not callable(operation):
        raise ProviderContractError("operation must be callable.")
    if not isinstance(timeouts, ProviderTimeouts):
        raise ProviderContractError("timeouts must be ProviderTimeouts.")
    policy = retry_policy or ProviderRetryPolicy()
    if not isinstance(policy, ProviderRetryPolicy):
        raise ProviderContractError(
            "retry_policy must be ProviderRetryPolicy."
        )
    if circuit_state is not None and not isinstance(
        circuit_state,
        ProviderCircuitState,
    ):
        raise ProviderContractError(
            "circuit_state must be ProviderCircuitState."
        )
    validated_no_match_statuses = _validate_no_match_http_statuses(
        no_match_http_statuses
    )
    sleep_function = time.sleep if sleep is None else sleep
    if not callable(sleep_function):
        raise ProviderContractError("sleep must be callable.")

    open_record = (
        circuit_state.get(provider)
        if circuit_state is not None
        else None
    )
    if open_record is not None:
        _log_policy_event(
            provider=provider,
            operation_name=operation_name,
            attempt=0,
            status=open_record.status,
            http_status=open_record.http_status,
            duration_ms=0,
            circuit_state="OPEN",
            event="provider_call_skipped",
        )
        return ProviderCallResult(
            status=open_record.status,
            value=None,
            attempts=0,
            http_status=open_record.http_status,
            circuit_open=True,
        )

    for attempt in range(1, policy.max_attempts + 1):
        started_at = time.perf_counter()
        value: ResponseT | None = None
        http_status: int | None = None
        try:
            value = operation(timeouts.as_requests_timeout(), attempt)
            if isinstance(value, requests.Response) or hasattr(
                value,
                "status_code",
            ):
                candidate_status = getattr(value, "status_code", None)
                if (
                    isinstance(candidate_status, bool)
                    or not isinstance(candidate_status, int)
                ):
                    raise ProviderInvalidResponseError(
                        "Provider response has no valid HTTP status."
                    )
                http_status = candidate_status
                status = (
                    "no_match"
                    if http_status in validated_no_match_statuses
                    else classify_http_status(http_status)
                )
            else:
                status = "success"
        except ProviderInvalidResponseError as exc:
            status = "invalid_response"
            http_status = exc.http_status
            value = None
        except requests.RequestException as exc:
            status = classify_request_exception(exc)
            response = getattr(exc, "response", None)
            candidate_status = getattr(response, "status_code", None)
            http_status = (
                candidate_status
                if isinstance(candidate_status, int)
                and not isinstance(candidate_status, bool)
                else None
            )
            value = None

        duration_ms = max(
            0,
            round((time.perf_counter() - started_at) * 1000),
        )
        if status in {"success", "no_match"}:
            _log_policy_event(
                provider=provider,
                operation_name=operation_name,
                attempt=attempt,
                status=status,
                http_status=http_status,
                duration_ms=duration_ms,
                circuit_state="CLOSED",
            )
            return ProviderCallResult(
                status=status,
                value=value,
                attempts=attempt,
                http_status=http_status,
                circuit_open=False,
            )

        retry_delay = _retry_delay_for_policy(
            status=status,
            http_status=http_status,
            response=value,
            attempt=attempt,
            policy=policy,
        )
        will_retry = (
            attempt < policy.max_attempts
            and retry_delay is not None
        )
        _log_policy_event(
            provider=provider,
            operation_name=operation_name,
            attempt=attempt,
            status=status,
            http_status=http_status,
            duration_ms=duration_ms,
            circuit_state="CLOSED",
            retry_scheduled=will_retry,
        )
        if will_retry:
            sleep_function(retry_delay)
            continue

        if circuit_state is not None:
            circuit_state.open(
                provider,
                status,
                http_status=http_status,
            )
            _log_policy_event(
                provider=provider,
                operation_name=operation_name,
                attempt=attempt,
                status=status,
                http_status=http_status,
                duration_ms=duration_ms,
                circuit_state="OPEN",
                event="provider_circuit_opened",
            )
        return ProviderCallResult(
            status=status,
            value=None,
            attempts=attempt,
            http_status=http_status,
            circuit_open=circuit_state is not None,
        )

    raise RuntimeError("Provider policy loop ended unexpectedly.")


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


def _retry_delay_for_policy(
    *,
    status: ProviderStatus,
    http_status: int | None,
    response: object,
    attempt: int,
    policy: ProviderRetryPolicy,
) -> float | None:
    if not is_retryable_failure(status, http_status=http_status):
        return None
    if status == "rate_limited" and response is not None:
        headers = getattr(response, "headers", {})
        if isinstance(headers, Mapping):
            raw_retry_after = headers.get(
                "Retry-After",
                headers.get("retry-after"),
            )
            if raw_retry_after is not None:
                try:
                    retry_after = float(raw_retry_after)
                except (TypeError, ValueError):
                    retry_after = None
                if retry_after is not None:
                    if (
                        retry_after < 0
                        or retry_after > policy.max_retry_after_seconds
                    ):
                        return None
                    return retry_after
    return min(
        policy.backoff_base_seconds * (2 ** (attempt - 1)),
        policy.backoff_max_seconds,
    )


def _validate_no_match_http_statuses(value: object) -> frozenset[int]:
    if not isinstance(value, frozenset):
        raise ProviderContractError(
            "no_match_http_statuses must be a frozenset."
        )
    for status in value:
        _validate_http_status(status)
        if not 400 <= status < 500:
            raise ProviderContractError(
                "No-match HTTP statuses must be client-error statuses."
            )
    return value


def _log_policy_event(
    *,
    provider: str,
    operation_name: str,
    attempt: int,
    status: ProviderStatus,
    http_status: int | None,
    duration_ms: int,
    circuit_state: Literal["CLOSED", "OPEN"],
    event: str = "provider_call",
    retry_scheduled: bool = False,
) -> None:
    fallback_transition = (
        "eligible" if should_trigger_fallback(status) else "none"
    )
    log_method = (
        LOGGER.info
        if status in {"success", "no_match"}
        else LOGGER.warning
    )
    log_method(
        "event=%s provider=%s operation=%s attempt=%d status=%s "
        "http_status=%s duration_ms=%d circuit_state=%s "
        "retry_scheduled=%s fallback_transition=%s",
        event,
        provider,
        operation_name,
        attempt,
        status,
        http_status,
        duration_ms,
        circuit_state,
        str(retry_scheduled).casefold(),
        fallback_transition,
    )


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
