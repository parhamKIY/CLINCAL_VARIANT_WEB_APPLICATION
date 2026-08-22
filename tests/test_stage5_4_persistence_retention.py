"""Stage 5.4 persistence-retention safety and lifecycle tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.database import (
    cleanup_analysis_records,
    connect_database,
    initialize_database,
    load_pipeline_state,
    save_complete_analysis,
)
from config import settings
from tests.test_stage99_persistence_recovery_v4 import _stored_draft


def _configure_retention(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    maximum: int = 2,
    preserve_failed: bool = True,
    allow_report_deletion: bool = False,
) -> None:
    monkeypatch.setattr(settings, "ANALYSIS_RETENTION_ENABLED", enabled)
    monkeypatch.setattr(settings, "ANALYSIS_RETENTION_MAX_RECORDS", maximum)
    monkeypatch.setattr(
        settings,
        "ANALYSIS_RETENTION_PRESERVE_FAILED",
        preserve_failed,
    )
    monkeypatch.setattr(
        settings,
        "ANALYSIS_RETENTION_ALLOW_REPORT_DELETION",
        allow_report_deletion,
    )


def _analysis_id(number: int) -> str:
    return f"analysis-{number:032x}"


def _seed_analyses(
    database_path: Path,
    rows: list[tuple[int, str, str]],
) -> None:
    initialize_database(database_path)
    connection = connect_database(database_path)
    try:
        connection.executemany(
            """
            INSERT INTO analyses (
                analysis_id,
                created_at,
                anonymized_filename,
                status,
                warnings_json
            ) VALUES (?, ?, NULL, ?, '[]')
            """,
            [
                (_analysis_id(number), created_at, status)
                for number, created_at, status in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _stored_analysis_ids(database_path: Path) -> list[str]:
    connection = connect_database(database_path)
    try:
        return [
            str(row["analysis_id"])
            for row in connection.execute(
                "SELECT analysis_id FROM analyses ORDER BY analysis_id"
            )
        ]
    finally:
        connection.close()


def _seed_pipeline_states(
    database_path: Path,
    states: list[tuple[int, str]],
) -> None:
    connection = connect_database(database_path)
    try:
        connection.executemany(
            """
            INSERT INTO pipeline_states (
                analysis_id,
                review_state,
                workflow_state,
                pipeline_schema_version,
                pipeline_json,
                updated_at
            ) VALUES (?, 'draft', ?, 'test', '{}', ?)
            """,
            [
                (
                    _analysis_id(number),
                    workflow_state,
                    "2026-01-10T00:00:00Z",
                )
                for number, workflow_state in states
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_disabled_retention_preserves_rows_and_logs_bounded_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    _seed_analyses(
        database_path,
        [
            (1, "2026-01-01T00:00:00Z", "success"),
            (2, "2026-01-02T00:00:00Z", "partial"),
            (3, "2026-01-03T00:00:00Z", "error"),
        ],
    )
    _configure_retention(monkeypatch, enabled=False, maximum=1)
    caplog.set_level(logging.INFO, logger="clinical_variant_app.database")

    result = cleanup_analysis_records(database_path=database_path)

    assert result == {
        "enabled": False,
        "max_records": 1,
        "records_evaluated": 3,
        "records_removed": 0,
        "records_retained": 3,
        "reason": "disabled",
    }
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=analysis_retention_started" in messages
    assert "event=analysis_retention_finished" in messages
    assert "records_evaluated=3 records_removed=0 records_retained=3" in messages
    assert "reason=disabled" in messages
    assert "analysis-" not in messages


def test_automatic_count_retention_respects_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    _configure_retention(monkeypatch, enabled=False)
    records = [
        save_complete_analysis(status="success", database_path=database_path)
        for _ in range(3)
    ]
    connection = connect_database(database_path)
    try:
        connection.executemany(
            """
            INSERT INTO pipeline_states (
                analysis_id,
                review_state,
                workflow_state,
                pipeline_schema_version,
                pipeline_json,
                updated_at
            ) VALUES (?, 'draft', 'completed', 'test', '{}', ?)
            """,
            [
                (record["analysis_id"], "2026-01-10T00:00:00Z")
                for record in records
            ],
        )
        connection.commit()
    finally:
        connection.close()
    _configure_retention(
        monkeypatch,
        enabled=True,
        maximum=2,
        preserve_failed=False,
        allow_report_deletion=True,
    )

    current = _stored_draft(database_path)

    stored = _stored_analysis_ids(database_path)
    assert len(stored) == 2
    assert current["analysis_id"] in stored
    assert len({record["analysis_id"] for record in records} & set(stored)) == 1


def test_cleanup_uses_deterministic_oldest_first_tie_breaking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    _seed_analyses(
        database_path,
        [
            (3, "2026-01-01T00:00:00Z", "success"),
            (1, "2026-01-01T00:00:00Z", "success"),
            (2, "2026-01-01T00:00:00Z", "success"),
        ],
    )
    _seed_pipeline_states(
        database_path,
        [(1, "completed"), (2, "completed"), (3, "completed")],
    )
    _configure_retention(monkeypatch, enabled=True, maximum=1)

    first = cleanup_analysis_records(database_path=database_path)
    second = cleanup_analysis_records(database_path=database_path)

    assert _stored_analysis_ids(database_path) == [_analysis_id(3)]
    assert first["records_removed"] == 2
    assert first["reason"] == "limit_enforced"
    assert second["records_removed"] == 0
    assert second["reason"] == "within_limit"


def test_active_and_policy_protected_failed_analyses_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    _seed_analyses(
        database_path,
        [
            (1, "2026-01-01T00:00:00Z", "pending"),
            (2, "2026-01-02T00:00:00Z", "running"),
            (3, "2026-01-03T00:00:00Z", "success"),
            (4, "2026-01-04T00:00:00Z", "partial"),
            (5, "2026-01-05T00:00:00Z", "success"),
            (6, "2026-01-06T00:00:00Z", "success"),
        ],
    )
    _seed_pipeline_states(
        database_path,
        [
            (3, "analysis_running"),
            (4, "failed"),
            (5, "completed"),
        ],
    )
    _configure_retention(
        monkeypatch,
        enabled=True,
        maximum=1,
        preserve_failed=True,
    )

    protected = cleanup_analysis_records(database_path=database_path)

    assert _stored_analysis_ids(database_path) == [
        _analysis_id(1),
        _analysis_id(2),
        _analysis_id(3),
        _analysis_id(4),
        _analysis_id(6),
    ]
    assert protected["reason"] == "protected_records_exceeded_limit"

    _configure_retention(
        monkeypatch,
        enabled=True,
        maximum=4,
        preserve_failed=False,
    )
    cleanup_analysis_records(database_path=database_path)
    assert _stored_analysis_ids(database_path) == [
        _analysis_id(1),
        _analysis_id(2),
        _analysis_id(3),
        _analysis_id(6),
    ]


def test_final_report_reference_is_preserved_unless_explicitly_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    _seed_analyses(
        database_path,
        [
            (1, "2026-01-01T00:00:00Z", "success"),
            (2, "2026-01-02T00:00:00Z", "success"),
            (3, "2026-01-03T00:00:00Z", "success"),
        ],
    )
    _seed_pipeline_states(
        database_path,
        [(1, "completed"), (2, "completed"), (3, "completed")],
    )
    connection = connect_database(database_path)
    try:
        connection.execute(
            """
            INSERT INTO finalization_states (
                analysis_id,
                confirmation_state,
                confirmed_at,
                selected_variant_ids_json,
                final_report_schema_version,
                final_report_id,
                final_report_generated_at,
                artifact_metadata_json,
                final_report_json,
                updated_at
            ) VALUES (?, 'finalized', ?, '[]', ?, ?, ?, '{}', '{}', ?)
            """,
            (
                _analysis_id(1),
                "2026-01-01T01:00:00Z",
                "1.0",
                "final-report-retention-test",
                "2026-01-01T01:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    _configure_retention(monkeypatch, enabled=True, maximum=1)

    cleanup_analysis_records(database_path=database_path)
    assert _stored_analysis_ids(database_path) == [_analysis_id(1)]

    _seed_analyses(
        database_path,
        [(4, "2026-01-04T00:00:00Z", "success")],
    )
    _configure_retention(
        monkeypatch,
        enabled=True,
        maximum=1,
        allow_report_deletion=True,
    )
    cleanup_analysis_records(database_path=database_path)
    assert _stored_analysis_ids(database_path) == [_analysis_id(4)]


def test_cleanup_preserves_complete_resumable_analysis_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    _configure_retention(monkeypatch, enabled=False)
    draft = _stored_draft(database_path)
    connection = connect_database(database_path)
    try:
        connection.execute(
            "UPDATE analyses SET created_at = ? WHERE analysis_id = ?",
            ("2026-01-03T00:00:00Z", draft["analysis_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    _seed_analyses(
        database_path,
        [
            (1, "2026-01-01T00:00:00Z", "success"),
            (2, "2026-01-02T00:00:00Z", "partial"),
        ],
    )
    _seed_pipeline_states(
        database_path,
        [(1, "completed"), (2, "completed")],
    )
    _configure_retention(monkeypatch, enabled=True, maximum=1)

    cleanup_analysis_records(database_path=database_path)

    assert load_pipeline_state(
        draft["analysis_id"],
        database_path=database_path,
    ) == draft
