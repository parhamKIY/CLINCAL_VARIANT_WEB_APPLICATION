"""SQLite persistence foundation for clinical analysis records."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

from backend.report import (
    MAX_CLINICAL_REPORT_TEXT_BYTES,
    MAX_EVIDENCE_ALLELE_LENGTH,
    EvidenceObject,
    EvidenceObjectError,
    sanitize_evidence_object,
)
from config import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    settings,
)

if TYPE_CHECKING:
    from backend.pipeline import PipelineResult


DATABASE_SCHEMA_VERSION = 4
DATABASE_BUSY_TIMEOUT_MS = 5_000
MAX_ANALYSIS_WARNINGS = 100
MAX_ANALYSIS_WARNING_LENGTH = 1_000
MAX_ANALYSIS_WARNINGS_JSON_BYTES = 64 * 1024
MAX_STORED_CANDIDATES = 100
MAX_STORED_EVIDENCE_OBJECTS = 100
MAX_PIPELINE_STATE_JSON_BYTES = 8 * 1024 * 1024
ANALYSIS_STATUSES = frozenset(
    {"pending", "running", "success", "partial", "error"}
)
_ANALYSIS_ID_PATTERN = re.compile(r"analysis-[0-9a-f]{32}")
_CONTROL_CHARACTER_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)
_CANDIDATE_REQUIRED_FIELDS = frozenset(
    {"chrom", "pos", "ref", "alt"}
)
_STORED_CANDIDATE_FIELDS = frozenset(
    {"chrom", "pos", "ref", "alt", "qual", "filter"}
)
_CANDIDATE_ALLOWED_FIELDS = frozenset(
    {
        "chrom",
        "input_index",
        "pos",
        "ref",
        "alt",
        "qual",
        "filter",
        "genotype",
    }
)
_REPORT_FILENAME_PATTERN = re.compile(
    r"clinical-report-[A-Za-z0-9][A-Za-z0-9.-]{0,239}"
    r"\.(?:txt|md)"
)
DATABASE_TABLES = {
    "analyses": (
        "analysis_id",
        "created_at",
        "anonymized_filename",
        "status",
        "warnings_json",
    ),
    "candidate_variants": (
        "analysis_id",
        "candidate_index",
        "variant_json",
    ),
    "evidence_objects": (
        "analysis_id",
        "evidence_index",
        "evidence_json",
    ),
    "reports": (
        "analysis_id",
        "report_path",
    ),
    "pipeline_states": (
        "analysis_id",
        "review_state",
        "workflow_state",
        "pipeline_schema_version",
        "pipeline_json",
        "updated_at",
    ),
    "analysis_contexts": (
        "analysis_id",
        "input_type",
        "accepted_hpo_json",
        "phenotype_extraction_model",
        "variant_interpretation_model",
        "phenotype_extraction_provenance_json",
        "analysis_state",
        "updated_at",
    ),
    "variant_review_states": (
        "analysis_id",
        "variant_index",
        "evidence_json",
        "conflict_json",
        "interpretation_json",
        "machine_original_report_json",
        "reviewed_report_json",
        "edit_history_json",
        "include_in_final_report",
        "selection_history_json",
        "interpretation_failure_json",
        "canonical_references_json",
        "updated_at",
    ),
    "finalization_states": (
        "analysis_id",
        "confirmation_state",
        "confirmed_at",
        "selected_variant_ids_json",
        "final_report_schema_version",
        "final_report_id",
        "final_report_generated_at",
        "artifact_metadata_json",
        "final_report_json",
        "updated_at",
    ),
    "report_recovery_states": (
        "analysis_id",
        "variant_index",
        "variant_id",
        "report_version",
        "report_data_json",
        "docx_artifact_json",
        "template_version",
        "interpretation_version",
        "reference_resolution_json",
        "review_edits_json",
        "include_in_final_report",
        "confirmation_state",
        "warning_state_json",
        "updated_at",
    ),
}

_DATABASE_SCHEMA_V3_TABLES = {
    name: columns
    for name, columns in DATABASE_TABLES.items()
    if name != "report_recovery_states"
}

_DATABASE_SCHEMA_V2_TABLES = {
    name: columns
    for name, columns in _DATABASE_SCHEMA_V3_TABLES.items()
    if name not in {
        "analysis_contexts",
        "variant_review_states",
        "finalization_states",
    }
}

_DATABASE_SCHEMA_SQL = f"""
BEGIN IMMEDIATE;

