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
from typing import TypedDict
from uuid import uuid4

from backend.report import (
    MAX_CLINICAL_REPORT_MARKDOWN_BYTES,
    MAX_EVIDENCE_ALLELE_LENGTH,
    EvidenceObject,
    EvidenceObjectError,
    sanitize_evidence_object,
)
from config import settings


DATABASE_SCHEMA_VERSION = 1
DATABASE_BUSY_TIMEOUT_MS = 5_000
MAX_ANALYSIS_WARNINGS = 100
MAX_ANALYSIS_WARNING_LENGTH = 1_000
MAX_ANALYSIS_WARNINGS_JSON_BYTES = 64 * 1024
MAX_STORED_CANDIDATES = 100
MAX_STORED_EVIDENCE_OBJECTS = 100
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
_CANDIDATE_ALLOWED_FIELDS = frozenset(
    {
        "chrom",
        "pos",
        "ref",
        "alt",
        "qual",
        "filter",
        "genotype",
    }
)
_REPORT_FILENAME_PATTERN = re.compile(
    r"clinical-report-[A-Za-z0-9][A-Za-z0-9.-]{0,239}\.md"
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


class DatabaseValidationError(DatabaseError, ValueError):
    """Raised when analysis metadata is unsafe or malformed."""


class DatabaseWriteError(DatabaseError):
    """Raised when validated analysis metadata cannot be persisted."""


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


def save_analysis(
    *,
    status: str,
    source_filename: str | None = None,
    warnings: Iterable[str] = (),
    database_path: str | Path | None = None,
) -> AnalysisRecord:
    """Create and persist one analysis metadata record."""

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

    resolved_path = initialize_database(database_path)
    connection = connect_database(resolved_path)
    try:
        with connection:
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
                    analysis_id,
                    created_at,
                    anonymized_filename,
                    normalized_status,
                    warnings_json,
                ),
            )
    except sqlite3.Error as exc:
        raise DatabaseWriteError(
            "The analysis metadata could not be saved."
        ) from exc
    finally:
        connection.close()

    return {
        "analysis_id": analysis_id,
        "created_at": created_at,
        "anonymized_filename": anonymized_filename,
        "status": normalized_status,
        "warnings": list(normalized_warnings),
    }


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


def save_variants(
    analysis_id: str,
    candidates: Iterable[object],
    *,
    database_path: str | Path | None = None,
) -> int:
    """Persist bounded genotype-free candidate variants atomically."""

    normalized_id = _validate_analysis_id(analysis_id)
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


def save_evidence_objects(
    analysis_id: str,
    evidence_objects: Iterable[object],
    *,
    database_path: str | Path | None = None,
) -> int:
    """Validate, sanitize, and persist Evidence Objects atomically."""

    normalized_id = _validate_analysis_id(analysis_id)
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
            "Report references must point to a generated Markdown "
            "file directly inside the configured report directory."
        )

    try:
        with resolved_path.open("rb") as report_file:
            report_data = report_file.read(
                MAX_CLINICAL_REPORT_MARKDOWN_BYTES + 1
            )
    except OSError as exc:
        raise DatabaseValidationError(
            "The generated report file could not be read."
        ) from exc
    if not report_data:
        raise DatabaseValidationError(
            "The generated report file is empty."
        )
    if len(report_data) > MAX_CLINICAL_REPORT_MARKDOWN_BYTES:
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


__all__ = [
    "DATABASE_SCHEMA_VERSION",
    "DATABASE_TABLES",
    "MAX_STORED_CANDIDATES",
    "MAX_STORED_EVIDENCE_OBJECTS",
    "ANALYSIS_STATUSES",
    "AnalysisRecord",
    "StoredCandidateVariant",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "DatabaseError",
    "DatabaseInitializationError",
    "DatabaseValidationError",
    "DatabaseWriteError",
    "connect_database",
    "initialize_database",
    "save_analysis",
    "save_evidence_objects",
    "save_report",
    "save_variants",
]
