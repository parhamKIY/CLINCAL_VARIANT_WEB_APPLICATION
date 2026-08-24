"""Provider-aware freshness policy for evidence repository snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Mapping

from backend.evidence_repository import (
    EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION,
    EvidenceRepositoryRecord,
)
from config import settings


FreshnessState = Literal["fresh", "stale", "unusable"]
FreshnessScope = tuple[str, str]


class EvidenceFreshnessPolicyError(ValueError):
    """Raised when freshness policy input is invalid."""


@dataclass(frozen=True)
class EvidenceFreshnessDecision:
    """One deterministic freshness classification."""

    state: FreshnessState
    reason: str
    age_seconds: float
    ttl_seconds: int


def _scope_value(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceFreshnessPolicyError(
            f"Freshness {field} must be a non-empty string."
        )
    return value.strip().casefold()


def _ttl_value(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceFreshnessPolicyError(
            f"Freshness {field} must be a positive integer."
        )
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceFreshnessPolicyError(
            "Freshness evaluation time must be timezone-aware."
        )
    return value.astimezone(UTC)


@dataclass(frozen=True)
class EvidenceFreshnessPolicy:
    """Resolve TTLs without embedding freshness state in stored records."""

    default_ttl_seconds: int
    ttl_seconds_by_scope: Mapping[FreshnessScope, int]

    def __post_init__(self) -> None:
        default_ttl = _ttl_value(
            self.default_ttl_seconds,
            field="default TTL",
        )
        normalized: dict[FreshnessScope, int] = {}
        if not isinstance(self.ttl_seconds_by_scope, Mapping):
            raise EvidenceFreshnessPolicyError(
                "Freshness scope TTLs must be a mapping."
            )
        for scope, ttl in self.ttl_seconds_by_scope.items():
            if not isinstance(scope, tuple) or len(scope) != 2:
                raise EvidenceFreshnessPolicyError(
                    "Freshness scope must contain provider and capability."
                )
            normalized[
                (
                    _scope_value(scope[0], field="provider"),
                    _scope_value(scope[1], field="capability"),
                )
            ] = _ttl_value(ttl, field="scope TTL")
        object.__setattr__(self, "default_ttl_seconds", default_ttl)
        object.__setattr__(self, "ttl_seconds_by_scope", normalized)

    @classmethod
    def from_settings(cls) -> EvidenceFreshnessPolicy:
        """Build the configured policy for currently integrated providers."""

        return cls(
            default_ttl_seconds=(
                settings.EVIDENCE_REPOSITORY_DEFAULT_TTL_SECONDS
            ),
            ttl_seconds_by_scope={
                ("gnomad", "population_evidence"): (
                    settings.EVIDENCE_REPOSITORY_GNOMAD_TTL_SECONDS
                ),
                ("ensembl_variation", "population_evidence"): (
                    settings.EVIDENCE_REPOSITORY_ENSEMBL_VARIATION_TTL_SECONDS
                ),
            },
        )

    def ttl_for(self, provider: object, semantic_node: object) -> int:
        """Return the configured TTL for one provider capability."""

        scope = (
            _scope_value(provider, field="provider"),
            _scope_value(semantic_node, field="capability"),
        )
        return self.ttl_seconds_by_scope.get(
            scope,
            self.default_ttl_seconds,
        )

    def evaluate(
        self,
        record: EvidenceRepositoryRecord,
        *,
        now: datetime,
        expected_source_version: str | None = None,
    ) -> EvidenceFreshnessDecision:
        """Classify one verified record as fresh, stale, or unusable."""

        if not isinstance(record, EvidenceRepositoryRecord):
            raise EvidenceFreshnessPolicyError(
                "Freshness evaluation requires a repository record."
            )
        current_time = _aware_utc(now)
        try:
            retrieved_at = datetime.fromisoformat(
                record.retrieved_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise EvidenceFreshnessPolicyError(
                "Repository retrieval timestamp is invalid."
            ) from exc
        retrieved_at = _aware_utc(retrieved_at)
        ttl_seconds = self.ttl_for(record.provider, record.semantic_node)
        age_seconds = (current_time - retrieved_at).total_seconds()
        if record.schema_version != EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION:
            return EvidenceFreshnessDecision(
                state="unusable",
                reason="incompatible_record_schema",
                age_seconds=age_seconds,
                ttl_seconds=ttl_seconds,
            )
        if age_seconds < 0:
            return EvidenceFreshnessDecision(
                state="unusable",
                reason="retrieval_timestamp_in_future",
                age_seconds=age_seconds,
                ttl_seconds=ttl_seconds,
            )
        if expected_source_version is not None:
            stored_source_version = record.normalized_payload.get(
                "upstream_version"
            )
            if stored_source_version != expected_source_version:
                return EvidenceFreshnessDecision(
                    state="unusable",
                    reason="source_version_mismatch",
                    age_seconds=age_seconds,
                    ttl_seconds=ttl_seconds,
                )
        if age_seconds > ttl_seconds:
            return EvidenceFreshnessDecision(
                state="stale",
                reason="ttl_expired",
                age_seconds=age_seconds,
                ttl_seconds=ttl_seconds,
            )
        return EvidenceFreshnessDecision(
            state="fresh",
            reason="within_ttl",
            age_seconds=age_seconds,
            ttl_seconds=ttl_seconds,
        )


__all__ = [
    "EvidenceFreshnessDecision",
    "EvidenceFreshnessPolicy",
    "EvidenceFreshnessPolicyError",
    "FreshnessState",
]