CREATE TABLE analyses (
    analysis_id TEXT PRIMARY KEY
        CHECK (length(analysis_id) BETWEEN 1 AND 64),
    created_at TEXT NOT NULL
        CHECK (length(created_at) BETWEEN 1 AND 64),
    anonymized_filename TEXT
        CHECK (
            anonymized_filename IS NULL
            OR length(anonymized_filename) BETWEEN 1 AND 255
        ),
    status TEXT NOT NULL
        CHECK (length(status) BETWEEN 1 AND 32),
    warnings_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE candidate_variants (
    analysis_id TEXT NOT NULL,
    candidate_index INTEGER NOT NULL
        CHECK (candidate_index >= 0),
    variant_json TEXT NOT NULL,
    PRIMARY KEY (analysis_id, candidate_index),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE evidence_objects (
    analysis_id TEXT NOT NULL,
    evidence_index INTEGER NOT NULL
        CHECK (evidence_index >= 0),
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (analysis_id, evidence_index),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE reports (
    analysis_id TEXT PRIMARY KEY,
    report_path TEXT NOT NULL
        CHECK (length(report_path) BETWEEN 1 AND 4096),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE pipeline_states (
    analysis_id TEXT PRIMARY KEY,
    review_state TEXT NOT NULL
        CHECK (review_state IN ('draft', 'confirmed')),
    workflow_state TEXT NOT NULL
        CHECK (length(workflow_state) BETWEEN 1 AND 32),
    pipeline_schema_version TEXT NOT NULL
        CHECK (length(pipeline_schema_version) BETWEEN 1 AND 32),
    pipeline_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
        CHECK (length(updated_at) BETWEEN 1 AND 64),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE analysis_contexts (
    analysis_id TEXT PRIMARY KEY,
    input_type TEXT
        CHECK (input_type IN ('vcf', 'vcf_gz', 'excel', 'manual')),
    accepted_hpo_json TEXT NOT NULL,
    phenotype_extraction_model TEXT,
    variant_interpretation_model TEXT,
    phenotype_extraction_provenance_json TEXT,
    analysis_state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE variant_review_states (
    analysis_id TEXT NOT NULL,
    variant_index INTEGER NOT NULL CHECK (variant_index >= 0),
    evidence_json TEXT NOT NULL,
    conflict_json TEXT NOT NULL,
    interpretation_json TEXT NOT NULL,
    machine_original_report_json TEXT NOT NULL,
    reviewed_report_json TEXT NOT NULL,
    edit_history_json TEXT NOT NULL,
    include_in_final_report INTEGER NOT NULL
        CHECK (include_in_final_report IN (0, 1)),
    selection_history_json TEXT NOT NULL,
    interpretation_failure_json TEXT,
    canonical_references_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (analysis_id, variant_index),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE finalization_states (
    analysis_id TEXT PRIMARY KEY,
    confirmation_state TEXT NOT NULL
        CHECK (confirmation_state IN ('draft', 'confirmed', 'finalized')),
    confirmed_at TEXT,
    selected_variant_ids_json TEXT NOT NULL,
    final_report_schema_version TEXT,
    final_report_id TEXT,
    final_report_generated_at TEXT,
    artifact_metadata_json TEXT NOT NULL,
    final_report_json TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE report_recovery_states (
    analysis_id TEXT NOT NULL,
    variant_index INTEGER NOT NULL CHECK (variant_index >= 0),
    variant_id TEXT NOT NULL CHECK (length(variant_id) BETWEEN 1 AND 255),
    report_version TEXT NOT NULL
        CHECK (length(report_version) BETWEEN 16 AND 64),
    report_data_json TEXT NOT NULL,
    docx_artifact_json TEXT NOT NULL,
    template_version TEXT NOT NULL,
    interpretation_version TEXT NOT NULL,
    reference_resolution_json TEXT NOT NULL,
    review_edits_json TEXT NOT NULL,
    include_in_final_report INTEGER NOT NULL
        CHECK (include_in_final_report IN (0, 1)),
    confirmation_state TEXT NOT NULL
        CHECK (confirmation_state IN ('draft', 'confirmed', 'finalized')),
    warning_state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (analysis_id, variant_index),
    UNIQUE (analysis_id, variant_id, report_version),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

PRAGMA user_version = {DATABASE_SCHEMA_VERSION};
COMMIT;
"""

_MIGRATE_SCHEMA_V3_TO_V4_SQL = f"""
BEGIN IMMEDIATE;

CREATE TABLE report_recovery_states (
    analysis_id TEXT NOT NULL,
    variant_index INTEGER NOT NULL CHECK (variant_index >= 0),
    variant_id TEXT NOT NULL CHECK (length(variant_id) BETWEEN 1 AND 255),
    report_version TEXT NOT NULL
        CHECK (length(report_version) BETWEEN 16 AND 64),
    report_data_json TEXT NOT NULL,
    docx_artifact_json TEXT NOT NULL,
    template_version TEXT NOT NULL,
    interpretation_version TEXT NOT NULL,
    reference_resolution_json TEXT NOT NULL,
    review_edits_json TEXT NOT NULL,
    include_in_final_report INTEGER NOT NULL
        CHECK (include_in_final_report IN (0, 1)),
    confirmation_state TEXT NOT NULL
        CHECK (confirmation_state IN ('draft', 'confirmed', 'finalized')),
    warning_state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (analysis_id, variant_index),
    UNIQUE (analysis_id, variant_id, report_version),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

PRAGMA user_version = {DATABASE_SCHEMA_VERSION};
COMMIT;
"""

_MIGRATE_SCHEMA_V1_TO_V2_SQL = """
BEGIN IMMEDIATE;
CREATE TABLE pipeline_states (
    analysis_id TEXT PRIMARY KEY,
    review_state TEXT NOT NULL
        CHECK (review_state IN ('draft', 'confirmed')),
    workflow_state TEXT NOT NULL
        CHECK (length(workflow_state) BETWEEN 1 AND 32),
    pipeline_schema_version TEXT NOT NULL
        CHECK (length(pipeline_schema_version) BETWEEN 1 AND 32),
    pipeline_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
        CHECK (length(updated_at) BETWEEN 1 AND 64),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;
PRAGMA user_version = 2;
COMMIT;
"""

_MIGRATE_SCHEMA_V2_TO_V3_SQL = f"""
BEGIN IMMEDIATE;

CREATE TABLE analysis_contexts (
    analysis_id TEXT PRIMARY KEY,
    input_type TEXT
        CHECK (input_type IN ('vcf', 'vcf_gz', 'excel', 'manual')),
    accepted_hpo_json TEXT NOT NULL,
    phenotype_extraction_model TEXT,
    variant_interpretation_model TEXT,
    phenotype_extraction_provenance_json TEXT,
    analysis_state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE variant_review_states (
    analysis_id TEXT NOT NULL,
    variant_index INTEGER NOT NULL CHECK (variant_index >= 0),
    evidence_json TEXT NOT NULL,
    conflict_json TEXT NOT NULL,
    interpretation_json TEXT NOT NULL,
    machine_original_report_json TEXT NOT NULL,
    reviewed_report_json TEXT NOT NULL,
    edit_history_json TEXT NOT NULL,
    include_in_final_report INTEGER NOT NULL
        CHECK (include_in_final_report IN (0, 1)),
    selection_history_json TEXT NOT NULL,
    interpretation_failure_json TEXT,
    canonical_references_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (analysis_id, variant_index),
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE finalization_states (
    analysis_id TEXT PRIMARY KEY,
    confirmation_state TEXT NOT NULL
        CHECK (confirmation_state IN ('draft', 'confirmed', 'finalized')),
    confirmed_at TEXT,
    selected_variant_ids_json TEXT NOT NULL,
    final_report_schema_version TEXT,
    final_report_id TEXT,
    final_report_generated_at TEXT,
    artifact_metadata_json TEXT NOT NULL,
    final_report_json TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (analysis_id)
        REFERENCES analyses (analysis_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

PRAGMA user_version = 3;
COMMIT;
"""


class DatabaseError(RuntimeError):
    """Base error for the application persistence boundary."""


class DatabaseConfigurationError(DatabaseError):
    """Raised when the configured database path is unusable."""


class DatabaseConnectionError(DatabaseError):
    """Raised when SQLite cannot establish a configured connection."""


class DatabaseInitializationError(DatabaseError):
    """Raised when the database schema cannot be initialized safely."""


class DatabaseValidationError(DatabaseError, ValueError):
    """Raised when analysis metadata is unsafe or malformed."""


class DatabaseWriteError(DatabaseError):
    """Raised when validated analysis metadata cannot be persisted."""


class DatabaseReadError(DatabaseError):
    """Raised when persisted analysis data cannot be read safely."""


class AnalysisNotFoundError(DatabaseReadError):
    """Raised when an analysis identifier has no stored record."""


class AnalysisRecord(TypedDict):
    """Frontend-safe metadata stored for one analysis."""

    analysis_id: str
    created_at: str
    anonymized_filename: str | None
    status: str
    warnings: list[str]


class StoredCandidateVariant(TypedDict):
    """Genotype-free candidate variant approved for persistence."""

    chrom: str
    pos: int
    ref: str
    alt: str
    qual: float | None
    filter: str | None


class StoredAnalysisRecord(AnalysisRecord):
    """Complete validated database record for one analysis."""

    candidates: list[StoredCandidateVariant]
    evidence_objects: list[EvidenceObject]
    report_path: str | None


def _resolve_database_path(
    database_path: str | Path | None,
) -> Path:
    """Resolve and validate one database file path."""

    configured_path = Path(
        database_path
        if database_path is not None
        else settings.DATABASE_PATH
    ).expanduser()
    if configured_path.is_symlink():
        raise DatabaseConfigurationError(
            "The configured database path cannot be a symbolic link."
        )
    resolved_path = configured_path.resolve()
    if resolved_path.exists() and not resolved_path.is_file():
        raise DatabaseConfigurationError(
            "The configured database path must point to a file."
        )
    return resolved_path


def connect_database(
    database_path: str | Path | None = None,
) -> sqlite3.Connection:
    """Open one configured SQLite connection with safe defaults."""

    resolved_path = _resolve_database_path(database_path)
    path_existed = resolved_path.exists()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            resolved_path,
            timeout=DATABASE_BUSY_TIMEOUT_MS / 1_000,
        )
        resolved_path.chmod(PRIVATE_FILE_MODE)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}"
        )
        foreign_keys_enabled = connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        if not path_existed:
            try:
                resolved_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise DatabaseConnectionError(
            "The analysis database could not be opened."
        ) from exc

    if foreign_keys_enabled != 1:
        connection.close()
        raise DatabaseConnectionError(
            "SQLite foreign-key enforcement could not be enabled."
        )
    return connection


def _existing_application_tables(
    connection: sqlite3.Connection,
) -> set[str]:
    """Return non-internal tables already present in the database."""

    return {
        str(row["name"])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            """
        )
    }


def _validate_schema_v1(connection: sqlite3.Connection) -> None:
    """Verify the exact legacy schema before applying its sole migration."""

    legacy_tables = {
        name: columns
        for name, columns in _DATABASE_SCHEMA_V2_TABLES.items()
        if name != "pipeline_states"
    }
    if _existing_application_tables(connection) != set(legacy_tables):
        raise DatabaseInitializationError(
            "The legacy analysis database schema is incomplete or unexpected."
        )
    for table_name, expected_columns in legacy_tables.items():
        actual_columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            )
        )
        if actual_columns != expected_columns:
            raise DatabaseInitializationError(
                "The legacy analysis database schema is incomplete or "
                "unexpected."
            )


def _validate_schema_v2(connection: sqlite3.Connection) -> None:
    """Verify the exact schema-2 layout before the V3 migration."""

    if _existing_application_tables(connection) != set(
        _DATABASE_SCHEMA_V2_TABLES
    ):
        raise DatabaseInitializationError(
            "The schema-2 analysis database is incomplete or unexpected."
        )
    for table_name, expected_columns in _DATABASE_SCHEMA_V2_TABLES.items():
        actual_columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            )
        )
        if actual_columns != expected_columns:
            raise DatabaseInitializationError(
                "The schema-2 analysis database is incomplete or "
                "unexpected."
            )


def _validate_schema_v3(connection: sqlite3.Connection) -> None:
    """Verify the exact schema-3 layout before the V4 migration."""

    if _existing_application_tables(connection) != set(
        _DATABASE_SCHEMA_V3_TABLES
    ):
        raise DatabaseInitializationError(
            "The schema-3 analysis database is incomplete or unexpected."
        )
    for table_name, expected_columns in _DATABASE_SCHEMA_V3_TABLES.items():
        actual_columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            )
        )
        if actual_columns != expected_columns:
            raise DatabaseInitializationError(
                "The schema-3 analysis database is incomplete or "
                "unexpected."
            )


def _validate_schema(connection: sqlite3.Connection) -> None:
    """Verify that the installed version has the expected table columns."""

    existing_tables = _existing_application_tables(connection)
    if existing_tables != set(DATABASE_TABLES):
        raise DatabaseInitializationError(
            "The analysis database schema is incomplete or unexpected."
        )

    for table_name, expected_columns in DATABASE_TABLES.items():
        actual_columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            )
        )
        if actual_columns != expected_columns:
            raise DatabaseInitializationError(
                "The analysis database schema is incomplete or "
                "unexpected."
            )


def initialize_database(
    database_path: str | Path | None = None,
) -> Path:
    """Create or validate the versioned SQLite analysis schema."""

    resolved_path = _resolve_database_path(database_path)
    try:
        resolved_path.parent.mkdir(
            mode=PRIVATE_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        resolved_path.parent.chmod(PRIVATE_DIRECTORY_MODE)
    except OSError as exc:
        raise DatabaseConfigurationError(
            "The database directory could not be prepared."
        ) from exc

    connection = connect_database(resolved_path)
    backfill_v3 = False
    backfill_v4 = False
    try:
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if schema_version > DATABASE_SCHEMA_VERSION:
            raise DatabaseInitializationError(
                "The analysis database was created by a newer "
                "application version."
            )
        if schema_version == 0:
            if _existing_application_tables(connection):
                raise DatabaseInitializationError(
                    "The existing database has no supported schema "
                    "version."
                )
            connection.executescript(_DATABASE_SCHEMA_SQL)
        elif schema_version == 1:
            _validate_schema_v1(connection)
            connection.executescript(_MIGRATE_SCHEMA_V1_TO_V2_SQL)
            _validate_schema_v2(connection)
            connection.executescript(_MIGRATE_SCHEMA_V2_TO_V3_SQL)
            _validate_schema_v3(connection)
            connection.executescript(_MIGRATE_SCHEMA_V3_TO_V4_SQL)
            backfill_v3 = True
            backfill_v4 = True
        elif schema_version == 2:
            _validate_schema_v2(connection)
            connection.executescript(_MIGRATE_SCHEMA_V2_TO_V3_SQL)
            _validate_schema_v3(connection)
            connection.executescript(_MIGRATE_SCHEMA_V3_TO_V4_SQL)
            backfill_v3 = True
            backfill_v4 = True
        elif schema_version == 3:
            _validate_schema_v3(connection)
            connection.executescript(_MIGRATE_SCHEMA_V3_TO_V4_SQL)
            backfill_v4 = True

        _validate_schema(connection)
        if backfill_v3:
            _backfill_v3_projections(connection)
        if backfill_v4:
            _backfill_v4_projections(connection)
        connection.execute("PRAGMA journal_mode = WAL")
    except DatabaseError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise DatabaseInitializationError(
            "The analysis database schema could not be initialized."
        ) from exc
    finally:
        connection.close()

    try:
        resolved_path.chmod(PRIVATE_FILE_MODE)
    except OSError as exc:
        raise DatabaseConfigurationError(
            "The database permissions could not be secured."
        ) from exc
    return resolved_path


def _validate_status(status: str) -> str:
    """Validate one pipeline analysis status."""

    if not isinstance(status, str):
        raise DatabaseValidationError(
            "Analysis status must be a string."
        )
    normalized_status = status.strip().casefold()
    if normalized_status not in ANALYSIS_STATUSES:
        raise DatabaseValidationError(
            "Analysis status must be pending, running, success, "
            "partial, or error."
        )
    return normalized_status


def _validate_analysis_id(analysis_id: str) -> str:
    """Validate an application-generated analysis identifier."""

    if (
        not isinstance(analysis_id, str)
        or _ANALYSIS_ID_PATTERN.fullmatch(analysis_id) is None
    ):
        raise DatabaseValidationError(
            "Analysis ID must use the application-generated format."
        )
    return analysis_id


def _normalize_warnings(warnings: Iterable[str]) -> list[str]:
    """Normalize bounded frontend-safe warnings for JSON storage."""

    if (
        isinstance(warnings, (str, bytes))
        or not isinstance(warnings, Iterable)
    ):
        raise DatabaseValidationError(
            "Analysis warnings must be a collection of strings."
        )

    normalized_warnings: list[str] = []
    for warning in warnings:
        if not isinstance(warning, str):
            raise DatabaseValidationError(
                "Every analysis warning must be a string."
            )
        normalized_warning = " ".join(
            _CONTROL_CHARACTER_PATTERN.sub(" ", warning).split()
        )
        if not normalized_warning:
            raise DatabaseValidationError(
                "Analysis warnings cannot be empty."
            )
        if len(normalized_warning) > MAX_ANALYSIS_WARNING_LENGTH:
            raise DatabaseValidationError(
                "An analysis warning exceeds the storage limit."
            )
        normalized_warnings.append(normalized_warning)
        if len(normalized_warnings) > MAX_ANALYSIS_WARNINGS:
            raise DatabaseValidationError(
                "The analysis contains too many warnings."
            )

    serialized_warnings = json.dumps(
        normalized_warnings,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if (
        len(serialized_warnings.encode("utf-8"))
        > MAX_ANALYSIS_WARNINGS_JSON_BYTES
    ):
        raise DatabaseValidationError(
            "Analysis warnings exceed the storage size limit."
        )
    return normalized_warnings


def _anonymize_filename(
    source_filename: str | None,
    analysis_id: str,
) -> str | None:
    """Replace an uploaded filename with an analysis-scoped alias."""

    if source_filename is None:
        return None
    if not isinstance(source_filename, str) or not source_filename.strip():
        raise DatabaseValidationError(
            "The source filename must be a non-empty string."
        )

    lowered_name = Path(source_filename.strip()).name.casefold()
    if lowered_name.endswith(".vcf.gz"):
        suffix = ".vcf.gz"
    elif lowered_name.endswith(".vcf"):
        suffix = ".vcf"
    else:
        raise DatabaseValidationError(
            "Only .vcf and .vcf.gz source filenames can be stored."
        )
    return f"{analysis_id}{suffix}"


def _prepare_analysis_record(
    *,
    status: str,
    source_filename: str | None,
    warnings: Iterable[str],
) -> tuple[AnalysisRecord, str]:
    """Build validated metadata and its deterministic warning payload."""

    normalized_status = _validate_status(status)
    normalized_warnings = _normalize_warnings(warnings)
    analysis_id = f"analysis-{uuid4().hex}"
    anonymized_filename = _anonymize_filename(
        source_filename,
        analysis_id,
    )
    created_at = (
        datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    warnings_json = json.dumps(
        normalized_warnings,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        {
            "analysis_id": analysis_id,
            "created_at": created_at,
            "anonymized_filename": anonymized_filename,
            "status": normalized_status,
            "warnings": list(normalized_warnings),
        },
        warnings_json,
    )


def _insert_analysis_record(
    connection: sqlite3.Connection,
    record: AnalysisRecord,
    warnings_json: str,
) -> None:
    """Insert one already validated analysis metadata record."""

    connection.execute(
        """
        INSERT INTO analyses (
            analysis_id,
            created_at,
            anonymized_filename,
            status,
            warnings_json
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            record["analysis_id"],
            record["created_at"],
            record["anonymized_filename"],
            record["status"],
            warnings_json,
        ),
    )


def save_analysis(
    *,
    status: str,
    source_filename: str | None = None,
    warnings: Iterable[str] = (),
    database_path: str | Path | None = None,
) -> AnalysisRecord:
    """Create and persist one analysis metadata record."""

    record, warnings_json = _prepare_analysis_record(
        status=status,
        source_filename=source_filename,
        warnings=warnings,
    )

    resolved_path = initialize_database(database_path)
    connection = connect_database(resolved_path)
    try:
        with connection:
            _insert_analysis_record(
                connection,
                record,
                warnings_json,
            )
    except sqlite3.Error as exc:
        raise DatabaseWriteError(
            "The analysis metadata could not be saved."
        ) from exc
    finally:
        connection.close()

    return record


def _bounded_collection(
    values: Iterable[object],
    *,
    name: str,
    maximum: int,
) -> list[object]:
    """Materialize one bounded non-string persistence collection."""

    if (
        isinstance(values, (str, bytes, dict))
        or not isinstance(values, Iterable)
    ):
        raise DatabaseValidationError(
            f"{name} must be a collection."
        )
    materialized = list(islice(values, maximum + 1))
    if len(materialized) > maximum:
        raise DatabaseValidationError(
            f"{name} exceeds the maximum of {maximum}."
        )
    return materialized


def _candidate_text(
    value: object,
    *,
    path: str,
    maximum: int,
) -> str:
    """Validate one bounded candidate text field."""

    if not isinstance(value, str):
        raise DatabaseValidationError(
            f"{path} must be a string."
        )
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or _CONTROL_CHARACTER_PATTERN.search(normalized) is not None
    ):
        raise DatabaseValidationError(
            f"{path} is empty, oversized, or contains control "
            "characters."
        )
    return normalized


def _sanitize_candidate(
    value: object,
    *,
    index: int,
) -> StoredCandidateVariant:
    """Select and validate genotype-free candidate fields."""

    path = f"candidates[{index}]"
    if not isinstance(value, dict):
        raise DatabaseValidationError(
            f"{path} must be a dictionary."
        )
    fields = set(value)
    missing = _CANDIDATE_REQUIRED_FIELDS - fields
    extra = fields - _CANDIDATE_ALLOWED_FIELDS
    if missing:
        raise DatabaseValidationError(
            f"{path} is missing required fields: "
            f"{', '.join(sorted(missing))}."
        )
    if extra:
        raise DatabaseValidationError(
            f"{path} contains unsupported fields: "
            f"{', '.join(sorted(str(field) for field in extra))}."
        )

    input_index = value.get("input_index", index)
    if (
        isinstance(input_index, bool)
        or not isinstance(input_index, int)
        or input_index != index
    ):
        raise DatabaseValidationError(
            f"{path}.input_index must match accepted input order."
        )

    position = value["pos"]
    if (
        isinstance(position, bool)
        or not isinstance(position, int)
        or position <= 0
    ):
        raise DatabaseValidationError(
            f"{path}.pos must be a positive integer."
        )

    quality = value.get("qual")
    if quality is not None and (
        isinstance(quality, bool)
        or not isinstance(quality, (int, float))
        or not math.isfinite(quality)
    ):
        raise DatabaseValidationError(
            f"{path}.qual must be a finite number or null."
        )

    raw_filter = value.get("filter")
    normalized_filter: str | None = None
    if raw_filter is not None:
        normalized_filter = _candidate_text(
            raw_filter,
            path=f"{path}.filter",
            maximum=500,
        )

    return {
        "chrom": _candidate_text(
            value["chrom"],
            path=f"{path}.chrom",
            maximum=32,
        ),
        "pos": position,
        "ref": _candidate_text(
            value["ref"],
            path=f"{path}.ref",
            maximum=MAX_EVIDENCE_ALLELE_LENGTH,
        ).upper(),
        "alt": _candidate_text(
            value["alt"],
            path=f"{path}.alt",
            maximum=MAX_EVIDENCE_ALLELE_LENGTH,
        ).upper(),
        "qual": None if quality is None else float(quality),
        "filter": normalized_filter,
    }


def _require_analysis(
    connection: sqlite3.Connection,
    analysis_id: str,
) -> None:
    """Require an existing parent analysis before child persistence."""

    exists = connection.execute(
        """
        SELECT 1
        FROM analyses
        WHERE analysis_id = ?
        """,
        (analysis_id,),
    ).fetchone()
    if exists is None:
        raise DatabaseWriteError(
            "The parent analysis does not exist."
        )


def _prepare_candidates(
    candidates: Iterable[object],
) -> tuple[list[StoredCandidateVariant], list[str]]:
    """Validate candidates and build deterministic JSON payloads."""

    raw_candidates = _bounded_collection(
        candidates,
        name="Candidate variants",
        maximum=MAX_STORED_CANDIDATES,
    )
    clean_candidates = [
        _sanitize_candidate(candidate, index=index)
        for index, candidate in enumerate(raw_candidates)
    ]
    serialized_candidates = [
        json.dumps(
            candidate,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for candidate in clean_candidates
    ]
    return clean_candidates, serialized_candidates


def save_variants(
    analysis_id: str,
    candidates: Iterable[object],
    *,
    database_path: str | Path | None = None,
) -> int:
    """Persist bounded genotype-free candidate variants atomically."""

    normalized_id = _validate_analysis_id(analysis_id)
    clean_candidates, serialized_candidates = _prepare_candidates(
        candidates
    )

    resolved_path = initialize_database(database_path)
    connection = connect_database(resolved_path)
    try:
        with connection:
            _require_analysis(connection, normalized_id)
            existing_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM candidate_variants
                WHERE analysis_id = ?
                """,
                (normalized_id,),
            ).fetchone()[0]
            if existing_count:
                raise DatabaseWriteError(
                    "Candidate variants have already been saved for "
                    "this analysis."
                )
            connection.executemany(
                """
                INSERT INTO candidate_variants (
                    analysis_id,
                    candidate_index,
                    variant_json
                )
                VALUES (?, ?, ?)
                """,
                (
                    (normalized_id, index, payload)
                    for index, payload in enumerate(
                        serialized_candidates
                    )
                ),
            )
    except DatabaseError:
        raise
    except sqlite3.Error as exc:
        raise DatabaseWriteError(
            "Candidate variants could not be saved."
        ) from exc
    finally:
        connection.close()
    return len(clean_candidates)


def _prepare_evidence_objects(
    evidence_objects: Iterable[object],
) -> tuple[list[EvidenceObject], list[str]]:
    """Validate Evidence Objects and build deterministic JSON payloads."""

    raw_evidence_objects = _bounded_collection(
        evidence_objects,
        name="Evidence Objects",
        maximum=MAX_STORED_EVIDENCE_OBJECTS,
    )
    clean_evidence_objects: list[EvidenceObject] = []
    for index, evidence in enumerate(raw_evidence_objects):
        try:
            clean_evidence_objects.append(
                sanitize_evidence_object(evidence)
            )
        except EvidenceObjectError as exc:
            raise DatabaseValidationError(
                f"Evidence Objects[{index}] is invalid: {exc}"
            ) from exc

    serialized_evidence_objects = [
        json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for evidence in clean_evidence_objects
    ]
    return clean_evidence_objects, serialized_evidence_objects


def save_evidence_objects(
    analysis_id: str,
    evidence_objects: Iterable[object],
    *,
    database_path: str | Path | None = None,
) -> int:
    """Validate, sanitize, and persist Evidence Objects atomically."""

    normalized_id = _validate_analysis_id(analysis_id)
    clean_evidence_objects, serialized_evidence_objects = (
        _prepare_evidence_objects(evidence_objects)
    )

    resolved_path = initialize_database(database_path)
    connection = connect_database(resolved_path)
    try:
        with connection:
            _require_analysis(connection, normalized_id)
            existing_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM evidence_objects
                WHERE analysis_id = ?
                """,
                (normalized_id,),
            ).fetchone()[0]
            if existing_count:
                raise DatabaseWriteError(
                    "Evidence Objects have already been saved for "
                    "this analysis."
                )
            connection.executemany(
                """
                INSERT INTO evidence_objects (
                    analysis_id,
                    evidence_index,
                    evidence_json
                )
                VALUES (?, ?, ?)
                """,
                (
                    (normalized_id, index, payload)
                    for index, payload in enumerate(
                        serialized_evidence_objects
                    )
                ),
            )
    except DatabaseError:
        raise
    except sqlite3.Error as exc:
        raise DatabaseWriteError(
            "Evidence Objects could not be saved."
        ) from exc
    finally:
        connection.close()
    return len(clean_evidence_objects)


def _validate_report_reference(
    report_path: str | Path,
    *,
    report_dir: str | Path | None,
) -> tuple[Path, str]:
    """Validate one generated report and return its safe relative path."""

    if not isinstance(report_path, (str, Path)):
        raise DatabaseValidationError(
            "Report path must be a string or Path."
        )
    if report_dir is not None and not isinstance(
        report_dir,
        (str, Path),
    ):
        raise DatabaseValidationError(
            "Report directory must be a string or Path."
        )
    allowed_directory = Path(
        settings.REPORT_DIR if report_dir is None else report_dir
    ).expanduser()
    try:
        allowed_directory = allowed_directory.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DatabaseValidationError(
            "The configured report directory is unavailable."
        ) from exc
    if not allowed_directory.is_dir():
        raise DatabaseValidationError(
            "The configured report directory is not a directory."
        )

    candidate_path = Path(report_path).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = allowed_directory / candidate_path
    if candidate_path.is_symlink():
        raise DatabaseValidationError(
            "Report references cannot point to symbolic links."
        )
    try:
        resolved_path = candidate_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DatabaseValidationError(
            "The generated report file is unavailable."
        ) from exc
    if (
        not resolved_path.is_file()
        or resolved_path.parent != allowed_directory
        or _REPORT_FILENAME_PATTERN.fullmatch(resolved_path.name)
        is None
    ):
        raise DatabaseValidationError(
            "Report references must point to a generated report "
            "file directly inside the configured report directory."
        )

    try:
        with resolved_path.open("rb") as report_file:
            report_data = report_file.read(
                MAX_CLINICAL_REPORT_TEXT_BYTES + 1
            )
    except OSError as exc:
        raise DatabaseValidationError(
            "The generated report file could not be read."
        ) from exc
    if not report_data:
        raise DatabaseValidationError(
            "The generated report file is empty."
        )
    if len(report_data) > MAX_CLINICAL_REPORT_TEXT_BYTES:
        raise DatabaseValidationError(
            "The generated report file exceeds the storage limit."
        )
    try:
        report_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatabaseValidationError(
            "The generated report file must contain valid UTF-8 text."
        ) from exc

    return resolved_path, resolved_path.name


def save_report(
    analysis_id: str,
    report_path: str | Path,
    *,
    database_path: str | Path | None = None,
    report_dir: str | Path | None = None,
) -> str:
    """Persist one confined relative reference to a generated report."""

    normalized_id = _validate_analysis_id(analysis_id)
    _, stored_reference = _validate_report_reference(
        report_path,
        report_dir=report_dir,
    )

    resolved_database_path = initialize_database(database_path)
    connection = connect_database(resolved_database_path)
    try:
        with connection:
            _require_analysis(connection, normalized_id)
            existing_report = connection.execute(
                """
                SELECT 1
                FROM reports
                WHERE analysis_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            if existing_report is not None:
                raise DatabaseWriteError(
                    "A report has already been saved for this analysis."
                )
            connection.execute(
                """
                INSERT INTO reports (
                    analysis_id,
                    report_path
                )
                VALUES (?, ?)
                """,
                (normalized_id, stored_reference),
            )
    except DatabaseError:
        raise
    except sqlite3.Error as exc:
        raise DatabaseWriteError(
            "The report reference could not be saved."
        ) from exc
    finally:
        connection.close()
    return stored_reference


def save_complete_analysis(
    *,
    status: str,
    source_filename: str | None = None,
    warnings: Iterable[str] = (),
    candidates: Iterable[object] = (),
    evidence_objects: Iterable[object] = (),
    report_path: str | Path | None = None,
    database_path: str | Path | None = None,
    report_dir: str | Path | None = None,
) -> AnalysisRecord:
    """Atomically persist one complete terminal pipeline result."""

    record, warnings_json = _prepare_analysis_record(
        status=status,
        source_filename=source_filename,
        warnings=warnings,
    )
    _, serialized_candidates = _prepare_candidates(candidates)
    _, serialized_evidence_objects = _prepare_evidence_objects(
        evidence_objects
    )
    stored_report_reference: str | None = None
    if report_path is not None:
        _, stored_report_reference = _validate_report_reference(
            report_path,
            report_dir=report_dir,
        )

    resolved_database_path = initialize_database(database_path)
    connection = connect_database(resolved_database_path)
    try:
        with connection:
            _insert_analysis_record(
                connection,
                record,
                warnings_json,
            )
            connection.executemany(
                """
                INSERT INTO candidate_variants (
                    analysis_id,
                    candidate_index,
                    variant_json
                )
                VALUES (?, ?, ?)
                """,
                (
                    (record["analysis_id"], index, payload)
                    for index, payload in enumerate(
                        serialized_candidates
                    )
                ),
            )
            connection.executemany(
                """
                INSERT INTO evidence_objects (
                    analysis_id,
                    evidence_index,
                    evidence_json
                )
                VALUES (?, ?, ?)
                """,
                (
                    (record["analysis_id"], index, payload)
                    for index, payload in enumerate(
                        serialized_evidence_objects
                    )
                ),
            )
            if stored_report_reference is not None:
                connection.execute(
                    """
                    INSERT INTO reports (
                        analysis_id,
                        report_path
                    )
                    VALUES (?, ?)
                    """,
                    (
                        record["analysis_id"],
                        stored_report_reference,
                    ),
                )
    except sqlite3.Error as exc:
        raise DatabaseWriteError(
            "The complete analysis could not be saved."
        ) from exc
    finally:
        connection.close()
    return record


def _derive_review_state(value: "PipelineResult") -> str:
    """Derive review completion from confirmed packages, never UI input."""

    confirmed_indexes = [
        package["variant_index"]
        for package in value["reviewed_evidence_packages"]
    ]
    return (
        "confirmed"
        if value["variant_count"] > 0
        and confirmed_indexes == list(range(value["variant_count"]))
        else "draft"
    )


def _prepare_pipeline_state(
    value: object,
) -> tuple["PipelineResult", str, str]:
    """Validate and serialize one complete resumable pipeline snapshot."""

    from backend.error_handling import PipelineResultError
    from backend.pipeline import validate_pipeline_result

    try:
        validated = validate_pipeline_result(value)
        serialized = json.dumps(
            validated,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (PipelineResultError, TypeError, ValueError) as exc:
        raise DatabaseValidationError(
            "Pipeline state must be a valid JSON-safe pipeline result."
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_PIPELINE_STATE_JSON_BYTES:
        raise DatabaseValidationError(
            "Pipeline state exceeds the storage size limit."
        )
    analysis_id = validated["analysis_id"]
    if analysis_id is None:
        raise DatabaseValidationError(
            "Pipeline state requires a persisted analysis ID."
        )
    _validate_analysis_id(analysis_id)
    return validated, serialized, _derive_review_state(validated)


def _json_text(value: object) -> str:
    """Serialize one validated projection value deterministically."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _v3_projection(
    value: "PipelineResult",
    *,
    updated_at: str,
) -> tuple[tuple[object, ...], list[tuple[object, ...]], tuple[object, ...]]:
    """Derive normalized V3 rows from one validated pipeline snapshot."""

    analysis_id = value["analysis_id"]
    assert analysis_id is not None
    context = value["analysis_context"]
    context_row: tuple[object, ...] = (
        analysis_id,
        context["input_type"],
        _json_text(context["accepted_hpo_terms"]),
        context["phenotype_extraction_model"],
        context["variant_interpretation_model"],
        (
            _json_text(context["phenotype_extraction_provenance"])
            if context["phenotype_extraction_provenance"] is not None
            else None
        ),
        value["workflow_state"],
        updated_at,
    )

    variant_rows: list[tuple[object, ...]] = []
    if len(value["draft_variant_reports"]) == value["variant_count"]:
        for index, report in enumerate(value["draft_variant_reports"]):
            interpretation = value["variant_interpretation_results"][index]
            reviewed = report["reviewed_report"]
            failure = (
                {
                    "status": interpretation["status"],
                    "error_type": interpretation["error_type"],
                    "warnings": interpretation["warnings"],
                }
                if interpretation["status"] == "failed"
                else None
            )
            variant_rows.append(
                (
                    analysis_id,
                    index,
                    _json_text(value["evidence_objects"][index]),
                    _json_text(reviewed["conflict_summary"]),
                    _json_text(interpretation),
                    _json_text(report["machine_original_report"]),
                    _json_text(reviewed),
                    _json_text(report["edit_history"]),
                    int(report["include_in_final_report"]),
                    _json_text(report["selection_history"]),
                    _json_text(failure) if failure is not None else None,
                    _json_text(reviewed["literature_references"]),
                    updated_at,
                )
            )

    packages = value["reviewed_evidence_packages"]
    fully_confirmed = (
        value["variant_count"] > 0
        and [package["variant_index"] for package in packages]
        == list(range(value["variant_count"]))
    )
    final_report = value["final_clinical_report"]
    confirmation_state = (
        "finalized"
        if final_report is not None
        else ("confirmed" if fully_confirmed else "draft")
    )
    confirmed_at = (
        max(str(package["confirmed_at"]) for package in packages)
        if fully_confirmed
        else None
    )
    selected_variant_ids = []
    for report in value["draft_variant_reports"]:
        if not report["include_in_final_report"]:
            continue
        variant_index = report["variant_index"]
        evidence = value["evidence_objects"][variant_index]
        variant = evidence["variant"]
        selected_variant_ids.append(
            f"{evidence['assembly']}:{variant['chrom']}:"
            f"{variant['pos']}:{variant['ref']}:{variant['alt']}"
        )
    artifact_metadata = {
        "available_formats": (
            ["text", "pdf", "docx"]
            if final_report is not None
            else []
        ),
        "delivery": "in_memory",
        "persisted_file_paths": [],
    }
    finalization_row: tuple[object, ...] = (
        analysis_id,
        confirmation_state,
        confirmed_at,
        _json_text(selected_variant_ids),
        (
            final_report["schema_version"]
            if final_report is not None
            else None
        ),
        final_report["report_id"] if final_report is not None else None,
        (
            final_report["generated_at"]
            if final_report is not None
            else None
        ),
        _json_text(artifact_metadata),
        _json_text(final_report) if final_report is not None else None,
        updated_at,
    )
    return context_row, variant_rows, finalization_row


def _report_variant_id(report_data: dict[str, object]) -> str:
    """Return the assembly-qualified allele identity persisted for artifacts."""

    identity = report_data["variant_identity"]
    assert isinstance(identity, dict)
    return (
        f"{identity['genome_build']}:{identity['chromosome']}:"
        f"{identity['position']}:{identity['reference']}:"
        f"{identity['alternate']}"
    )


def _v4_recovery_projection(
    value: "PipelineResult",
    *,
    updated_at: str,
) -> list[tuple[object, ...]]:
    """Derive normalized Stage 99 report-first recovery rows."""

    analysis_id = value["analysis_id"]
    assert analysis_id is not None
    records = value["variant_report_records"]
    drafts = value["draft_variant_reports"]
    if not records:
        return []
    rows: list[tuple[object, ...]] = []
    for index, record in enumerate(records):
        report_data = record["report_data"]
        artifact = record["docx_artifact"]
        draft = drafts[index]
        reference_resolution = {
            "literature_references": report_data["literature_references"],
            "data_sources": report_data["data_sources"],
        }
        review_edits = {
            "report_edit_history": draft["edit_history"],
            "selection_history": draft["selection_history"],
            "reviewer_summary": report_data["review_state"][
                "reviewer_summary"
            ],
            "reviewer_notes": report_data["review_state"]["reviewer_notes"],
        }
        rows.append(
            (
                analysis_id,
                index,
                _report_variant_id(report_data),
                artifact["sha256"][:16],
                _json_text(report_data),
                _json_text(artifact),
                report_data["template_version"],
                report_data["interpretation"]["prompt_version"],
                _json_text(reference_resolution),
                _json_text(review_edits),
                int(report_data["review_state"]["include_in_final_report"]),
                record["lifecycle_state"],
                _json_text(report_data["warnings"]),
                updated_at,
            )
        )
    return rows


def _write_v4_recovery_projection(
    connection: sqlite3.Connection,
    value: "PipelineResult",
    *,
    updated_at: str,
) -> None:
    """Replace one analysis's V4 report recovery projection atomically."""

    analysis_id = value["analysis_id"]
    assert analysis_id is not None
    rows = _v4_recovery_projection(value, updated_at=updated_at)
    connection.execute(
        "DELETE FROM report_recovery_states WHERE analysis_id = ?",
        (analysis_id,),
    )
    if rows:
        connection.executemany(
            """
            INSERT INTO report_recovery_states (
                analysis_id, variant_index, variant_id, report_version,
                report_data_json, docx_artifact_json, template_version,
                interpretation_version, reference_resolution_json,
                review_edits_json, include_in_final_report,
                confirmation_state, warning_state_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _validate_v4_recovery_projection(
    connection: sqlite3.Connection,
    value: "PipelineResult",
    *,
    updated_at: str,
) -> None:
    """Require Stage 99 recovery rows to match the canonical snapshot."""

    analysis_id = value["analysis_id"]
    assert analysis_id is not None
    expected = _v4_recovery_projection(value, updated_at=updated_at)
    actual = connection.execute(
        """
        SELECT * FROM report_recovery_states
        WHERE analysis_id = ? ORDER BY variant_index
        """,
        (analysis_id,),
    ).fetchall()
    if [tuple(row) for row in actual] != expected:
        raise DatabaseReadError(
            "The stored Stage 99 report recovery projection is invalid."
        )


def _write_v3_projection(
    connection: sqlite3.Connection,
    value: "PipelineResult",
    *,
    updated_at: str,
) -> None:
    """Replace one analysis's normalized V3 projection atomically."""

    context_row, variant_rows, finalization_row = _v3_projection(
        value,
        updated_at=updated_at,
    )
    analysis_id = value["analysis_id"]
    assert analysis_id is not None
    connection.execute(
        """
        INSERT INTO analysis_contexts (
            analysis_id, input_type, accepted_hpo_json,
            phenotype_extraction_model, variant_interpretation_model,
            phenotype_extraction_provenance_json, analysis_state, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (analysis_id) DO UPDATE SET
            input_type = excluded.input_type,
            accepted_hpo_json = excluded.accepted_hpo_json,
            phenotype_extraction_model = excluded.phenotype_extraction_model,
            variant_interpretation_model = excluded.variant_interpretation_model,
            phenotype_extraction_provenance_json = excluded.phenotype_extraction_provenance_json,
            analysis_state = excluded.analysis_state,
            updated_at = excluded.updated_at
        """,
        context_row,
    )
    connection.execute(
        "DELETE FROM variant_review_states WHERE analysis_id = ?",
        (analysis_id,),
    )
    if variant_rows:
        connection.executemany(
            """
            INSERT INTO variant_review_states (
                analysis_id, variant_index, evidence_json, conflict_json,
                interpretation_json, machine_original_report_json,
                reviewed_report_json, edit_history_json,
                include_in_final_report, selection_history_json,
                interpretation_failure_json, canonical_references_json,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            variant_rows,
        )
    connection.execute(
        """
        INSERT INTO finalization_states (
            analysis_id, confirmation_state, confirmed_at,
            selected_variant_ids_json, final_report_schema_version,
            final_report_id, final_report_generated_at,
            artifact_metadata_json, final_report_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (analysis_id) DO UPDATE SET
            confirmation_state = excluded.confirmation_state,
            confirmed_at = excluded.confirmed_at,
            selected_variant_ids_json = excluded.selected_variant_ids_json,
            final_report_schema_version = excluded.final_report_schema_version,
            final_report_id = excluded.final_report_id,
            final_report_generated_at = excluded.final_report_generated_at,
            artifact_metadata_json = excluded.artifact_metadata_json,
            final_report_json = excluded.final_report_json,
            updated_at = excluded.updated_at
        """,
        finalization_row,
    )


def _validate_v3_projection(
    connection: sqlite3.Connection,
    value: "PipelineResult",
    *,
    updated_at: str,
) -> None:
    """Require normalized V3 rows to match the canonical pipeline JSON."""

    expected_context, expected_variants, expected_finalization = (
        _v3_projection(value, updated_at=updated_at)
    )
    analysis_id = value["analysis_id"]
    assert analysis_id is not None
    context_row = connection.execute(
        "SELECT * FROM analysis_contexts WHERE analysis_id = ?",
        (analysis_id,),
    ).fetchone()
    variant_rows = connection.execute(
        """
        SELECT * FROM variant_review_states
        WHERE analysis_id = ? ORDER BY variant_index
        """,
        (analysis_id,),
    ).fetchall()
    finalization_row = connection.execute(
        "SELECT * FROM finalization_states WHERE analysis_id = ?",
        (analysis_id,),
    ).fetchone()
    if (
        context_row is None
        or tuple(context_row) != expected_context
        or [tuple(row) for row in variant_rows] != expected_variants
        or finalization_row is None
        or tuple(finalization_row) != expected_finalization
    ):
        raise DatabaseReadError(
            "The stored Stage 57 persistence projection is invalid."
        )


def _bounded_stage56_pipeline_migration(
    raw: object,
) -> "PipelineResult" | None:
    """Upgrade only the Stage 56 report lifecycle; never reinterpret Output A/B."""

    from backend.final_clinical_report import (
        FinalClinicalReportError,
        compose_final_clinical_report,
    )
    from backend.pipeline import (
        PIPELINE_SCHEMA_VERSION,
        migrate_pipeline_schema32_to33,
        migrate_pipeline_schema33_to34,
        validate_pipeline_result,
    )
    from backend.evidence_readiness import (
        EvidenceReadinessError,
        build_evidence_readiness_audit,
    )
    from backend.report_lifecycle import (
        ReportLifecycleError,
        build_variant_report_records,
    )
    from backend.variant_integrity import (
        VariantIntegrityError,
        build_variant_integrity_records,
        index_input_variants,
    )

    if not isinstance(raw, dict):
        return None
    candidate = dict(raw)
    if candidate.get("schema_version") == PIPELINE_SCHEMA_VERSION:
        try:
            return validate_pipeline_result(candidate)
        except (TypeError, ValueError):
            return None
    source_version = candidate.get("schema_version")
    if source_version == "3.3":
        return migrate_pipeline_schema33_to34(candidate)
    if source_version == "3.2":
        return migrate_pipeline_schema32_to33(candidate)
    if source_version == "3.1":
        evidence_objects = candidate.get("evidence_objects")
        if not isinstance(evidence_objects, list) or not all(
            isinstance(item, dict) for item in evidence_objects
        ):
            return None
        try:
            readiness = []
            for index, evidence in enumerate(evidence_objects):
                before = build_evidence_readiness_audit(
                    evidence,
                    variant_index=index,
                )
                readiness.append(
                    build_evidence_readiness_audit(
                        evidence,
                        variant_index=index,
                        before_rescue=before,
                    )
                )
        except EvidenceReadinessError:
            return None
        candidate["evidence_readiness"] = readiness
        candidate["schema_version"] = "3.2"
        return migrate_pipeline_schema32_to33(candidate)
    if source_version not in {"2.8", "2.9", "3.0"}:
        return None
    variant_count = candidate.get("variant_count")
    reports = candidate.get("draft_variant_reports")
    interpretations = candidate.get("variant_interpretation_results")
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or not isinstance(reports, list)
        or len(reports) != variant_count
        or not isinstance(interpretations, list)
        or len(interpretations) != variant_count
    ):
        return None
    if source_version == "2.8":
        accepted_hpo_terms: list[str] = []
        if reports:
            reviewed = reports[0].get("reviewed_report")
            phenotype = (
                reviewed.get("phenotype_context")
                if isinstance(reviewed, dict)
                else None
            )
            accepted = (
                phenotype.get("accepted_hpo_terms")
                if isinstance(phenotype, dict)
                else None
            )
            if isinstance(accepted, list) and all(
                isinstance(item, str) for item in accepted
            ):
                accepted_hpo_terms = list(accepted)
        interpretation_model = None
        if interpretations and isinstance(interpretations[0], dict):
            model = interpretations[0].get("configured_model")
            if isinstance(model, str) and model.strip():
                interpretation_model = model
        candidate["analysis_context"] = {
            "input_type": None,
            "accepted_hpo_terms": accepted_hpo_terms,
            "phenotype_extraction_model": None,
            "variant_interpretation_model": interpretation_model,
            "phenotype_extraction_provenance": None,
        }
    candidate["schema_version"] = "3.2"
    raw_variants = candidate.get("variants")
    evidence_objects = candidate.get("evidence_objects")
    if not isinstance(raw_variants, list) or not all(
        isinstance(item, dict) for item in raw_variants
    ):
        return None
    try:
        candidate["variants"] = index_input_variants(raw_variants)
    except VariantIntegrityError:
        return None
    final_report = candidate.get("final_clinical_report")
    generated_at = (
        final_report.get("generated_at")
        if isinstance(final_report, dict)
        else None
    )
    packages = candidate.get("reviewed_evidence_packages")
    try:
        candidate["variant_report_records"] = build_variant_report_records(
            reports,
            analysis_id=candidate.get("analysis_id"),
            confirmed_packages=(packages if isinstance(packages, list) else []),
            finalized_at=(
                generated_at
                if candidate.get("workflow_state") == "completed"
                and final_report is not None
                else None
            ),
        )
    except (ReportLifecycleError, TypeError, ValueError):
        return None
    try:
        candidate["variant_integrity_records"] = (
            build_variant_integrity_records(
                candidate["variants"],
                candidate["variants"],
                assembly=(
                    evidence_objects[0]["assembly"]
                    if isinstance(evidence_objects, list)
                    and evidence_objects
                    and isinstance(evidence_objects[0], dict)
                    else settings.GENOME_ASSEMBLY
                ),
                evidence_objects=(
                    evidence_objects
                    if isinstance(evidence_objects, list)
                    else []
                ),
                draft_reports=reports,
                review_records=candidate["variant_report_records"],
            )
        )
    except (VariantIntegrityError, TypeError, ValueError):
        return None
    if final_report is not None:
        candidate["final_clinical_report"] = None
        try:
            candidate["final_clinical_report"] = dict(
                compose_final_clinical_report(
                    candidate,
                    timestamp=generated_at,
                )
            )
        except FinalClinicalReportError:
            return None
    if "evidence_readiness" not in candidate:
        if not isinstance(evidence_objects, list):
            return None
        try:
            candidate["evidence_readiness"] = []
            for index, evidence in enumerate(evidence_objects):
                if not isinstance(evidence, dict):
                    return None
                before = build_evidence_readiness_audit(
                    evidence,
                    variant_index=index,
                )
                candidate["evidence_readiness"].append(
                    build_evidence_readiness_audit(
                        evidence,
                        variant_index=index,
                        before_rescue=before,
                    )
                )
        except EvidenceReadinessError:
            return None
    return migrate_pipeline_schema32_to33(candidate)


def _backfill_v3_projections(connection: sqlite3.Connection) -> None:
    """Materialize valid Stage 56 snapshots during the bounded V2-to-V3 migration."""

    rows = connection.execute(
        """
        SELECT analysis_id, pipeline_json, updated_at
        FROM pipeline_states ORDER BY analysis_id
        """
    ).fetchall()
    with connection:
        for row in rows:
            raw_json = row["pipeline_json"]
            if not isinstance(raw_json, str):
                continue
            try:
                raw = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            migrated = _bounded_stage56_pipeline_migration(raw)
            if migrated is None:
                continue
            serialized = _json_text(migrated)
            connection.execute(
                """
                UPDATE pipeline_states
                SET pipeline_schema_version = ?, pipeline_json = ?
                WHERE analysis_id = ?
                """,
                (
                    migrated["schema_version"],
                    serialized,
                    row["analysis_id"],
                ),
            )
            _write_v3_projection(
                connection,
                migrated,
                updated_at=row["updated_at"],
            )


def _backfill_v4_projections(connection: sqlite3.Connection) -> None:
    """Materialize valid report recovery rows during the V3-to-V4 migration."""

    rows = connection.execute(
        """
        SELECT pipeline_json, updated_at
        FROM pipeline_states ORDER BY analysis_id
        """
    ).fetchall()
    with connection:
        for row in rows:
            raw_json = row["pipeline_json"]
            if not isinstance(raw_json, str):
                continue
            try:
                raw = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            migrated = _bounded_stage56_pipeline_migration(raw)
            if migrated is None:
                continue
            _write_v4_recovery_projection(
                connection,
                migrated,
                updated_at=row["updated_at"],
            )


def save_pipeline_state(
    value: object,
    *,
    database_path: str | Path | None = None,
) -> "PipelineResult":
    """Upsert a validated Draft or Confirmed pipeline state for later use."""

    validated, serialized, review_state = _prepare_pipeline_state(value)
    analysis_id = validated["analysis_id"]
    assert analysis_id is not None
    normalized_status = _validate_status(validated["status"])
    normalized_warnings = _normalize_warnings(validated["warnings"])
    warnings_json = json.dumps(
        normalized_warnings,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    updated_at = (
        datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    resolved_database_path = initialize_database(database_path)
    connection = connect_database(resolved_database_path)
    try:
        with connection:
            exists = connection.execute(
                "SELECT 1 FROM analyses WHERE analysis_id = ?",
                (analysis_id,),
            ).fetchone()
            if exists is None:
                raise AnalysisNotFoundError(
                    "The pipeline state requires an existing analysis."
                )
            connection.execute(
                """
                INSERT INTO pipeline_states (
                    analysis_id,
                    review_state,
                    workflow_state,
                    pipeline_schema_version,
                    pipeline_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (analysis_id) DO UPDATE SET
                    review_state = excluded.review_state,
                    workflow_state = excluded.workflow_state,
                    pipeline_schema_version = excluded.pipeline_schema_version,
                    pipeline_json = excluded.pipeline_json,
                    updated_at = excluded.updated_at
                """,
                (
                    analysis_id,
                    review_state,
                    validated["workflow_state"],
                    validated["schema_version"],
                    serialized,
                    updated_at,
                ),
            )
            _write_v3_projection(
                connection,
                validated,
                updated_at=updated_at,
            )
            _write_v4_recovery_projection(
                connection,
                validated,
                updated_at=updated_at,
            )
            connection.execute(
                """
                UPDATE analyses
                SET status = ?, warnings_json = ?
                WHERE analysis_id = ?
                """,
                (normalized_status, warnings_json, analysis_id),
            )
    except DatabaseError:
        raise
    except sqlite3.Error as exc:
        raise DatabaseWriteError(
            "The pipeline state could not be saved."
        ) from exc
    finally:
        connection.close()
    return validated


def load_pipeline_state(
    analysis_id: str,
    *,
    database_path: str | Path | None = None,
) -> "PipelineResult":
    """Load and revalidate one resumable pipeline snapshot."""

    from backend.error_handling import PipelineResultError
    from backend.pipeline import (
        PIPELINE_SCHEMA_VERSION,
        validate_pipeline_result,
    )

    normalized_id = _validate_analysis_id(analysis_id)
    resolved_database_path = initialize_database(database_path)
    connection = connect_database(resolved_database_path)
    try:
        row = connection.execute(
            """
            SELECT
                review_state,
                workflow_state,
                pipeline_schema_version,
                pipeline_json,
                updated_at
            FROM pipeline_states
            WHERE analysis_id = ?
            """,
            (normalized_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseReadError(
            "The stored pipeline state could not be read."
        ) from exc
    finally:
        connection.close()
    if row is None:
        raise AnalysisNotFoundError(
            "The requested analysis has no saved pipeline state."
        )
    raw_json = row["pipeline_json"]
    if (
        not isinstance(raw_json, str)
        or len(raw_json.encode("utf-8")) > MAX_PIPELINE_STATE_JSON_BYTES
    ):
        raise DatabaseReadError("The stored pipeline state is invalid.")
    migrated_from_legacy_schema = False
    try:
        raw = json.loads(raw_json)
        if isinstance(raw, dict) and raw.get("schema_version") in {"3.1", "3.2", "3.3"}:
            migrated = _bounded_stage56_pipeline_migration(raw)
            if migrated is None:
                raise DatabaseReadError(
                    "The stored pipeline state is invalid."
                )
            raw = migrated
            migrated_from_legacy_schema = True
        if (
            isinstance(raw, dict)
            and raw.get("schema_version") != PIPELINE_SCHEMA_VERSION
        ):
            raise DatabaseReadError(
                "This legacy Stage 44 analysis cannot be resumed by the "
                "Stage 57 report workflow."
            )
        validated = validate_pipeline_result(raw)
    except DatabaseReadError:
        raise
    except (
        json.JSONDecodeError,
        PipelineResultError,
        TypeError,
        ValueError,
    ) as exc:
        raise DatabaseReadError(
            "The stored pipeline state is invalid."
        ) from exc
    if (
        validated["analysis_id"] != normalized_id
        or validated["workflow_state"] != row["workflow_state"]
        or (
            validated["schema_version"] != row["pipeline_schema_version"]
            and not (
                migrated_from_legacy_schema
                and row["pipeline_schema_version"] in {"3.1", "3.2", "3.3"}
            )
        )
        or _derive_review_state(validated) != row["review_state"]
    ):
        raise DatabaseReadError("The stored pipeline state is invalid.")
    projection_connection = connect_database(resolved_database_path)
    try:
        _validate_v3_projection(
            projection_connection,
            validated,
            updated_at=row["updated_at"],
        )
        _validate_v4_recovery_projection(
            projection_connection,
            validated,
            updated_at=row["updated_at"],
        )
    except sqlite3.Error as exc:
        raise DatabaseReadError(
            "The stored Stage 57 persistence projection could not be read."
        ) from exc
    finally:
        projection_connection.close()
    return validated


def _restore_analysis_metadata(
    row: sqlite3.Row,
) -> AnalysisRecord:
    """Validate metadata loaded from the database without normalizing it."""

    analysis_id = _validate_analysis_id(row["analysis_id"])
    created_at = row["created_at"]
    if not isinstance(created_at, str):
        raise DatabaseReadError(
            "Stored analysis metadata is invalid."
        )
    try:
        parsed_created_at = datetime.strptime(
            created_at,
            "%Y-%m-%dT%H:%M:%SZ",
        )
    except ValueError as exc:
        raise DatabaseReadError(
            "Stored analysis metadata is invalid."
        ) from exc
    if (
        parsed_created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        != created_at
    ):
        raise DatabaseReadError(
            "Stored analysis metadata is invalid."
        )

    stored_status = row["status"]
    try:
        normalized_status = _validate_status(stored_status)
    except DatabaseValidationError as exc:
        raise DatabaseReadError(
            "Stored analysis metadata is invalid."
        ) from exc
    if normalized_status != stored_status:
        raise DatabaseReadError(
            "Stored analysis metadata is invalid."
        )

    anonymized_filename = row["anonymized_filename"]
    approved_filenames = {
        f"{analysis_id}.vcf",
        f"{analysis_id}.vcf.gz",
    }
    if (
        anonymized_filename is not None
        and anonymized_filename not in approved_filenames
    ):
        raise DatabaseReadError(
            "Stored analysis metadata is invalid."
        )

    try:
        raw_warnings = json.loads(row["warnings_json"])
        restored_warnings = _normalize_warnings(raw_warnings)
    except (
        json.JSONDecodeError,
        TypeError,
        DatabaseValidationError,
    ) as exc:
        raise DatabaseReadError(
            "Stored analysis warnings are invalid."
        ) from exc
    if restored_warnings != raw_warnings:
        raise DatabaseReadError(
            "Stored analysis warnings are invalid."
        )

    return {
        "analysis_id": analysis_id,
        "created_at": created_at,
        "anonymized_filename": anonymized_filename,
        "status": normalized_status,
        "warnings": restored_warnings,
    }


def _restore_candidates(
    rows: list[sqlite3.Row],
) -> list[StoredCandidateVariant]:
    """Restore ordered genotype-free candidates and reject corruption."""

    if len(rows) > MAX_STORED_CANDIDATES:
        raise DatabaseReadError(
            "Stored candidate variants exceed the retrieval limit."
        )
    restored: list[StoredCandidateVariant] = []
    for expected_index, row in enumerate(rows):
        if row["candidate_index"] != expected_index:
            raise DatabaseReadError(
                "Stored candidate variant order is invalid."
            )
        try:
            raw_candidate = json.loads(row["variant_json"])
            if (
                not isinstance(raw_candidate, dict)
                or set(raw_candidate) != _STORED_CANDIDATE_FIELDS
            ):
                raise DatabaseValidationError(
                    "Stored candidate fields are invalid."
                )
            clean_candidate = _sanitize_candidate(
                raw_candidate,
                index=expected_index,
            )
        except (
            json.JSONDecodeError,
            TypeError,
            DatabaseValidationError,
        ) as exc:
            raise DatabaseReadError(
                "A stored candidate variant is invalid."
            ) from exc
        if clean_candidate != raw_candidate:
            raise DatabaseReadError(
                "A stored candidate variant is invalid."
            )
        restored.append(clean_candidate)
    return restored


def _restore_evidence_objects(
    rows: list[sqlite3.Row],
) -> list[EvidenceObject]:
    """Restore ordered Evidence Objects through the Stage 7 boundary."""

    if len(rows) > MAX_STORED_EVIDENCE_OBJECTS:
        raise DatabaseReadError(
            "Stored Evidence Objects exceed the retrieval limit."
        )
    restored: list[EvidenceObject] = []
    for expected_index, row in enumerate(rows):
        if row["evidence_index"] != expected_index:
            raise DatabaseReadError(
                "Stored Evidence Object order is invalid."
            )
        try:
            raw_evidence = json.loads(row["evidence_json"])
            clean_evidence = sanitize_evidence_object(raw_evidence)
        except (
            json.JSONDecodeError,
            TypeError,
            EvidenceObjectError,
        ) as exc:
            raise DatabaseReadError(
                "A stored Evidence Object is invalid."
            ) from exc
        if clean_evidence != raw_evidence:
            raise DatabaseReadError(
                "A stored Evidence Object is invalid."
            )
        restored.append(clean_evidence)
    return restored


def get_analysis(
    analysis_id: str,
    *,
    database_path: str | Path | None = None,
    report_dir: str | Path | None = None,
) -> StoredAnalysisRecord:
    """Retrieve and revalidate one complete persisted analysis."""

    normalized_id = _validate_analysis_id(analysis_id)
    resolved_database_path = initialize_database(database_path)
    connection = connect_database(resolved_database_path)
    try:
        metadata_row = connection.execute(
            """
            SELECT
                analysis_id,
                created_at,
                anonymized_filename,
                status,
                warnings_json
            FROM analyses
            WHERE analysis_id = ?
            """,
            (normalized_id,),
        ).fetchone()
        if metadata_row is None:
            raise AnalysisNotFoundError(
                "The requested analysis was not found."
            )
        candidate_rows = list(
            connection.execute(
                """
                SELECT candidate_index, variant_json
                FROM candidate_variants
                WHERE analysis_id = ?
                ORDER BY candidate_index
                LIMIT ?
                """,
                (normalized_id, MAX_STORED_CANDIDATES + 1),
            )
        )
        evidence_rows = list(
            connection.execute(
                """
                SELECT evidence_index, evidence_json
                FROM evidence_objects
                WHERE analysis_id = ?
                ORDER BY evidence_index
                LIMIT ?
                """,
                (normalized_id, MAX_STORED_EVIDENCE_OBJECTS + 1),
            )
        )
        report_row = connection.execute(
            """
            SELECT report_path
            FROM reports
            WHERE analysis_id = ?
            """,
            (normalized_id,),
        ).fetchone()
    except DatabaseError:
        raise
    except sqlite3.Error as exc:
        raise DatabaseReadError(
            "The stored analysis could not be read."
        ) from exc
    finally:
        connection.close()

    metadata = _restore_analysis_metadata(metadata_row)
    candidates = _restore_candidates(candidate_rows)
    evidence_objects = _restore_evidence_objects(evidence_rows)

    restored_report_path: str | None = None
    if report_row is not None:
        try:
            resolved_report_path, stored_reference = (
                _validate_report_reference(
                    report_row["report_path"],
                    report_dir=report_dir,
                )
            )
        except DatabaseValidationError as exc:
            raise DatabaseReadError(
                "The stored report reference is invalid."
            ) from exc
        if stored_reference != report_row["report_path"]:
            raise DatabaseReadError(
                "The stored report reference is invalid."
            )
        restored_report_path = str(resolved_report_path)

    return {
        **metadata,
        "candidates": candidates,
        "evidence_objects": evidence_objects,
        "report_path": restored_report_path,
    }


__all__ = [
    "DATABASE_SCHEMA_VERSION",
    "DATABASE_TABLES",
    "MAX_STORED_CANDIDATES",
    "MAX_STORED_EVIDENCE_OBJECTS",
    "MAX_PIPELINE_STATE_JSON_BYTES",
    "ANALYSIS_STATUSES",
    "AnalysisRecord",
    "AnalysisNotFoundError",
    "StoredCandidateVariant",
    "StoredAnalysisRecord",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "DatabaseError",
    "DatabaseInitializationError",
    "DatabaseReadError",
    "DatabaseValidationError",
    "DatabaseWriteError",
    "connect_database",
    "get_analysis",
    "initialize_database",
    "load_pipeline_state",
    "save_analysis",
    "save_complete_analysis",
    "save_evidence_objects",
    "save_report",
    "save_pipeline_state",
    "save_variants",
]
