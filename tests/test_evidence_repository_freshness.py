"""Stage 7.4 repository freshness, maintenance, and metrics tests."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests

from backend.conditional_enrichment import fetch_gnomad_evidence
from backend.evidence_repository import EvidenceRepository
from backend.evidence_repository_freshness import EvidenceFreshnessPolicy
from backend.evidence_repository_maintenance import (
    EvidenceRepositoryMaintenance,
)
from backend.provider_repository import (
    PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION,
    ProviderRepositoryMetrics,
)
from config import Settings, settings
from tests.test_pipeline import FakeConditionalSession, FakeResponse
from tests.test_provider_repository_integration import (
    _candidate,
    _gnomad_payload,
)


pytestmark = [
    pytest.mark.regression,
    pytest.mark.stage59_testing_v3,
]

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
GNOMAD_VERSION = settings.GNOMAD_DATASET_GRCH38


def _repository(tmp_path: Path) -> EvidenceRepository:
    return EvidenceRepository(tmp_path / "repository.sqlite3")


def _gnomad_result(
    retrieved_at: datetime,
    *,
    status: str = "available",
) -> dict[str, object]:
    available = status == "available"
    return {
        "status": status,
        "response_status": status,
        "provider": "gnomAD",
        "provider_version": GNOMAD_VERSION,
        "source": "gnomad",
        "assembly": "GRCh38",
        "dataset": GNOMAD_VERSION,
        "release": GNOMAD_VERSION,
        "query_identifier": "2-166848215-C-T",
        "variant_id": "2-166848215-C-T",
        "retrieved_at": retrieved_at.isoformat().replace("+00:00", "Z"),
        "global_maf": 0.00001 if available else None,
        "population_frequency": 0.00001 if available else None,
        "joint": (
            {
                "allele_count": 2,
                "allele_number": 200_000,
                "allele_frequency": 0.00001,
                "homozygote_count": 0,
                "hemizygote_count": 0,
            }
            if available
            else None
        ),
        "exome": None,
        "genome": None,
        "populations": [],
        "warnings": [],
        "failure_reason": None,
        "provider_role": "primary",
        "fallback_used": False,
        "fallback_for": None,
        "primary_failure": None,
    }


def _store_gnomad(
    repository: EvidenceRepository,
    *,
    retrieved_at: datetime,
    status: str = "available",
    upstream_version: str = GNOMAD_VERSION,
):
    observation_status = "no_match" if status == "no_match" else "success"
    return repository.store_provider_observation(
        canonical_variant=_candidate()["variant"],  # type: ignore[arg-type]
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="gnomad",
        provider_role="primary",
        source="gnomad",
        query_identity={
            "operation": "lookup_variant",
            "variant_id": "2-166848215-C-T",
            "dataset": GNOMAD_VERSION,
        },
        normalized_payload={
            "schema_version": PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION,
            "status": observation_status,
            "provider_result": _gnomad_result(
                retrieved_at,
                status=status,
            ),
            "upstream_version": upstream_version,
            "fallback_for": None,
            "primary_failure": None,
        },
        observation_status=observation_status,
        retrieved_at=retrieved_at,
    )


def _policy(
    *,
    gnomad_ttl: int = 3_600,
    ensembl_ttl: int = 3_600,
) -> EvidenceFreshnessPolicy:
    return EvidenceFreshnessPolicy(
        default_ttl_seconds=1_800,
        ttl_seconds_by_scope={
            ("gnomad", "population_evidence"): gnomad_ttl,
            ("ensembl_variation", "population_evidence"): ensembl_ttl,
        },
    )


def test_fresh_record_returns_repository_result(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _store_gnomad(
        repository,
        retrieved_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    metrics = ProviderRepositoryMetrics()
    session = FakeConditionalSession()

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(),
        repository_metrics=metrics,
    )

    assert session.post_calls == []
    assert result["status"] == "available"
    assert result["source_mode"] == "repository_cache"
    assert metrics.snapshot() == {
        "cache_hits": 1,
        "cache_misses": 0,
        "stale_lookups": 0,
        "invalid_records_rejected": 0,
    }


def test_expired_record_triggers_live_refresh(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _store_gnomad(repository, retrieved_at=NOW - timedelta(hours=2))
    metrics = ProviderRepositoryMetrics()
    session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(gnomad_ttl=3_600),
        repository_metrics=metrics,
        repository_clock=lambda: NOW,
    )

    assert len(session.post_calls) == 1
    assert result["status"] == "available"
    assert result["source_mode"] == "live_provider"
    assert metrics.snapshot() == {
        "cache_hits": 0,
        "cache_misses": 1,
        "stale_lookups": 1,
        "invalid_records_rejected": 0,
    }


def test_failed_refresh_never_returns_stale_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CONDITIONAL_ENRICHMENT_MAX_RETRIES", 0)
    repository = _repository(tmp_path)
    stale = _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(hours=2),
    )
    metrics = ProviderRepositoryMetrics()

    result = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[requests.Timeout("bounded timeout")]
        ),
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(gnomad_ttl=3_600),
        repository_metrics=metrics,
        repository_clock=lambda: NOW,
    )

    assert result["status"] == "unavailable"
    assert result["source_mode"] == "live_provider"
    assert result["primary_failure"] == "timeout"
    assert repository.find_by_canonical_identity(
        canonical_variant=_candidate()["variant"],  # type: ignore[arg-type]
        assembly="GRCh38",
        provider="gnomad",
    ) == [stale]


def test_corrupted_record_is_rejected_and_live_provider_is_used(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    stored = _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(minutes=5),
    )
    connection = sqlite3.connect(repository.database_path)
    try:
        connection.execute(
            """
            UPDATE evidence_repository_records
            SET normalized_payload_json = '{"corrupted":true}'
            WHERE record_id = ?
            """,
            (stored.record_id,),
        )
        connection.commit()
    finally:
        connection.close()
    metrics = ProviderRepositoryMetrics()
    session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(),
        repository_metrics=metrics,
        repository_clock=lambda: NOW,
    )

    assert len(session.post_calls) == 1
    assert result["source_mode"] == "live_provider"
    assert metrics.snapshot()["invalid_records_rejected"] == 1


def test_repository_storage_failure_is_a_miss_not_an_invalid_record(
    tmp_path: Path,
) -> None:
    blocking_parent = tmp_path / "not-a-directory"
    blocking_parent.touch()
    repository = EvidenceRepository(blocking_parent / "repository.sqlite3")
    metrics = ProviderRepositoryMetrics()

    result = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[FakeResponse(200, _gnomad_payload())]
        ),
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(),
        repository_metrics=metrics,
    )

    assert result["status"] == "available"
    assert metrics.snapshot() == {
        "cache_hits": 0,
        "cache_misses": 1,
        "stale_lookups": 0,
        "invalid_records_rejected": 0,
    }


def test_invalid_newer_snapshot_is_counted_before_older_fresh_hit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    older = _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(minutes=10),
    )
    invalid_result = _gnomad_result(NOW - timedelta(minutes=5))
    invalid_result["variant_id"] = "wrong-identity"
    repository.store_provider_observation(
        canonical_variant=_candidate()["variant"],  # type: ignore[arg-type]
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="gnomad",
        provider_role="primary",
        source="gnomad",
        query_identity={
            "operation": "lookup_variant",
            "variant_id": "2-166848215-C-T",
            "dataset": GNOMAD_VERSION,
        },
        normalized_payload={
            "schema_version": PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION,
            "status": "success",
            "provider_result": invalid_result,
            "upstream_version": GNOMAD_VERSION,
            "fallback_for": None,
            "primary_failure": None,
        },
        observation_status="success",
        retrieved_at=NOW - timedelta(minutes=5),
    )
    metrics = ProviderRepositoryMetrics()

    result = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(),  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(),
        repository_metrics=metrics,
        repository_clock=lambda: NOW,
    )

    assert result["source_mode"] == "repository_cache"
    assert result["retrieved_at"] == older.retrieved_at
    assert metrics.snapshot() == {
        "cache_hits": 1,
        "cache_misses": 0,
        "stale_lookups": 0,
        "invalid_records_rejected": 1,
    }


def test_different_provider_ttl_policies_classify_same_age_differently(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    retrieved_at = NOW - timedelta(hours=2)
    gnomad = _store_gnomad(repository, retrieved_at=retrieved_at)
    ensembl = repository.store_provider_observation(
        canonical_variant=_candidate()["variant"],  # type: ignore[arg-type]
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="ensembl_variation",
        provider_role="primary",
        source="ensembl_variation",
        query_identity={"rsid": "rs121913529", "assembly": "GRCh38"},
        normalized_payload={"status": "success", "release": "156"},
        observation_status="success",
        retrieved_at=retrieved_at,
    )
    policy = _policy(gnomad_ttl=14_400, ensembl_ttl=3_600)

    assert policy.evaluate(gnomad, now=NOW).state == "fresh"
    assert policy.evaluate(ensembl, now=NOW).state == "stale"
    assert policy.ttl_for("gnomad", "population_evidence") == 14_400
    assert policy.ttl_for(
        "ensembl_variation",
        "population_evidence",
    ) == 3_600


def test_changed_source_version_is_unusable_and_triggers_refresh(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(minutes=5),
        upstream_version="obsolete-release",
    )
    metrics = ProviderRepositoryMetrics()
    session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(),
        repository_metrics=metrics,
        repository_clock=lambda: NOW,
    )

    assert len(session.post_calls) == 1
    assert result["source_mode"] == "live_provider"
    assert metrics.snapshot()["invalid_records_rejected"] == 1


def test_cached_no_match_retains_semantics_with_freshness_policy(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(minutes=5),
        status="no_match",
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(),  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
        repository_freshness_policy=_policy(),
        repository_clock=lambda: NOW,
    )

    assert result["status"] == "no_match"
    assert result["response_status"] == "no_match"
    assert result["source_mode"] == "repository_cache"


def test_metrics_and_logs_contain_no_sensitive_context(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _repository(tmp_path)
    _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(minutes=5),
    )
    metrics = ProviderRepositoryMetrics()
    candidate = _candidate()
    candidate["clinical_narrative"] = "SENSITIVE-MARKER"

    with caplog.at_level(
        logging.INFO,
        logger="clinical_variant_app.provider_repository",
    ):
        fetch_gnomad_evidence(
            candidate,
            session=FakeConditionalSession(),  # type: ignore[arg-type]
            evidence_repository=repository,
            use_repository=True,
            repository_freshness_policy=_policy(),
            repository_metrics=metrics,
            repository_clock=lambda: NOW,
        )

    rendered = json.dumps(metrics.snapshot()) + caplog.text
    assert "SENSITIVE-MARKER" not in rendered
    assert "clinical_narrative" not in rendered
    assert set(metrics.snapshot()) == {
        "cache_hits",
        "cache_misses",
        "stale_lookups",
        "invalid_records_rejected",
    }


def test_maintenance_reports_stale_and_removes_only_invalid_records(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    stale = _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(hours=2),
    )
    invalid = _store_gnomad(
        repository,
        retrieved_at=NOW - timedelta(minutes=5),
        status="no_match",
    )
    connection = sqlite3.connect(repository.database_path)
    try:
        connection.execute(
            """
            UPDATE evidence_repository_records
            SET payload_sha256 = ?
            WHERE record_id = ?
            """,
            ("0" * 64, invalid.record_id),
        )
        connection.commit()
    finally:
        connection.close()
    maintenance = EvidenceRepositoryMaintenance(
        repository,
        _policy(gnomad_ttl=3_600),
        clock=lambda: NOW,
    )

    stale_records = maintenance.identify_stale_records()
    health = maintenance.health_report()
    cleanup = maintenance.remove_invalid_records()

    assert [item.record_id for item in stale_records] == [stale.record_id]
    assert health.status == "degraded"
    assert health.total_records == 2
    assert health.valid_records == 1
    assert health.invalid_records == 1
    assert health.stale_records == 1
    assert cleanup.records_evaluated == 2
    assert cleanup.records_removed == 1
    assert cleanup.records_retained == 1
    assert cleanup.reason == "integrity_validation_failed"
    assert repository.find_by_canonical_identity(
        canonical_variant=_candidate()["variant"],  # type: ignore[arg-type]
        assembly="GRCh38",
    ) == [stale]


def test_maintenance_uses_assembly_specific_gnomad_version(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.store_provider_observation(
        canonical_variant=_candidate()["variant"],  # type: ignore[arg-type]
        assembly="GRCh37",
        semantic_node="population_evidence",
        provider="gnomad",
        provider_role="primary",
        source="gnomad",
        query_identity={
            "operation": "lookup_variant",
            "variant_id": "2-166848215-C-T",
            "dataset": settings.GNOMAD_DATASET_GRCH37,
        },
        normalized_payload={
            "schema_version": PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION,
            "status": "success",
            "provider_result": {"status": "available"},
            "upstream_version": settings.GNOMAD_DATASET_GRCH37,
            "fallback_for": None,
            "primary_failure": None,
        },
        observation_status="success",
        retrieved_at=NOW - timedelta(minutes=5),
    )
    maintenance = EvidenceRepositoryMaintenance(
        repository,
        _policy(),
        clock=lambda: NOW,
    )

    health = maintenance.health_report()

    assert health.status == "healthy"
    assert health.fresh_records == 1
    assert health.unusable_records == 0


def test_settings_back_provider_specific_freshness_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Settings,
        "EVIDENCE_REPOSITORY_DEFAULT_TTL_SECONDS",
        7_200,
    )
    monkeypatch.setattr(
        Settings,
        "EVIDENCE_REPOSITORY_GNOMAD_TTL_SECONDS",
        14_400,
    )
    monkeypatch.setattr(
        Settings,
        "EVIDENCE_REPOSITORY_ENSEMBL_VARIATION_TTL_SECONDS",
        3_600,
    )

    policy = EvidenceFreshnessPolicy.from_settings()

    assert policy.ttl_for("unknown", "unknown") == 7_200
    assert policy.ttl_for("gnomad", "population_evidence") == 14_400
    assert policy.ttl_for(
        "ensembl_variation",
        "population_evidence",
    ) == 3_600


def test_invalid_ttl_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Settings,
        "EVIDENCE_REPOSITORY_DEFAULT_TTL_SECONDS",
        0,
    )

    with pytest.raises(RuntimeError, match="TTL values must be positive"):
        Settings.validate()
