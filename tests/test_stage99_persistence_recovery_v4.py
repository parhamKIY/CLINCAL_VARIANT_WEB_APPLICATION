"""Stage 99 persistence and recovery V4 acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.variant_interpretation as variant_interpretation
from backend.database import (
    DATABASE_SCHEMA_VERSION,
    DatabaseReadError,
    connect_database,
    initialize_database,
    load_pipeline_state,
    save_pipeline_state,
)
from backend.pipeline import (
    confirm_reviewed_evidence,
    finalize_reviewed_analysis,
    update_draft_variant_report,
)
from backend.report_lifecycle import build_variant_report_records
from backend.variant_report import (
    save_draft_variant_report,
    set_draft_variant_report_inclusion,
)
from tests.test_pipeline import (
    TestStage57PersistenceSchemaV3 as _Stage57Persistence,
)


pytestmark = pytest.mark.stage99_persistence_recovery_v4


def _stored_draft(database_path: Path) -> dict[str, object]:
    draft = _Stage57Persistence._stored_report_draft(database_path)
    draft["variant_report_records"] = build_variant_report_records(
        draft["draft_variant_reports"],
        analysis_id=draft["analysis_id"],
    )
    return save_pipeline_state(draft, database_path=database_path)


def _recovery_row(database_path: Path) -> dict[str, object]:
    connection = connect_database(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM report_recovery_states WHERE variant_index = 0"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return dict(row)


def test_schema_v4_persists_complete_report_first_recovery_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    draft = _stored_draft(database_path)
    record = draft["variant_report_records"][0]  # type: ignore[index]
    report_data = record["report_data"]
    row = _recovery_row(database_path)

    connection = connect_database(database_path)
    try:
        schema_version = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
    finally:
        connection.close()

    assert schema_version == DATABASE_SCHEMA_VERSION == 4
    assert row["analysis_id"] == draft["analysis_id"]
    assert row["variant_id"] == "GRCh38:2:166848215:C:T"
    assert row["report_version"] == record["docx_artifact"]["sha256"][:16]
    assert json.loads(row["report_data_json"]) == report_data
    assert json.loads(row["docx_artifact_json"]) == record["docx_artifact"]
    assert row["template_version"] == report_data["template_version"]
    assert row["interpretation_version"] == report_data["interpretation"][
        "prompt_version"
    ]
    assert json.loads(row["reference_resolution_json"]) == {
        "literature_references": report_data["literature_references"],
        "data_sources": report_data["data_sources"],
    }
    assert row["include_in_final_report"] == 1
    assert row["confirmation_state"] == "draft"
    assert json.loads(row["warning_state_json"]) == report_data["warnings"]


def test_edits_selection_preview_and_artifact_identity_survive_recovery(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    draft = _stored_draft(database_path)
    original_version = _recovery_row(database_path)["report_version"]
    report = draft["draft_variant_reports"][0]  # type: ignore[index]
    reviewed = report["reviewed_report"]
    edited = save_draft_variant_report(
        report,
        reviewer_summary="Stage 99 recovered reviewer summary.",
        interpretation_narrative=reviewed["variant_interpretation"]["narrative"],
        conflict_assessment=reviewed["variant_interpretation"][
            "conflict_assessment"
        ],
        reviewer_notes=["Recovered reviewer note."],
        timestamp="2026-08-11T12:00:00Z",
        reviewer_context="stage99_test",
    )
    edited = set_draft_variant_report_inclusion(
        edited,
        False,
        timestamp="2026-08-11T12:01:00Z",
        reviewer_context="stage99_test",
    )
    updated = update_draft_variant_report(draft, edited)
    saved = save_pipeline_state(updated, database_path=database_path)

    restored = load_pipeline_state(
        saved["analysis_id"],
        database_path=database_path,
    )
    row = _recovery_row(database_path)
    edits = json.loads(row["review_edits_json"])

    assert restored == saved
    assert restored["draft_variant_reports"][0]["reviewed_report"][
        "reviewer_summary"
    ] == "Stage 99 recovered reviewer summary."
    assert restored["draft_variant_reports"][0][
        "include_in_final_report"
    ] is False
    assert row["report_version"] != original_version
    assert row["include_in_final_report"] == 0
    assert edits["report_edit_history"]
    assert edits["selection_history"]
    assert edits["reviewer_notes"] == ["Recovered reviewer note."]


def test_confirmation_and_finalization_state_round_trip(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    draft = _stored_draft(database_path)
    confirmed = confirm_reviewed_evidence(
        draft,
        draft["evidence_review_reports"],
        timestamp="2026-08-11T12:05:00Z",
    )
    save_pipeline_state(confirmed, database_path=database_path)

    confirmed_row = _recovery_row(database_path)
    assert confirmed_row["confirmation_state"] == "confirmed"

    finalized = finalize_reviewed_analysis(
        confirmed,
        timestamp="2026-08-11T12:10:00Z",
    )
    save_pipeline_state(finalized, database_path=database_path)
    restored = load_pipeline_state(
        finalized["analysis_id"],
        database_path=database_path,
    )

    assert restored == finalized
    assert _recovery_row(database_path)["confirmation_state"] == "finalized"


def test_recovery_does_not_regenerate_interpretation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    draft = _stored_draft(database_path)
    expected = draft["variant_interpretation_results"]

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Interpretation must not rerun during recovery.")

    monkeypatch.setattr(
        variant_interpretation,
        "interpret_variant",
        fail_if_called,
    )

    restored = load_pipeline_state(
        draft["analysis_id"],
        database_path=database_path,
    )

    assert restored["variant_interpretation_results"] == expected


def test_schema_v3_migration_backfills_v4_recovery_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    draft = _stored_draft(database_path)
    connection = connect_database(database_path)
    try:
        connection.execute("DROP TABLE report_recovery_states")
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()

    initialize_database(database_path)
    restored = load_pipeline_state(
        draft["analysis_id"],
        database_path=database_path,
    )

    assert restored == draft
    assert _recovery_row(database_path)["variant_id"] == (
        "GRCh38:2:166848215:C:T"
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("template_version", "tampered-template"),
        ("confirmation_state", "confirmed"),
        ("warning_state_json", "[]"),
    ],
)
def test_tampered_recovery_projection_fails_closed(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    draft = _stored_draft(database_path)
    connection = connect_database(database_path)
    try:
        connection.execute(
            f'UPDATE report_recovery_states SET "{column}" = ?',
            (value,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseReadError, match="Stage 99"):
        load_pipeline_state(
            draft["analysis_id"],
            database_path=database_path,
        )
