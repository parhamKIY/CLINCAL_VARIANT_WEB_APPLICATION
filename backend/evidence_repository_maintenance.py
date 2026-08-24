"""Callable maintenance operations for the evidence repository."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from backend.evidence_repository import (
    EvidenceRepository,
    EvidenceRepositoryRecord,
)
from backend.evidence_repository_freshness import EvidenceFreshnessPolicy
from backend.logging_config import get_logger
from config import settings


LOGGER = get_logger("evidence_repository_maintenance")
MaintenanceClock = Callable[[], datetime]
RepositoryHealthStatus = Literal["healthy", "degraded"]


@dataclass(frozen=True)
class RepositoryRecordFreshness:
    """Safe maintenance projection for one stale repository record."""

    record_id: str
    provider: str
    semantic_node: str
    retrieved_at: str
    reason: str


@dataclass(frozen=True)
class RepositoryHealthReport:
    """Payload-free repository integrity and freshness counts."""

    status: RepositoryHealthStatus
    total_records: int
    valid_records: int
    invalid_records: int
    fresh_records: int
    stale_records: int
    unusable_records: int


@dataclass(frozen=True)
class RepositoryCleanupResult:
    """Bounded result for explicit invalid-record removal."""

    records_evaluated: int
    records_removed: int
    records_retained: int
    reason: str


class EvidenceRepositoryMaintenance:
    """Inspect freshness and explicitly remove only invalid records."""

    def __init__(
        self,
        repository: EvidenceRepository,
        freshness_policy: EvidenceFreshnessPolicy,
        *,
        clock: MaintenanceClock = lambda: datetime.now(UTC),
        expected_source_versions: Mapping[
            tuple[str, str, str], str
        ] | None = None,
    ) -> None:
        if not isinstance(repository, EvidenceRepository):
            raise TypeError("repository must be an EvidenceRepository.")
        if not isinstance(freshness_policy, EvidenceFreshnessPolicy):
            raise TypeError(
                "freshness_policy must be an EvidenceFreshnessPolicy."
            )
        if not callable(clock):
            raise TypeError("maintenance clock must be callable.")
        self._repository = repository
        self._freshness_policy = freshness_policy
        self._clock = clock
        self._expected_source_versions = dict(
            expected_source_versions
            or {
                (
                    "gnomad",
                    "population_evidence",
                    "GRCh37",
                ): settings.GNOMAD_DATASET_GRCH37,
                (
                    "gnomad",
                    "population_evidence",
                    "GRCh38",
                ): settings.GNOMAD_DATASET_GRCH38,
            }
        )

    def _expected_source_version(
        self,
        record: EvidenceRepositoryRecord,
    ) -> str | None:
        return self._expected_source_versions.get(
            (record.provider, record.semantic_node, record.assembly)
        )

    def identify_stale_records(self) -> tuple[RepositoryRecordFreshness, ...]:
        """Return safe metadata for stale records without deleting history."""

        scan = self._repository.scan_integrity()
        now = self._clock()
        stale: list[RepositoryRecordFreshness] = []
        for record in scan.records:
            decision = self._freshness_policy.evaluate(
                record,
                now=now,
                expected_source_version=self._expected_source_version(record),
            )
            if decision.state == "stale":
                stale.append(
                    RepositoryRecordFreshness(
                        record_id=record.record_id,
                        provider=record.provider,
                        semantic_node=record.semantic_node,
                        retrieved_at=record.retrieved_at,
                        reason=decision.reason,
                    )
                )
        return tuple(stale)

    def health_report(self) -> RepositoryHealthReport:
        """Report payload-free repository integrity and freshness counts."""

        scan = self._repository.scan_integrity()
        now = self._clock()
        counts = {"fresh": 0, "stale": 0, "unusable": 0}
        for record in scan.records:
            decision = self._freshness_policy.evaluate(
                record,
                now=now,
                expected_source_version=self._expected_source_version(record),
            )
            counts[decision.state] += 1
        status: RepositoryHealthStatus = (
            "degraded"
            if scan.invalid_records or counts["unusable"] or counts["stale"]
            else "healthy"
        )
        return RepositoryHealthReport(
            status=status,
            total_records=scan.total_records,
            valid_records=len(scan.records),
            invalid_records=scan.invalid_records,
            fresh_records=counts["fresh"],
            stale_records=counts["stale"],
            unusable_records=counts["unusable"],
        )

    def remove_invalid_records(self) -> RepositoryCleanupResult:
        """Explicitly delete corruption while retaining all valid history."""

        LOGGER.info(
            "event=evidence_repository_maintenance_started "
            "operation=remove_invalid reason=integrity_validation_failed"
        )
        cleanup = self._repository.remove_invalid_records()
        LOGGER.info(
            "event=evidence_repository_maintenance_finished "
            "operation=remove_invalid records_evaluated=%s "
            "records_removed=%s records_retained=%s "
            "reason=integrity_validation_failed",
            cleanup.records_evaluated,
            cleanup.records_removed,
            cleanup.records_retained,
        )
        return RepositoryCleanupResult(
            records_evaluated=cleanup.records_evaluated,
            records_removed=cleanup.records_removed,
            records_retained=cleanup.records_retained,
            reason="integrity_validation_failed",
        )


__all__ = [
    "EvidenceRepositoryMaintenance",
    "RepositoryCleanupResult",
    "RepositoryHealthReport",
    "RepositoryRecordFreshness",
]
