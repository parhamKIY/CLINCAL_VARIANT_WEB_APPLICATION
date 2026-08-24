"""Stage 7.2 provenance-first Evidence Repository foundation tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.evidence_repository import (
    EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION,
    EVIDENCE_REPOSITORY_SCHEMA_VERSION,
    EvidenceRepository,
    EvidenceRepositoryIntegrityError,
    EvidenceRepositoryValidationError,
)
from backend.pipeline import run_variant_processing
from backend.variant_integrity import stable_allele_identity
from config import settings


pytestmark = [
    pytest.mark.regression,
    pytest.mark.stage59_testing_v3,
]


CANONICAL_VARIANT = {
    "chrom": "2",
    "pos": 166_848_215,
    "ref": "C",
    "alt": "T",
}
RETRIEVED_AT = datetime(2026, 8, 24, 8, 30, tzinfo=UTC)


def _payload_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _store(
    repository: EvidenceRepository,
    *,
    variant: dict[str, object] | None = None,
    provider: str = "clinvar",
    observation_status: str = "success",
    payload: object | None = None,
):
    return repository.store_provider_observation(
        canonical_variant=variant or CANONICAL_VARIANT,
        assembly="GRCh38",
        semantic_node="clinvar_clinical_evidence",
        provider=provider,
        provider_role="primary",
        source="ncbi_clinvar",
        query_identity={
            "assembly": "GRCh38",
            "variation_id": "12345",
        },
        normalized_payload=(
            {
                "clinical_significance": "Pathogenic",
                "variation_id": "12345",
            }
            if payload is None
            else payload
        ),
        observation_status=observation_status,
        retrieved_at=RETRIEVED_AT,
    )


def test_store_and_retrieve_normalized_provider_observation(
    tmp_path: Path,
) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")

    stored = _store(repository)
    retrieved = repository.find_by_canonical_identity(
        canonical_variant=CANONICAL_VARIANT,
        assembly="GRCh38",
    )

    assert retrieved == [stored]
    assert stored.canonical_digest == stable_allele_identity(
        CANONICAL_VARIANT,
        assembly="GRCh38",
    )
    assert stored.provider == "clinvar"
    assert stored.provider_role == "primary"
    assert stored.source == "ncbi_clinvar"
    assert stored.schema_version == EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION
    assert stored.normalized_payload == {
        "clinical_significance": "Pathogenic",
        "variation_id": "12345",
    }


def test_canonical_identity_isolation(tmp_path: Path) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")
    other_variant = {**CANONICAL_VARIANT, "alt": "G"}
    first = _store(repository)
    second = _store(repository, variant=other_variant)

    assert repository.find_by_canonical_identity(
        canonical_variant=CANONICAL_VARIANT,
        assembly="GRCh38",
    ) == [first]
    assert repository.find_by_canonical_identity(
        canonical_variant=other_variant,
        assembly="GRCh38",
    ) == [second]
    assert first.canonical_digest != second.canonical_digest


def test_provider_isolation(tmp_path: Path) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")
    clinvar = _store(repository, provider="clinvar")
    myvariant = _store(repository, provider="myvariant")

    assert repository.find_by_canonical_identity(
        canonical_variant=CANONICAL_VARIANT,
        assembly="GRCh38",
        provider="clinvar",
    ) == [clinvar]
    assert repository.find_by_canonical_identity(
        canonical_variant=CANONICAL_VARIANT,
        assembly="GRCh38",
        provider="myvariant",
    ) == [myvariant]


def test_payload_sha256_covers_deterministic_normalized_json(
    tmp_path: Path,
) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")
    payload = {"z": [3, 2, 1], "a": {"available": True}}

    stored = _store(repository, payload=payload)

    assert stored.payload_sha256 == hashlib.sha256(
        _payload_bytes(payload)
    ).hexdigest()
    assert len(stored.query_identity_hash) == 64


def test_corrupted_payload_is_rejected_before_retrieval(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "repository.sqlite3"
    repository = EvidenceRepository(database_path)
    stored = _store(repository)
    connection = sqlite3.connect(database_path)
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

    with pytest.raises(
        EvidenceRepositoryIntegrityError,
        match="integrity verification failed",
    ):
        repository.find_by_canonical_identity(
            canonical_variant=CANONICAL_VARIANT,
            assembly="GRCh38",
        )


@pytest.mark.parametrize(
    "failure_status",
    [
        "unavailable",
        "timeout",
        "rate_limited",
        "server_error",
        "network_failure",
    ],
)
def test_operational_failures_are_not_stored(
    tmp_path: Path,
    failure_status: str,
) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")

    with pytest.raises(
        EvidenceRepositoryValidationError,
        match="operational failures cannot be stored",
    ):
        _store(repository, observation_status=failure_status)

    assert repository.find_by_canonical_identity(
        canonical_variant=CANONICAL_VARIANT,
        assembly="GRCh38",
    ) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "timeout"},
        {"status": "rate_limited"},
        {"http_status": 429},
        {"http_status": 503},
    ],
)
def test_operational_failure_payloads_cannot_be_stored_as_success(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")

    with pytest.raises(
        EvidenceRepositoryValidationError,
        match="operational failures cannot be stored",
    ):
        _store(repository, payload=payload)


def test_explicit_provider_no_match_is_preserved(
    tmp_path: Path,
) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")

    stored = _store(
        repository,
        observation_status="no_match",
        payload={"status": "no_match", "matched_record": None},
    )

    assert stored.observation_status == "no_match"
    assert stored.normalized_payload["status"] == "no_match"


@pytest.mark.parametrize(
    "payload",
    [
        {"analysis_id": "analysis-secret"},
        {"patient_hpo_terms": ["HP:0001250"]},
        {"clinical_narrative": "case text"},
        {"final_interpretation": "Pathogenic"},
        {"clinical_report": "report text"},
    ],
)
def test_case_specific_or_conclusion_payloads_are_rejected(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")

    with pytest.raises(
        EvidenceRepositoryValidationError,
        match="case-specific or clinical conclusion",
    ):
        _store(repository, payload=payload)


def test_labeled_phi_inside_normalized_payload_is_rejected(
    tmp_path: Path,
) -> None:
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")

    with pytest.raises(
        EvidenceRepositoryValidationError,
        match="case-specific or clinical conclusion",
    ):
        _store(
            repository,
            payload={"provider_note": "patient_id: MRN-12345"},
        )


def test_repository_schema_is_isolated_from_clinical_persistence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "repository.sqlite3"
    repository = EvidenceRepository(database_path)
    repository.initialize()
    connection = sqlite3.connect(database_path)
    try:
        schema_version = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(evidence_repository_records)"
            )
        }
    finally:
        connection.close()

    assert schema_version == EVIDENCE_REPOSITORY_SCHEMA_VERSION == 1
    assert settings.EVIDENCE_REPOSITORY_PATH != settings.DATABASE_PATH
    assert "analysis_id" not in columns
    assert not {
        "patient_id",
        "clinical_narrative",
        "acmg_conclusion",
        "interpretation",
        "report",
    } & columns


def test_repository_failure_does_not_affect_existing_clinical_workflow(
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

    settings.validate()
    result = run_variant_processing(
        vcf_path=None,
        phenotypes=[],
        manual_variants=[
            {
                "chrom": "2",
                "pos": 166_848_215,
                "ref": "C",
                "alt": "T",
                "qual": None,
                "filter": "PASS",
            }
        ],
    )

    assert result["status"] == "running"
    assert result["current_stage"] == "annotation"
    assert result["variant_count"] == 1
