"""Stage 8 deterministic performance and resource-safety acceptance."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
import tracemalloc

import pytest
import requests

from backend.conditional_enrichment import (
    fetch_gnomad_evidence,
    fetch_population_evidence_with_fallback,
)
from backend.evidence_repository import (
    EvidenceRepository,
    EvidenceRepositoryIntegrityError,
)
from backend.llm import LLMClient
from backend.pipeline import run_analysis
from backend.provider_repository import ProviderRepositoryMetrics
from backend.report_docx import _replace_paragraph_text
from backend.report_lifecycle import _rendered_docx
from config import settings
from test_pipeline import (
    FakeConditionalSession,
    FakeLLMAdapter,
    FakeResponse,
    _variant_interpretation_response,
)
from test_provider_repository_integration import (
    _candidate,
    _ensembl_payload,
    _gnomad_payload,
)
from test_stage4_3_acceptance import _install_offline_boundaries


pytestmark = [
    pytest.mark.regression,
    pytest.mark.stage59_testing_v3,
]


_VARIANT = {
    "chrom": "2",
    "pos": 166_848_215,
    "ref": "C",
    "alt": "T",
}


class _CountingRun:
    def __init__(self, text: str) -> None:
        self._text = text
        self.read_count = 0

    @property
    def text(self) -> str:
        self.read_count += 1
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        self._text = value


class _CountingParagraph:
    def __init__(self, text: str) -> None:
        self.runs = [_CountingRun(text)]

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)

    def add_run(self, text: str) -> _CountingRun:
        run = _CountingRun(text)
        self.runs.append(run)
        return run


class _CountingRepository(EvidenceRepository):
    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.connection_count = 0

    def _connect(self) -> sqlite3.Connection:
        self.connection_count += 1
        return super()._connect()


def _store(repository: EvidenceRepository) -> None:
    repository.store_provider_observation(
        canonical_variant=_VARIANT,
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="gnomad",
        provider_role="primary",
        source="gnomad",
        query_identity={"variant_id": "2-166848215-C-T"},
        normalized_payload={
            "status": "success",
            "provider_result": {"status": "available"},
        },
        observation_status="success",
        retrieved_at=datetime(2026, 8, 24, 8, 30, tzinfo=UTC),
    )


def test_docx_placeholder_scan_reads_each_run_a_bounded_number_of_times() -> None:
    """Catch token-count-multiplied XML traversals during DOCX rendering."""

    paragraph = _CountingParagraph("Result: {{TARGET}}")
    replacements = {
        **{f"{{{{UNUSED_{index}}}}}": str(index) for index in range(100)},
        "{{TARGET}}": "available",
    }

    _replace_paragraph_text(paragraph, replacements)

    assert paragraph.runs[0]._text == "Result: available"
    assert paragraph.runs[0].read_count <= 3


def test_repeated_repository_lookups_use_one_connection_per_operation(
    tmp_path: Path,
) -> None:
    """Catch duplicate initialize-plus-query connections on every cache hit."""

    repository = _CountingRepository(tmp_path / "repository.sqlite3")
    _store(repository)
    repository.connection_count = 0

    first = repository.find_by_canonical_identity(
        canonical_variant=_VARIANT,
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="gnomad",
        query_identity={"variant_id": "2-166848215-C-T"},
    )
    second = repository.find_by_canonical_identity(
        canonical_variant=_VARIANT,
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="gnomad",
        query_identity={"variant_id": "2-166848215-C-T"},
    )

    assert first == second
    assert len(first) == 1
    assert repository.connection_count == 2


def test_repository_schema_validation_remains_active_between_operations(
    tmp_path: Path,
) -> None:
    """Optimization must not let an altered repository schema become trusted."""

    database_path = tmp_path / "repository.sqlite3"
    repository = EvidenceRepository(database_path)
    _store(repository)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE unexpected_table (value TEXT)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        EvidenceRepositoryIntegrityError,
        match="schema is incomplete or unexpected",
    ):
        repository.find_by_canonical_identity(
            canonical_variant=_VARIANT,
            assembly="GRCh38",
        )


def test_schema1_repository_migrates_to_query_identity_lookup_index(
    tmp_path: Path,
) -> None:
    """Catch history scans and temporary sorts on exact provider lookups."""

    database_path = tmp_path / "repository.sqlite3"
    repository = EvidenceRepository(database_path)
    _store(repository)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("DROP INDEX IF EXISTS evidence_repository_lookup_v2")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS evidence_repository_lookup
            ON evidence_repository_records (
                canonical_digest,
                semantic_node,
                provider,
                retrieved_at
            )
            """
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    migrated = EvidenceRepository(database_path)
    migrated.initialize()
    records = migrated.find_by_canonical_identity(
        canonical_variant=_VARIANT,
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="gnomad",
        query_identity={"variant_id": "2-166848215-C-T"},
    )
    connection = sqlite3.connect(database_path)
    try:
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        index_columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(evidence_repository_lookup_v2)"
            )
        ]
        query_plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM evidence_repository_records
            WHERE canonical_digest = ?
              AND semantic_node = ?
              AND provider = ?
              AND query_identity_hash = ?
            ORDER BY retrieved_at, stored_at, record_id
            """,
            (
                records[0].canonical_digest,
                "population_evidence",
                "gnomad",
                records[0].query_identity_hash,
            ),
        ).fetchall()
    finally:
        connection.close()

    assert schema_version == 2
    assert len(records) == 1
    assert index_columns == [
        "canonical_digest",
        "semantic_node",
        "provider",
        "query_identity_hash",
        "retrieved_at",
        "stored_at",
        "record_id",
    ]
    assert all("TEMP B-TREE" not in row[3] for row in query_plan)


def test_concurrent_repository_writes_remain_idempotent(tmp_path: Path) -> None:
    """Concurrent cache fills must retain one immutable provider snapshot."""

    repository = EvidenceRepository(tmp_path / "repository.sqlite3")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: _store(repository), range(16)))

    records = repository.find_by_canonical_identity(
        canonical_variant=_VARIANT,
        assembly="GRCh38",
        semantic_node="population_evidence",
        provider="gnomad",
        query_identity={"variant_id": "2-166848215-C-T"},
    )

    assert len(records) == 1


@pytest.mark.parametrize("variant_count", [1, 5])
def test_offline_pipeline_performance_acceptance_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant_count: int,
) -> None:
    """Measure single/multi-variant execution with every major stage enabled."""

    _install_offline_boundaries(monkeypatch)
    variants = [
        {
            "chrom": str(index + 1),
            "pos": (index + 1) * 100,
            "ref": "A",
            "alt": "G",
            "qual": None,
            "filter": "PASS",
        }
        for index in range(variant_count)
    ]
    _rendered_docx.cache_clear()
    tracemalloc.start()
    started_at = perf_counter()
    try:
        result = run_analysis(
            vcf_path=None,
            manual_variants=variants,
            phenotypes=[],
            llm_client=LLMClient(
                FakeLLMAdapter(
                    _variant_interpretation_response(
                        model="stage8-performance-model",
                        phenotype_conclusion="phenotype evidence unavailable",
                    )
                )
            ),
            database_path=tmp_path / f"clinical-{variant_count}.sqlite3",
        )
        elapsed_seconds = perf_counter() - started_at
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["status"] == "partial"
    assert result["variant_count"] == variant_count
    assert len(result["evidence_objects"]) == variant_count
    assert len(result["variant_interpretation_results"]) == variant_count
    assert len(result["variant_report_records"]) == variant_count
    assert elapsed_seconds < 30
    assert peak_bytes < 64 * 1024 * 1024


def test_repeated_repository_hits_are_bounded_and_do_not_call_provider(
    tmp_path: Path,
) -> None:
    """Measure the repeated-analysis provider-cache path without network work."""

    repository = EvidenceRepository(tmp_path / "repository.sqlite3")
    metrics = ProviderRepositoryMetrics()
    live_session = FakeConditionalSession(
        post_responses=[FakeResponse(200, _gnomad_payload())]
    )
    live = fetch_gnomad_evidence(
        _candidate(),
        session=live_session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
        repository_metrics=metrics,
    )
    cache_session = FakeConditionalSession()

    started_at = perf_counter()
    cached_results = [
        fetch_gnomad_evidence(
            _candidate(),
            session=cache_session,  # type: ignore[arg-type]
            evidence_repository=repository,
            use_repository=True,
            repository_metrics=metrics,
        )
        for _ in range(50)
    ]
    elapsed_seconds = perf_counter() - started_at

    assert len(live_session.post_calls) == 1
    assert cache_session.post_calls == []
    assert all(
        item["status"] == live["status"] == "available"
        for item in cached_results
    )
    assert all(item["source_mode"] == "repository_cache" for item in cached_results)
    assert metrics.snapshot() == {
        "cache_hits": 50,
        "cache_misses": 1,
        "stale_lookups": 0,
        "invalid_records_rejected": 0,
    }
    assert elapsed_seconds < 5


def test_degraded_provider_fallback_performance_remains_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measure one operational failure and validated fallback without retries."""

    monkeypatch.setattr(settings, "CONDITIONAL_ENRICHMENT_MAX_RETRIES", 0)
    repository = EvidenceRepository(tmp_path / "repository.sqlite3")
    session = FakeConditionalSession(
        post_responses=[requests.Timeout("bounded timeout")],
        get_responses=[FakeResponse(200, _ensembl_payload())],
    )

    started_at = perf_counter()
    result = fetch_population_evidence_with_fallback(
        _candidate(),
        session=session,  # type: ignore[arg-type]
        evidence_repository=repository,
        use_repository=True,
    )
    elapsed_seconds = perf_counter() - started_at

    assert result["status"] == "available"
    assert result["provider"] == "Ensembl REST Variation"
    assert result["fallback_used"] is True
    assert result["primary_failure"] == "timeout"
    assert len(session.post_calls) == 1
    assert len(session.get_calls) == 3
    assert elapsed_seconds < 5


