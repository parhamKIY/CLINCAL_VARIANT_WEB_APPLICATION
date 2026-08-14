"""Bounded, checkpointed orchestration for live provider contract probes."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from threading import Thread
from time import monotonic
from typing import Any, Literal, TypedDict


LIVE_VALIDATION_SCHEMA_VERSION = "1.0"
DEFAULT_OVERALL_DEADLINE_SECONDS = 180.0
MAX_OVERALL_DEADLINE_SECONDS = 600.0
SAFE_CHECK_FIELDS = frozenset(
    {
        "provider",
        "status",
        "provider_version",
        "failure_reason",
        "http_status",
        "request_attempts",
        "configured_model",
        "response_model",
        "candidate_count",
        "prompt_mode",
        "error_type",
    }
)
ProviderRunStatus = Literal[
    "not_started",
    "running",
    "completed",
    "unavailable",
    "overall_deadline_exceeded",
]


class LiveValidationError(ValueError):
    """Raised when live-validation orchestration inputs are unsafe."""


class ProviderCheckpoint(TypedDict):
    provider: str
    status: ProviderRunStatus
    elapsed_seconds: float | None
    check_count: int


class LiveValidationSummary(TypedDict):
    schema_version: str
    generated_at: str
    overall_deadline_seconds: float
    status: str
    providers: list[ProviderCheckpoint]
    checks: list[dict[str, object]]
    elapsed_seconds: float


@dataclass(frozen=True)
class ProviderTask:
    """One sequential, independently checkpointed provider probe."""

    provider: str
    run: Callable[[], Sequence[Mapping[str, object]]]
    close: Callable[[], None] | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validate_task(task: ProviderTask) -> None:
    if not isinstance(task.provider, str) or not task.provider.strip():
        raise LiveValidationError("Provider task name must be non-empty text.")
    if not callable(task.run) or (task.close is not None and not callable(task.close)):
        raise LiveValidationError("Provider task callbacks are invalid.")


def _validate_deadline(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= MAX_OVERALL_DEADLINE_SECONDS
    ):
        raise LiveValidationError(
            "Overall validation deadline must be greater than 0 and at most 600 seconds."
        )
    return float(value)


def _atomic_write(path: Path, value: LiveValidationSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _safe_scalar(value: object) -> str | int | float | None:
    if value is None or isinstance(value, bool):
        return value if value is None else None
    if isinstance(value, str):
        normalized = value.strip()
        if len(normalized) > 200:
            return None
        return normalized or None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value == value and value not in {float("inf"), float("-inf")}:
        return round(value, 6)
    return None


def _normalize_checks(
    provider: str,
    value: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for candidate in value:
        if not isinstance(candidate, Mapping):
            raise LiveValidationError("Provider task returned a non-mapping check.")
        check: dict[str, object] = {"provider": provider}
        for field in SAFE_CHECK_FIELDS:
            normalized = _safe_scalar(candidate.get(field))
            if normalized is not None:
                check[field] = normalized
        if not isinstance(check.get("provider"), str):
            check["provider"] = provider
        checks.append(check)
    return checks


def _close(task: ProviderTask) -> None:
    if task.close is None:
        return
    try:
        task.close()
    except Exception:
        pass


def run_live_validation(
    tasks: Sequence[ProviderTask],
    *,
    output_path: Path,
    overall_deadline_seconds: float = DEFAULT_OVERALL_DEADLINE_SECONDS,
    emit: Callable[[str], None] = print,
) -> tuple[LiveValidationSummary, int]:
    """Run ordered probes with bounded wall time and atomic safe checkpoints."""

    deadline = _validate_deadline(overall_deadline_seconds)
    if not isinstance(output_path, Path):
        raise LiveValidationError("Live-validation output path must be a Path.")
    normalized_tasks = list(tasks)
    if len({task.provider for task in normalized_tasks}) != len(normalized_tasks):
        raise LiveValidationError("Provider task names must be unique.")
    for task in normalized_tasks:
        _validate_task(task)

    started = monotonic()
    summary: LiveValidationSummary = {
        "schema_version": LIVE_VALIDATION_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "overall_deadline_seconds": deadline,
        "status": "running",
        "providers": [
            {
                "provider": task.provider,
                "status": "not_started",
                "elapsed_seconds": None,
                "check_count": 0,
            }
            for task in normalized_tasks
        ],
        "checks": [],
        "elapsed_seconds": 0.0,
    }

    def checkpoint() -> None:
        summary["elapsed_seconds"] = round(monotonic() - started, 3)
        _atomic_write(output_path, summary)

    checkpoint()
    for index, task in enumerate(normalized_tasks):
        record = summary["providers"][index]
        remaining = deadline - (monotonic() - started)
        if remaining <= 0:
            for pending in summary["providers"][index:]:
                pending["status"] = "overall_deadline_exceeded"
            summary["status"] = "overall_deadline_exceeded"
            checkpoint()
            emit(f"Provider {task.provider}: overall_deadline_exceeded")
            return summary, 1

        record["status"] = "running"
        checkpoint()
        emit(f"Provider {task.provider}: running")
        outcomes: Queue[tuple[str, object]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcomes.put(("completed", task.run()))
            except KeyboardInterrupt:
                outcomes.put(("interrupted", None))
            except BaseException:
                outcomes.put(("unavailable", None))

        task_started = monotonic()
        worker = Thread(target=invoke, daemon=True)
        worker.start()
        try:
            worker.join(remaining)
        except KeyboardInterrupt:
            _close(task)
            record["status"] = "unavailable"
            record["elapsed_seconds"] = round(monotonic() - task_started, 3)
            summary["status"] = "cancelled"
            checkpoint()
            emit(f"Provider {task.provider}: unavailable")
            return summary, 1
        if worker.is_alive():
            _close(task)
            record["status"] = "overall_deadline_exceeded"
            record["elapsed_seconds"] = round(monotonic() - task_started, 3)
            for pending in summary["providers"][index + 1 :]:
                pending["status"] = "overall_deadline_exceeded"
            summary["status"] = "overall_deadline_exceeded"
            checkpoint()
            emit(f"Provider {task.provider}: overall_deadline_exceeded")
            return summary, 1

        outcome, payload = outcomes.get_nowait()
        record["elapsed_seconds"] = round(monotonic() - task_started, 3)
        if outcome == "interrupted":
            _close(task)
            record["status"] = "unavailable"
            summary["status"] = "cancelled"
            checkpoint()
            emit(f"Provider {task.provider}: unavailable")
            return summary, 1
        if outcome != "completed":
            _close(task)
            record["status"] = "unavailable"
            checkpoint()
            emit(f"Provider {task.provider}: unavailable")
            continue
        try:
            checks = _normalize_checks(
                task.provider,
                payload if isinstance(payload, Sequence) else (),
            )
        except LiveValidationError:
            _close(task)
            record["status"] = "unavailable"
            checkpoint()
            emit(f"Provider {task.provider}: unavailable")
            continue
        _close(task)
        record["status"] = "completed"
        record["check_count"] = len(checks)
        summary["checks"].extend(checks)
        checkpoint()
        emit(f"Provider {task.provider}: completed ({len(checks)} checks)")

    summary["status"] = "completed"
    checkpoint()
    return summary, 0


__all__ = [
    "DEFAULT_OVERALL_DEADLINE_SECONDS",
    "LIVE_VALIDATION_SCHEMA_VERSION",
    "LiveValidationError",
    "LiveValidationSummary",
    "ProviderCheckpoint",
    "ProviderTask",
    "run_live_validation",
]
