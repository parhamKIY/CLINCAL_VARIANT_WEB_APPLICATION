"""SQLite persistence foundation for clinical analysis records."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from config import settings


DATABASE_SCHEMA_VERSION = 1
DATABASE_BUSY_TIMEOUT_MS = 5_000
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

PRAGMA user_version = {DATABASE_SCHEMA_VERSION};
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


def _resolve_database_path(
    database_path: str | Path | None,
) -> Path:
    """Resolve and validate one database file path."""

    resolved_path = Path(
        database_path
        if database_path is not None
        else settings.DATABASE_PATH
    ).expanduser().resolve()
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
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            resolved_path,
            timeout=DATABASE_BUSY_TIMEOUT_MS / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}"
        )
        foreign_keys_enabled = connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
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
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseConfigurationError(
            "The database directory could not be prepared."
        ) from exc

    connection = connect_database(resolved_path)
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
        elif schema_version < DATABASE_SCHEMA_VERSION:
            raise DatabaseInitializationError(
                "The analysis database requires an unsupported schema "
                "migration."
            )

        _validate_schema(connection)
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

    return resolved_path


__all__ = [
    "DATABASE_SCHEMA_VERSION",
    "DATABASE_TABLES",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "DatabaseError",
    "DatabaseInitializationError",
    "connect_database",
    "initialize_database",
]