def test_repeated_pipeline_runs_do_not_share_analysis_or_variant_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated same-process analyses must remain identity-isolated."""

    _install_offline_boundaries(monkeypatch)

    def execute(variant: dict[str, object], suffix: str) -> dict[str, object]:
        return run_analysis(
            vcf_path=None,
            manual_variants=[variant],
            phenotypes=[],
            llm_client=LLMClient(
                FakeLLMAdapter(
                    _variant_interpretation_response(
                        model="stage8-performance-model",
                        phenotype_conclusion="phenotype evidence unavailable",
                    )
                )
            ),
            database_path=tmp_path / f"clinical-{suffix}.sqlite3",
        )

    first = execute(
        {
            "chrom": "1",
            "pos": 100,
            "ref": "A",
            "alt": "G",
            "qual": None,
            "filter": "PASS",
        },
        "first",
    )
    second = execute(
        {
            "chrom": "2",
            "pos": 200,
            "ref": "C",
            "alt": "T",
            "qual": None,
            "filter": "PASS",
        },
        "second",
    )

    assert first["analysis_id"] != second["analysis_id"]
    assert first["evidence_objects"][0]["variant"] == {
        "chrom": "1",
        "pos": 100,
        "ref": "A",
        "alt": "G",
    }
    assert second["evidence_objects"][0]["variant"] == {
        "chrom": "2",
        "pos": 200,
        "ref": "C",
        "alt": "T",
    }
