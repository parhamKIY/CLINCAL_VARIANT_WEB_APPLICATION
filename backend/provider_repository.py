"""Provider-boundary reuse of validated normalized repository snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal, cast

from backend.evidence_repository import (
    EvidenceRepository,
    EvidenceRepositoryError,
    EvidenceRepositoryIntegrityError,
    EvidenceRepositoryRecord,
    EvidenceRepositoryValidationError,
)
from backend.evidence_repository_freshness import EvidenceFreshnessPolicy
from backend.execution_trace import record_execution_event
from backend.logging_config import get_logger
from backend.provider_resilience import (
    OPERATIONAL_FAILURE_STATUSES,
    ProviderRole,
    ProviderStatus,
)


LOGGER = get_logger("provider_repository")
PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION = "1.0"
ProviderObservationStatus = Literal["success", "no_match"]
ProviderResultValidator = Callable[[object], dict[str, Any]]
ProviderStatusResolver = Callable[
    [Mapping[str, object]], ProviderObservationStatus | None
]
ProviderLiveCall = Callable[[], Mapping[str, object]]
ProviderClock = Callable[[], datetime]
_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "provider_result",
        "upstream_version",
        "fallback_for",
        "primary_failure",
    }
)


class ProviderRepositoryResultError(ValueError):
    """Raised when a normalized provider result violates its boundary."""


class ProviderRepositoryMetrics:
    """Thread-safe payload-free repository lookup counters."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts = {
            "cache_hits": 0,
            "cache_misses": 0,
            "stale_lookups": 0,
            "invalid_records_rejected": 0,
        }

    def _increment(self, field: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[field] += amount

    def snapshot(self) -> dict[str, int]:
        """Return bounded counters without query or clinical context."""

        with self._lock:
            return dict(self._counts)


PROVIDER_REPOSITORY_METRICS = ProviderRepositoryMetrics()


@dataclass(frozen=True)
class ProviderRepositoryContext:
    """Exact reusable identity and provenance for one provider operation."""

    canonical_variant: Mapping[str, object]
    assembly: str
    semantic_node: str
    provider: str
    provider_role: ProviderRole
    source: str
    query_identity: Mapping[str, object]
    upstream_version: str | None = None
    fallback_for: str | None = None
    primary_failure: ProviderStatus | None = None

    def __post_init__(self) -> None:
        if self.provider_role == "primary":
            if self.fallback_for is not None or self.primary_failure is not None:
                raise ProviderRepositoryResultError(
                    "Primary repository context cannot claim fallback."
                )
            return
        if self.provider_role != "fallback":
            raise ProviderRepositoryResultError(
                "Repository provider role must be primary or fallback."
            )
        if (
            not isinstance(self.fallback_for, str)
            or not self.fallback_for
            or self.primary_failure not in OPERATIONAL_FAILURE_STATUSES
        ):
            raise ProviderRepositoryResultError(
                "Fallback repository context requires an operational "
                "primary failure."
            )


def default_observation_status(
    result: Mapping[str, object],
) -> ProviderObservationStatus | None:
    """Map only usable normalized outcomes into repository observations."""

    status = result.get("status")
    if status in {"available", "partial", "success"}:
        return "success"
    if status == "no_match":
        return "no_match"
    return None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderRepositoryResultError(
            "Provider repository clock must be timezone-aware."
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _retrieved_at(result: Mapping[str, object]) -> str:
    value = result.get("retrieved_at")
    if not isinstance(value, str) or not value:
        raise ProviderRepositoryResultError(
            "Normalized provider result requires retrieved_at provenance."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderRepositoryResultError(
            "Normalized provider result has invalid retrieved_at provenance."
        ) from exc
    return _timestamp(parsed)


def _upstream_version(
    context: ProviderRepositoryContext,
    result: Mapping[str, object],
) -> str | None:
    if context.upstream_version is not None:
        return context.upstream_version
    for field in ("provider_version", "release"):
        value = result.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _decorate_result(
    result: Mapping[str, object],
    *,
    context: ProviderRepositoryContext,
    source_mode: Literal["live_provider", "repository_cache"],
    original_retrieved_at: str,
    cache_retrieved_at: str | None,
    upstream_version: str | None,
) -> dict[str, Any]:
    decorated = deepcopy(dict(result))
    decorated.update(
        {
            "source_mode": source_mode,
            "original_retrieved_at": original_retrieved_at,
            "cache_retrieved_at": cache_retrieved_at,
            "upstream_version": upstream_version,
        }
    )
    if context.provider_role == "fallback":
        decorated.update(
            {
                "provider_role": "fallback",
                "fallback_used": True,
                "fallback_for": context.fallback_for,
                "primary_failure": context.primary_failure,
            }
        )
    else:
        decorated.setdefault("provider_role", "primary")
        decorated.setdefault("fallback_used", False)
        decorated.setdefault("fallback_for", None)
        decorated.setdefault("primary_failure", None)
    decorated["retrieved_at"] = original_retrieved_at
    return decorated


def _payload_result(
    record: EvidenceRepositoryRecord,
    *,
    context: ProviderRepositoryContext,
    validate_result: ProviderResultValidator,
) -> tuple[dict[str, Any], str | None] | None:
    payload = record.normalized_payload
    if set(payload) != _PAYLOAD_FIELDS:
        return None
    if payload.get("schema_version") != (
        PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION
    ):
        return None
    if payload.get("status") != record.observation_status:
        return None
    if payload.get("fallback_for") != context.fallback_for:
        return None
    if payload.get("primary_failure") != context.primary_failure:
        return None
    result = validate_result(payload.get("provider_result"))
    upstream_version = payload.get("upstream_version")
    if upstream_version is not None and not isinstance(upstream_version, str):
        return None
    if record.observation_status == "no_match" and (
        result.get("status") != "no_match"
    ):
        return None
    return result, cast(str | None, upstream_version)


def _load_cached_result(
    repository: EvidenceRepository,
    *,
    context: ProviderRepositoryContext,
    validate_result: ProviderResultValidator,
    freshness_policy: EvidenceFreshnessPolicy,
    now: datetime,
) -> tuple[dict[str, Any] | None, bool, int]:
    records = repository.find_by_canonical_identity(
        canonical_variant=context.canonical_variant,
        assembly=context.assembly,
        semantic_node=context.semantic_node,
        provider=context.provider,
        query_identity=context.query_identity,
    )
    stale_found = False
    invalid_records = 0
    for record in reversed(records):
        if (
            record.provider_role != context.provider_role
            or record.source != context.source
        ):
            continue
        decision = freshness_policy.evaluate(
            record,
            now=now,
            expected_source_version=context.upstream_version,
        )
        if decision.state == "stale":
            stale_found = True
            break
        if decision.state == "unusable":
            invalid_records += 1
            continue
        try:
            cached = _payload_result(
                record,
                context=context,
                validate_result=validate_result,
            )
        except ProviderRepositoryResultError:
            invalid_records += 1
            continue
        if cached is None:
            invalid_records += 1
            continue
        result, upstream_version = cached
        return (
            _decorate_result(
                result,
                context=context,
                source_mode="repository_cache",
                original_retrieved_at=record.retrieved_at,
                cache_retrieved_at=_timestamp(now),
                upstream_version=upstream_version,
            ),
            stale_found,
            invalid_records,
        )
    return None, stale_found, invalid_records


def execute_provider_with_repository(
    *,
    repository: EvidenceRepository,
    context: ProviderRepositoryContext,
    live_call: ProviderLiveCall,
    validate_result: ProviderResultValidator,
    resolve_status: ProviderStatusResolver = default_observation_status,
    clock: ProviderClock = lambda: datetime.now(UTC),
    freshness_policy: EvidenceFreshnessPolicy | None = None,
    metrics: ProviderRepositoryMetrics | None = None,
) -> dict[str, Any]:
    """Reuse or store one fully normalized, revalidated provider result."""

    if not isinstance(repository, EvidenceRepository):
        raise ProviderRepositoryResultError(
            "repository must be an EvidenceRepository."
        )
    if not isinstance(context, ProviderRepositoryContext):
        raise ProviderRepositoryResultError(
            "context must be ProviderRepositoryContext."
        )
    if not callable(live_call) or not callable(validate_result):
        raise ProviderRepositoryResultError(
            "Provider repository callbacks must be callable."
        )
    policy = freshness_policy or EvidenceFreshnessPolicy.from_settings()
    if not isinstance(policy, EvidenceFreshnessPolicy):
        raise ProviderRepositoryResultError(
            "freshness_policy must be an EvidenceFreshnessPolicy."
        )
    metric_store = metrics or PROVIDER_REPOSITORY_METRICS
    if not isinstance(metric_store, ProviderRepositoryMetrics):
        raise ProviderRepositoryResultError(
            "metrics must be ProviderRepositoryMetrics."
        )
    lookup_time = clock()
    stale_found = False
    invalid_records = 0
    try:
        cached, stale_found, invalid_records = _load_cached_result(
            repository,
            context=context,
            validate_result=validate_result,
            freshness_policy=policy,
            now=lookup_time,
        )
    except (EvidenceRepositoryIntegrityError, ProviderRepositoryResultError):
        invalid_records = 1
        LOGGER.warning(
            "event=evidence_repository_lookup outcome=invalid "
            "provider=%s semantic_node=%s",
            context.provider,
            context.semantic_node,
        )
        record_execution_event(
            "repository_record_rejected",
            scope="repository",
            provider=context.provider,
            capability=context.semantic_node,
            status="invalid",
            outcome_category="invalid_record",
            reason_category="integrity_or_validation_failure",
        )
        cached = None
    except (EvidenceRepositoryError, EvidenceRepositoryValidationError):
        LOGGER.warning(
            "event=evidence_repository_lookup outcome=repository_error "
            "provider=%s semantic_node=%s",
            context.provider,
            context.semantic_node,
        )
        record_execution_event(
            "repository_lookup_failed",
            scope="repository",
            provider=context.provider,
            capability=context.semantic_node,
            status="error",
            outcome_category="repository_error",
            reason_category="repository_error",
        )
        cached = None
    if cached is not None:
        metric_store._increment("cache_hits")
        if invalid_records:
            metric_store._increment(
                "invalid_records_rejected",
                invalid_records,
            )
        LOGGER.info(
            "event=evidence_repository_lookup outcome=hit provider=%s "
            "semantic_node=%s invalid_records=%s",
            context.provider,
            context.semantic_node,
            invalid_records,
        )
        record_execution_event(
            "repository_lookup_hit",
            scope="repository",
            provider=context.provider,
            capability=context.semantic_node,
            status="success",
            outcome_category="cache_hit",
            source_mode="repository_cache",
            provider_role=context.provider_role,
            fallback_for=context.fallback_for,
        )
        record_execution_event(
            "provider_source_selected",
            scope="provider",
            provider=context.provider,
            capability=context.semantic_node,
            status=str(cached.get("status", "success")),
            outcome_category="success",
            source_mode="repository_cache",
            provider_role=context.provider_role,
            fallback_for=context.fallback_for,
        )
        return cached
    metric_store._increment("cache_misses")
    if stale_found:
        metric_store._increment("stale_lookups")
    if invalid_records:
        metric_store._increment(
            "invalid_records_rejected",
            invalid_records,
        )
    LOGGER.info(
        "event=evidence_repository_lookup outcome=miss provider=%s "
        "semantic_node=%s stale=%s invalid=%s",
        context.provider,
        context.semantic_node,
        str(stale_found).casefold(),
        str(bool(invalid_records)).casefold(),
    )
    record_execution_event(
        (
            "repository_lookup_stale"
            if stale_found
            else "repository_lookup_miss"
        ),
        scope="repository",
        provider=context.provider,
        capability=context.semantic_node,
        status="stale" if stale_found else "missing",
        outcome_category="cache_miss",
        provider_role=context.provider_role,
        fallback_for=context.fallback_for,
    )

    raw_live_result = live_call()
    if not isinstance(raw_live_result, Mapping):
        raise ProviderRepositoryResultError(
            "Normalized provider result must be a mapping."
        )
    live_result = deepcopy(dict(raw_live_result))
    observation_status = resolve_status(live_result)
    if observation_status is not None:
        live_result = validate_result(live_result)
    retrieved_at = _retrieved_at(live_result)
    upstream_version = _upstream_version(context, live_result)
    if observation_status is not None:
        payload = {
            "schema_version": PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION,
            "status": observation_status,
            "provider_result": deepcopy(live_result),
            "upstream_version": upstream_version,
            "fallback_for": context.fallback_for,
            "primary_failure": context.primary_failure,
        }
        try:
            repository.store_provider_observation(
                canonical_variant=context.canonical_variant,
                assembly=context.assembly,
                semantic_node=context.semantic_node,
                provider=context.provider,
                provider_role=context.provider_role,
                source=context.source,
                query_identity=context.query_identity,
                normalized_payload=payload,
                observation_status=observation_status,
                retrieved_at=retrieved_at,
            )
        except (
            EvidenceRepositoryError,
            EvidenceRepositoryValidationError,
        ):
            LOGGER.warning(
                "event=evidence_repository_store_failed provider=%s "
                "semantic_node=%s status=%s",
                context.provider,
                context.semantic_node,
                observation_status,
            )
    decorated = _decorate_result(
        live_result,
        context=context,
        source_mode="live_provider",
        original_retrieved_at=retrieved_at,
        cache_retrieved_at=None,
        upstream_version=upstream_version,
    )
    record_execution_event(
        "provider_source_selected",
        scope="provider",
        provider=context.provider,
        capability=context.semantic_node,
        status=str(decorated.get("status", "success")),
        outcome_category=(
            "no_match" if observation_status == "no_match" else "success"
        ),
        source_mode="live_provider",
        provider_role=context.provider_role,
        fallback_for=context.fallback_for,
    )
    return decorated


__all__ = [
    "PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION",
    "PROVIDER_REPOSITORY_METRICS",
    "ProviderRepositoryContext",
    "ProviderRepositoryMetrics",
    "ProviderRepositoryResultError",
    "default_observation_status",
    "execute_provider_with_repository",
]
