"""Bounded, payload-free runtime tracing for one analysis journey."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Callable, Literal, TypedDict, cast


EXECUTION_TRACE_SCHEMA_VERSION = "1.0"
DEFAULT_MAX_EXECUTION_TRACE_EVENTS = 500
MAX_EXECUTION_TRACE_EVENTS = 5_000
RUN_ID_PATTERN = re.compile(r"run-[0-9a-f]{32}")
TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,79}")

TraceScope = Literal[
    "analysis",
    "stage",
    "variant",
    "provider",
    "repository",
    "llm",
    "report",
]
TraceSourceMode = Literal["live_provider", "repository_cache"]
TraceProviderRole = Literal["primary", "fallback"]
TraceStatus = Literal["active", "completed", "aborted"]


class ExecutionTraceEvent(TypedDict):
    """One strictly bounded event without arbitrary payload fields."""

    sequence: int
    occurred_at: str
    event_type: str
    scope: TraceScope
    stage: str | None
    variant_index: int | None
    provider: str | None
    capability: str | None
    attempt: int | None
    status: str | None
    outcome_category: str | None
    duration_ms: int | None
    source_mode: TraceSourceMode | None
    provider_role: TraceProviderRole | None
    fallback_for: str | None
    reason_category: str | None
    model_identifier: str | None
    retry_scheduled: bool | None
    circuit_state: str | None
    variant_count: int | None
    succeeded_count: int | None
    failed_count: int | None


class ExecutionTraceSnapshot(TypedDict):
    """Immutable caller-facing snapshot of one runtime-only trace."""

    schema_version: str
    run_id: str | None
    status: TraceStatus
    max_events: int
    event_count: int
    events_dropped: int
    events: list[ExecutionTraceEvent]


TraceClock = Callable[[], datetime]
_ACTIVE_EXECUTION_TRACE: ContextVar[AnalysisExecutionTrace | None] = ContextVar(
    "clinical_variant_analysis_execution_trace",
    default=None,
)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Execution trace clock must be timezone-aware.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _token(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Execution trace {field} must be text.")
    normalized = value.strip().casefold()
    if TOKEN_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"Execution trace {field} is invalid.")
    return normalized


def _label(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Execution trace {field} must be text.")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > 120
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"Execution trace {field} is invalid.")
    return normalized


def _bounded_integer(
    value: object,
    field: str,
    *,
    maximum: int = 1_000_000,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Execution trace {field} must be an integer.")
    if value < 0 or value > maximum:
        raise ValueError(f"Execution trace {field} is out of range.")
    return value


class AnalysisExecutionTrace:
    """Thread-safe caller-owned trace with deterministic event truncation."""

    def __init__(
        self,
        *,
        max_events: int = DEFAULT_MAX_EXECUTION_TRACE_EVENTS,
        clock: TraceClock = lambda: datetime.now(UTC),
    ) -> None:
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= MAX_EXECUTION_TRACE_EVENTS
        ):
            raise ValueError("Execution trace max_events is invalid.")
        if not callable(clock):
            raise ValueError("Execution trace clock must be callable.")
        self._max_events = max_events
        self._clock = clock
        self._lock = Lock()
        self._run_id: str | None = None
        self._status: TraceStatus = "active"
        self._events: list[ExecutionTraceEvent] = []
        self._events_dropped = 0

    def bind_run_id(self, run_id: str) -> None:
        """Bind exactly one application-generated run ID."""

        if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ValueError("Execution trace run ID is invalid.")
        with self._lock:
            if self._run_id not in {None, run_id}:
                raise ValueError("Execution trace is already bound to another run.")
            self._run_id = run_id

    def record(
        self,
        event_type: str,
        *,
        scope: TraceScope,
        stage: str | None = None,
        variant_index: int | None = None,
        provider: str | None = None,
        capability: str | None = None,
        attempt: int | None = None,
        status: str | None = None,
        outcome_category: str | None = None,
        duration_ms: int | None = None,
        source_mode: TraceSourceMode | None = None,
        provider_role: TraceProviderRole | None = None,
        fallback_for: str | None = None,
        reason_category: str | None = None,
        model_identifier: str | None = None,
        retry_scheduled: bool | None = None,
        circuit_state: str | None = None,
        variant_count: int | None = None,
        succeeded_count: int | None = None,
        failed_count: int | None = None,
    ) -> bool:
        """Append one validated event or count it as deterministically dropped."""

        if scope not in cast(tuple[str, ...], TraceScope.__args__):
            raise ValueError("Execution trace scope is invalid.")
        if source_mode not in {None, "live_provider", "repository_cache"}:
            raise ValueError("Execution trace source_mode is invalid.")
        if provider_role not in {None, "primary", "fallback"}:
            raise ValueError("Execution trace provider_role is invalid.")
        if retry_scheduled is not None and not isinstance(retry_scheduled, bool):
            raise ValueError("Execution trace retry_scheduled must be boolean.")
        if circuit_state not in {None, "OPEN", "CLOSED"}:
            raise ValueError("Execution trace circuit_state is invalid.")
        occurred_at = _timestamp(self._clock())
        normalized_event_type = cast(
            str,
            _token(event_type, "event_type"),
        )
        with self._lock:
            if self._run_id is None:
                raise ValueError("Execution trace must be bound before recording.")
            if normalized_event_type == "analysis_completed":
                self._status = "completed"
            elif normalized_event_type == "analysis_aborted":
                self._status = "aborted"
            if len(self._events) >= self._max_events:
                self._events_dropped += 1
                return False
            event: ExecutionTraceEvent = {
                "sequence": len(self._events) + 1,
                "occurred_at": occurred_at,
                "event_type": normalized_event_type,
                "scope": scope,
                "stage": _token(stage, "stage"),
                "variant_index": _bounded_integer(
                    variant_index,
                    "variant_index",
                ),
                "provider": _label(provider, "provider"),
                "capability": _token(capability, "capability"),
                "attempt": _bounded_integer(attempt, "attempt", maximum=100),
                "status": _token(status, "status"),
                "outcome_category": _token(
                    outcome_category,
                    "outcome_category",
                ),
                "duration_ms": _bounded_integer(
                    duration_ms,
                    "duration_ms",
                    maximum=86_400_000,
                ),
                "source_mode": source_mode,
                "provider_role": provider_role,
                "fallback_for": _token(fallback_for, "fallback_for"),
                "reason_category": _token(
                    reason_category,
                    "reason_category",
                ),
                "model_identifier": _label(
                    model_identifier,
                    "model_identifier",
                ),
                "retry_scheduled": retry_scheduled,
                "circuit_state": circuit_state,
                "variant_count": _bounded_integer(
                    variant_count,
                    "variant_count",
                ),
                "succeeded_count": _bounded_integer(
                    succeeded_count,
                    "succeeded_count",
                ),
                "failed_count": _bounded_integer(
                    failed_count,
                    "failed_count",
                ),
            }
            self._events.append(event)
            return True

    def snapshot(self) -> ExecutionTraceSnapshot:
        """Return a detached runtime-only snapshot."""

        with self._lock:
            return {
                "schema_version": EXECUTION_TRACE_SCHEMA_VERSION,
                "run_id": self._run_id,
                "status": self._status,
                "max_events": self._max_events,
                "event_count": len(self._events),
                "events_dropped": self._events_dropped,
                "events": deepcopy(self._events),
            }


def bind_execution_trace(
    trace: AnalysisExecutionTrace,
    run_id: str,
) -> Token[AnalysisExecutionTrace | None]:
    """Bind a caller-owned trace to the current analysis context."""

    if not isinstance(trace, AnalysisExecutionTrace):
        raise ValueError("execution_trace must be an AnalysisExecutionTrace.")
    trace.bind_run_id(run_id)
    return _ACTIVE_EXECUTION_TRACE.set(trace)


def reset_execution_trace(
    token: Token[AnalysisExecutionTrace | None],
) -> None:
    """Restore the prior trace context."""

    _ACTIVE_EXECUTION_TRACE.reset(token)


def record_execution_event(
    event_type: str,
    *,
    scope: TraceScope,
    **fields: object,
) -> bool:
    """Record through the active trace, or no-op when tracing is disabled."""

    trace = _ACTIVE_EXECUTION_TRACE.get()
    if trace is None:
        return False
    try:
        return trace.record(
            event_type,
            scope=scope,
            **fields,  # type: ignore[arg-type]
        )
    except (TypeError, ValueError):
        return False


__all__ = [
    "AnalysisExecutionTrace",
    "DEFAULT_MAX_EXECUTION_TRACE_EVENTS",
    "EXECUTION_TRACE_SCHEMA_VERSION",
    "ExecutionTraceEvent",
    "ExecutionTraceSnapshot",
    "MAX_EXECUTION_TRACE_EVENTS",
    "bind_execution_trace",
    "record_execution_event",
    "reset_execution_trace",
]
