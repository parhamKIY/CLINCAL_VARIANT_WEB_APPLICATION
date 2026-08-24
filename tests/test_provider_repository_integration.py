"""Stage 7.3 provider-boundary Evidence Repository integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from backend.conditional_enrichment import (
    fetch_gnomad_evidence,
    fetch_population_evidence_with_fallback,
)
from backend.evidence_repository import EvidenceRepository
from backend.provider_repository import (
    PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION,
)
from backend.report import _compact_conditional_enrichment
from config import settings
from tests.test_pipeline import FakeConditionalSession, FakeResponse


pytestmark = [
    pytest.mark.regression,
    pytest.mark.stage59_testing_v3,
]


def _candidate() -> dict[str, object]:
    return {
        "variant": {
            "chrom": "2",
            "pos": 166_848_215,
            "ref": "C",
            "alt": "T",
        },
        "assembly": "GRCh38",
        "sources": {"myvariant": {"rsid": "rs121913529"}},
    }


def _gnomad_payload() -> dict[str, object]:
    return {
        "data": {
            "variant": {
                "variant_id": "2-166848215-C-T",
                "ref": "C",
                "alt": "T",
                "rsid": "rs121913529",
                "joint": {
                    "ac": 2,
                    "an": 200_000,
                    "populations": [
                        {"id": "nfe", "ac": 2, "an": 100_000}
                    ],
                },
                "exome": {
                    "ac": 1,
                    "an": 150_000,
                    "ac_hom": 0,
                    "ac_hemi": 0,
                    "populations": [],
                },
                "genome": {
                    "ac": 1,
                    "an": 50_000,
                    "ac_hom": 0,
                    "ac_hemi": 0,
                    "populations": [],
                },
            }
        }
    }


def _ensembl_payload() -> dict[str, object]:
    return {
        "name": "rs121913529",
        "source": "dbSNP",
        "release": 156,
        "MAF": "0.001",
        "mappings": [
            {
                "assembly_name": "GRCh38",
                "seq_region_name": "2",
                "start": 166_848_215,
                "allele_string": "C/T",
            }
        ],
        "populations": [
            {
                "population": "1000GENOMES:phase_3:EUR",
                "allele": "T",
                "frequency": 0.002,
            }
        ],
    }


def _repository(tmp_path: Path) -> EvidenceRepository:
    return EvidenceRepository(tmp_path / "repository.sqlite3")


def _records(
    repository: EvidenceRepository,
    *,
    provider: str,
) -> list[object]:
    return repository.find_by_canonical_identity(
        canonical_variant=_candidate()["variant"],  # type: ignore[arg-type]
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider=provider,
    )


def test_cache_hit_avoids_provider_call_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    live_session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )
    live = fetch_gnomad_evidence(
        _candidate(),
        session=live_session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )
    cache_session = FakeConditionalSession()

    cached = fetch_gnomad_evidence(
        _candidate(),
        session=cache_session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )

    assert len(live_session.post_calls) == 1
    assert cache_session.post_calls == []
    assert cached["status"] == "available"
    assert cached["provider"] == "gnomAD"
    assert cached["source_mode"] == "repository_cache"
    assert cached["retrieved_at"] == live["retrieved_at"]
    assert cached["original_retrieved_at"] == live["retrieved_at"]
    assert isinstance(cached["cache_retrieved_at"], str)
    assert cached["upstream_version"] == settings.GNOMAD_DATASET_GRCH38


def test_cache_provenance_survives_evidence_projection(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[FakeResponse(200, _gnomad_payload())]
        ),
        evidence_repository=repository,
        use_repository=True,
    )
    cached = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(),  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )

    compact = _compact_conditional_enrichment(
        {"population_frequency": cached}
    )["population_frequency"]

    assert compact["provider"] == "gnomAD"
    assert compact["source_mode"] == "repository_cache"
    assert compact["retrieved_at"] == cached["retrieved_at"]
    assert compact["original_retrieved_at"] == cached["retrieved_at"]
    assert compact["cache_retrieved_at"] == cached["cache_retrieved_at"]
    assert compact["upstream_version"] == settings.GNOMAD_DATASET_GRCH38


def test_cache_miss_executes_existing_provider_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )

    assert len(session.post_calls) == 1
    assert result["status"] == "available"
    assert result["source_mode"] == "live_provider"
    assert result["cache_retrieved_at"] is None


def test_successful_provider_result_stores_normalized_snapshot(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[FakeResponse(200, _gnomad_payload())]
        ),
        evidence_repository=repository,
        use_repository=True,
    )

    records = _records(repository, provider="gnomad")
    assert len(records) == 1
    record = records[0]
    assert record.provider == "gnomad"
    assert record.provider_role == "primary"
    assert record.observation_status == "success"
    assert record.normalized_payload["provider_result"]["status"] == (
        "available"
    )


def test_provider_timeout_is_not_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CONDITIONAL_ENRICHMENT_MAX_RETRIES", 0)
    repository = _repository(tmp_path)

    result = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[requests.Timeout("bounded timeout")]
        ),
        evidence_repository=repository,
        use_repository=True,
    )

    assert result["status"] == "unavailable"
    assert result["primary_failure"] == "timeout"
    assert _records(repository, provider="gnomad") == []


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_http_429_and_5xx_are_not_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(settings, "CONDITIONAL_ENRICHMENT_MAX_RETRIES", 0)
    repository = _repository(tmp_path)

    result = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[FakeResponse(status_code, {})]
        ),
        evidence_repository=repository,
        use_repository=True,
    )

    assert result["status"] == "unavailable"
    assert _records(repository, provider="gnomad") == []


def test_malformed_provider_response_is_not_stored(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[
                FakeResponse(
                    200,
                    {"data": {"variant": {"variant_id": "wrong"}}},
                )
            ]
        ),
        evidence_repository=repository,
        use_repository=True,
    )

    assert result["status"] == "invalid_response"
    assert _records(repository, provider="gnomad") == []


def test_semantically_invalid_cached_result_is_rejected_before_reuse(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
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
            "dataset": settings.GNOMAD_DATASET_GRCH38,
        },
        normalized_payload={
            "schema_version": PROVIDER_REPOSITORY_PAYLOAD_SCHEMA_VERSION,
            "status": "success",
            "provider_result": {
                "status": "available",
                "response_status": "available",
                "provider": "gnomAD",
                "source": "gnomad",
                "assembly": "GRCh38",
                "query_identifier": "2-166848215-C-T",
                "variant_id": "wrong-identity",
                "retrieved_at": "2026-08-24T08:00:00Z",
                "populations": [],
            },
            "upstream_version": settings.GNOMAD_DATASET_GRCH38,
            "fallback_for": None,
            "primary_failure": None,
        },
        observation_status="success",
        retrieved_at="2026-08-24T08:00:00Z",
    )
    session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )

    assert len(session.post_calls) == 1
    assert result["status"] == "available"
    assert result["source_mode"] == "live_provider"


def test_repository_failure_fails_open_to_existing_provider_path(
    tmp_path: Path,
) -> None:
    blocking_parent = tmp_path / "not-a-directory"
    blocking_parent.touch()
    repository = EvidenceRepository(blocking_parent / "repository.sqlite3")
    session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )

    assert len(session.post_calls) == 1
    assert result["status"] == "available"
    assert result["source_mode"] == "live_provider"


def test_repository_configuration_failure_keeps_provider_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_repository_path = tmp_path / "repository-directory"
    invalid_repository_path.mkdir()
    monkeypatch.setattr(
        type(settings),
        "EVIDENCE_REPOSITORY_PATH",
        invalid_repository_path,
    )
    session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )

    result = fetch_gnomad_evidence(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        use_repository=True,
    )

    assert len(session.post_calls) == 1
    assert result["status"] == "available"


def test_cached_no_match_remains_no_match(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = fetch_gnomad_evidence(
        _candidate(),
        session=FakeConditionalSession(  # type: ignore[arg-type]
            post_responses=[FakeResponse(200, {"data": {"variant": None}})]
        ),
        evidence_repository=repository,
        use_repository=True,
    )

    cache_session = FakeConditionalSession()
    second = fetch_gnomad_evidence(
        _candidate(),
        session=cache_session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )

    assert first["status"] == "no_match"
    assert second["status"] == "no_match"
    assert second["response_status"] == "no_match"
    assert second["source_mode"] == "repository_cache"
    assert cache_session.post_calls == []
    assert _records(repository, provider="gnomad")[0].observation_status == (
        "no_match"
    )


def test_primary_failure_still_executes_and_stores_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CONDITIONAL_ENRICHMENT_MAX_RETRIES", 0)
    repository = _repository(tmp_path)
    session = FakeConditionalSession(
        post_responses=[requests.Timeout("bounded timeout")],
        get_responses=[FakeResponse(200, _ensembl_payload())],
    )

    result = fetch_population_evidence_with_fallback(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )

    assert result["status"] == "available"
    assert result["provider"] == "Ensembl REST Variation"
    assert result["fallback_used"] is True
    assert result["fallback_for"] == "gnomad"
    assert result["primary_failure"] == "timeout"
    assert _records(repository, provider="gnomad") == []
    fallback_records = _records(
        repository,
        provider="ensembl_variation",
    )
    assert len(fallback_records) == 1
    assert fallback_records[0].provider_role == "fallback"
    assert fallback_records[0].normalized_payload["fallback_for"] == (
        "gnomad"
    )
    assert fallback_records[0].normalized_payload["primary_failure"] == (
        "timeout"
    )
