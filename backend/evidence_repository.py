"""Isolated, provenance-first storage for normalized provider observations.

Repository records are operational snapshots. They are never trusted clinical
evidence and this module intentionally has no provider or pipeline integration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from backend.privacy import (
    ClinicalDataPrivacyError,
    validate_llm_payload,
    validate_no_prohibited_fields,
)
from backend.variant_integrity import (
    VariantIntegrityError,
    stable_allele_identity,
)
from config import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE, settings


EVIDENCE_REPOSITORY_SCHEMA_VERSION = 2
EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION = "1.0"
EVIDENCE_REPOSITORY_BUSY_TIMEOUT_MS = 5_000
_REPOSITORY_TABLE = "evidence_repository_records"
_REPOSITORY_LOOKUP_INDEX = "evidence_repository_lookup_v2"
_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_RECORD_ID_PATTERN = re.compile(r"repository-sha256:[0-9a-f]{64}")
_CANONICAL_DIGEST_PATTERN = re.compile(r"allele-sha256:[0-9a-f]{64}")
_STORABLE_OBSERVATION_STATUSES = frozenset({"success", "no_match"})
_OPERATIONAL_FAILURE_STATUSES = frozenset(
    {
        "configuration_error",
        "connection_error",
        "forbidden",
        "http_429",
        "http_5xx",
        "invalid_response",
        "network_failure",
        "rate_limited",
        "server_error",
        "timeout",
        "unavailable",
    }
)
_FORBIDDEN_REPOSITORY_FIELDS = frozenset(
    {
        "accepted_hpo_terms",
        "acmg_conclusion",
        "analysis_id",
        "clinical_narrative",
        "clinical_report",
        "clinical_text",
        "clinical_text_fa",
        "final_acmg_classification",
        "final_interpretation",
        "final_report",
        "human_interpretation",
        "llm_interpretation",
        "patient",
        "patient_hpo_context",
        "patient_hpo_terms",
        "report",
    }
)
_REPOSITORY_COLUMNS = (
    "record_id",
    "canonical_digest",
    "assembly",
    "chromosome",
    "position",
    "reference_allele",
    "alternate_allele",
    "semantic_node",
    "provider",
    "provider_role",
    "source",
    "query_identity_hash",
    "observation_status",
    "normalized_payload_json",
    "retrieved_at",
    "stored_at",
    "payload_sha256",
    "schema_version",
)
_REPOSITORY_LOOKUP_COLUMNS = (
    "canonical_digest",
    "semantic_node",
    "provider",
    "query_identity_hash",
    "retrieved_at",
    "stored_at",
    "record_id",
)


class EvidenceRepositoryError(RuntimeError):
    """Base error for isolated evidence-repository operations."""


class EvidenceRepositoryConfigurationError(EvidenceRepositoryError):
    """Raised when repository storage cannot be configured safely."""


class EvidenceRepositoryStorageError(EvidenceRepositoryError):
    """Raised when SQLite cannot complete a repository operation."""


class EvidenceRepositoryIntegrityError(EvidenceRepositoryError):
    """Raised when stored bytes or identity fail integrity verification."""


class EvidenceRepositoryValidationError(ValueError):
    """Raised when a proposed repository observation is unsafe."""


@dataclass(frozen=True)
class EvidenceRepositoryRecord:
    """One verified immutable provider-observation snapshot."""

    record_id: str
    canonical_digest: str
    assembly: str
    chromosome: str
    position: int
    reference_allele: str
    alternate_allele: str
    semantic_node: str
    provider: str
    provider_role: str
    source: str
    query_identity_hash: str
    observation_status: str
    normalized_payload: dict[str, Any]
    retrieved_at: str
    stored_at: str
    payload_sha256: str
    schema_version: str


@dataclass(frozen=True)
class EvidenceRepositoryIntegrityScan:
    """Bounded integrity scan without exposing invalid stored content."""

    records: tuple[EvidenceRepositoryRecord, ...]
    total_records: int
    invalid_records: int


@dataclass(frozen=True)
class EvidenceRepositoryInvalidCleanup:
    """Result of an explicit invalid-record cleanup operation."""

    records_evaluated: int
    records_removed: int
    records_retained: int


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceRepositoryValidationError(
            "Repository data must be finite JSON."
        ) from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _field_name(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


def _validate_global_provider_data(
    value: Mapping[str, object],
    *,
    context: str,
) -> dict[str, Any]:
    try:
        validate_no_prohibited_fields(value, context=context)
        validate_llm_payload(value)
    except ClinicalDataPrivacyError as exc:
        raise EvidenceRepositoryValidationError(
            "Repository data contains case-specific or clinical conclusion "
            "fields."
        ) from exc

    def inspect(item: object) -> None:
        if isinstance(item, Mapping):
            if any(
                _field_name(key) in _FORBIDDEN_REPOSITORY_FIELDS
                for key in item
            ):
                raise EvidenceRepositoryValidationError(
                    "Repository data contains case-specific or clinical "
                    "conclusion fields."
                )
            for nested in item.values():
                inspect(nested)
        elif isinstance(item, list | tuple):
            for nested in item:
                inspect(nested)

    inspect(value)
    canonical = _canonical_json_bytes(value)
    return json.loads(canonical.decode("utf-8"))


def _contains_operational_failure(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key, item in value.items():
        normalized_key = _field_name(key)
        if (
            normalized_key
            in {"failure_status", "provider_status", "status"}
            and isinstance(item, str)
            and item.strip().casefold() in _OPERATIONAL_FAILURE_STATUSES
        ):
            return True
        if (
            normalized_key in {"http_status", "status_code"}
            and isinstance(item, int)
            and not isinstance(item, bool)
            and (item == 429 or item >= 500)
        ):
            return True
        if isinstance(item, Mapping) and _contains_operational_failure(item):
            return True
        if isinstance(item, list | tuple) and any(
            _contains_operational_failure(nested) for nested in item
        ):
            return True
    return False


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceRepositoryValidationError(
            f"Repository {field} must be a string."
        )
    normalized = value.strip().casefold()
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise EvidenceRepositoryValidationError(
            f"Repository {field} is invalid."
        )
    return normalized


def _timestamp(value: object, *, field: str) -> str:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceRepositoryValidationError(
                f"Repository {field} is invalid."
            ) from exc
    else:
        raise EvidenceRepositoryValidationError(
            f"Repository {field} is invalid."
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceRepositoryValidationError(
            f"Repository {field} must include a timezone."
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_identity(
    variant: Mapping[str, object],
    *,
    assembly: object,
) -> tuple[str, str, str, int, str, str]:
    try:
        digest = stable_allele_identity(variant, assembly=assembly)
    except VariantIntegrityError as exc:
        raise EvidenceRepositoryValidationError(
            "Repository canonical identity is invalid."
        ) from exc
    assert isinstance(assembly, str)
    chromosome = str(variant["chrom"]).strip().removeprefix("chr").upper()
    if chromosome == "M":
        chromosome = "MT"
    position = int(variant["pos"])
    reference = str(variant["ref"]).strip().upper()
    alternate = str(variant["alt"]).strip().upper()
    return digest, assembly, chromosome, position, reference, alternate


def _record_id(
    *,
    canonical_digest: str,
    assembly: str,
    chromosome: str,
    position: int,
    reference_allele: str,
    alternate_allele: str,
    semantic_node: str,
    provider: str,
    provider_role: str,
    source: str,
    query_identity_hash: str,
    observation_status: str,
    retrieved_at: str,
    payload_sha256: str,
) -> str:
    identity = {
        "alternate_allele": alternate_allele,
        "assembly": assembly,
        "canonical_digest": canonical_digest,
        "chromosome": chromosome,
        "observation_status": observation_status,
        "payload_sha256": payload_sha256,
        "position": position,
        "provider": provider,
        "provider_role": provider_role,
        "query_identity_hash": query_identity_hash,
        "reference_allele": reference_allele,
        "retrieved_at": retrieved_at,
        "schema_version": EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION,
        "semantic_node": semantic_node,
        "source": source,
    }
    return "repository-sha256:" + _sha256(_canonical_json_bytes(identity))


class EvidenceRepository:
    """Versioned SQLite storage for verified normalized provider snapshots."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        configured = Path(
            settings.EVIDENCE_REPOSITORY_PATH
            if database_path is None
            else database_path
        ).expanduser()
        if configured.is_symlink():
            raise EvidenceRepositoryConfigurationError(
                "The evidence repository path cannot be a symbolic link."
            )
        resolved = configured.resolve()
        if resolved.exists() and not resolved.is_file():
            raise EvidenceRepositoryConfigurationError(
                "The evidence repository path must point to a file."
            )
        if resolved == Path(settings.DATABASE_PATH).expanduser().resolve():
            raise EvidenceRepositoryConfigurationError(
                "The evidence repository must be separate from clinical "
                "analysis persistence."
            )
        self.database_path = resolved
        self._initialization_lock = RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        existed = self.database_path.exists()
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=EVIDENCE_REPOSITORY_BUSY_TIMEOUT_MS / 1_000,
            )
            self.database_path.chmod(PRIVATE_FILE_MODE)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"PRAGMA busy_timeout = {EVIDENCE_REPOSITORY_BUSY_TIMEOUT_MS}"
            )
            connection.execute("PRAGMA synchronous = NORMAL")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise sqlite3.DatabaseError(
                    "SQLite foreign keys could not be enabled."
                )
            return connection
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            if not existed:
                try:
                    self.database_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise EvidenceRepositoryStorageError(
                "The evidence repository could not be opened."
            ) from exc

    @staticmethod
    def _application_tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }

    @staticmethod
    def _validate_table_schema(connection: sqlite3.Connection) -> None:
        if EvidenceRepository._application_tables(connection) != {
            _REPOSITORY_TABLE
        }:
            raise EvidenceRepositoryIntegrityError(
                "The evidence repository schema is incomplete or unexpected."
            )
        columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f'PRAGMA table_info("{_REPOSITORY_TABLE}")'
            )
        )
        if columns != _REPOSITORY_COLUMNS:
            raise EvidenceRepositoryIntegrityError(
                "The evidence repository schema is incomplete or unexpected."
            )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        EvidenceRepository._validate_table_schema(connection)
        index_columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f'PRAGMA index_info("{_REPOSITORY_LOOKUP_INDEX}")'
            )
        )
        if index_columns != _REPOSITORY_LOOKUP_COLUMNS:
            raise EvidenceRepositoryIntegrityError(
                "The evidence repository schema is incomplete or unexpected."
            )

    @staticmethod
    def _create_lookup_index(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE INDEX {_REPOSITORY_LOOKUP_INDEX}
            ON {_REPOSITORY_TABLE} (
                {', '.join(_REPOSITORY_LOOKUP_COLUMNS)}
            )
            """
        )

    def initialize(self) -> Path:
        """Create or verify the isolated repository schema."""

        with self._initialization_lock:
            try:
                self.database_path.parent.mkdir(
                    mode=PRIVATE_DIRECTORY_MODE,
                    parents=True,
                    exist_ok=True,
                )
                self.database_path.parent.chmod(PRIVATE_DIRECTORY_MODE)
            except OSError as exc:
                raise EvidenceRepositoryConfigurationError(
                    "The evidence repository directory could not be prepared."
                ) from exc

            connection = self._connect()
            try:
                journal_mode = connection.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()[0]
                if str(journal_mode).casefold() != "wal":
                    raise EvidenceRepositoryStorageError(
                        "The evidence repository could not enable WAL mode."
                    )
                connection.execute("BEGIN IMMEDIATE")
                schema_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if schema_version > EVIDENCE_REPOSITORY_SCHEMA_VERSION:
                    raise EvidenceRepositoryIntegrityError(
                        "The evidence repository was created by a newer version."
                    )
                if schema_version == 0:
                    if self._application_tables(connection):
                        raise EvidenceRepositoryIntegrityError(
                            "The existing evidence repository has no supported "
                            "schema version."
                        )
                    connection.execute(
                        """
                        CREATE TABLE evidence_repository_records (
                            record_id TEXT PRIMARY KEY,
                            canonical_digest TEXT NOT NULL,
                            assembly TEXT NOT NULL,
                            chromosome TEXT NOT NULL,
                            position INTEGER NOT NULL CHECK (position > 0),
                            reference_allele TEXT NOT NULL,
                            alternate_allele TEXT NOT NULL,
                            semantic_node TEXT NOT NULL,
                            provider TEXT NOT NULL,
                            provider_role TEXT NOT NULL
                                CHECK (provider_role IN ('primary', 'fallback')),
                            source TEXT NOT NULL,
                            query_identity_hash TEXT NOT NULL,
                            observation_status TEXT NOT NULL
                                CHECK (observation_status IN ('success', 'no_match')),
                            normalized_payload_json TEXT NOT NULL,
                            retrieved_at TEXT NOT NULL,
                            stored_at TEXT NOT NULL,
                            payload_sha256 TEXT NOT NULL,
                            schema_version TEXT NOT NULL
                        ) WITHOUT ROWID
                        """
                    )
                    self._create_lookup_index(connection)
                    connection.execute(
                        f"PRAGMA user_version = {EVIDENCE_REPOSITORY_SCHEMA_VERSION}"
                    )
                elif schema_version == 1:
                    self._validate_table_schema(connection)
                    connection.execute(
                        "DROP INDEX IF EXISTS evidence_repository_lookup"
                    )
                    self._create_lookup_index(connection)
                    connection.execute(
                        f"PRAGMA user_version = {EVIDENCE_REPOSITORY_SCHEMA_VERSION}"
                    )
                self._validate_schema(connection)
                connection.commit()
            except EvidenceRepositoryError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise EvidenceRepositoryStorageError(
                    "The evidence repository schema could not be initialized."
                ) from exc
            finally:
                connection.close()
            try:
                self.database_path.chmod(PRIVATE_FILE_MODE)
            except OSError as exc:
                raise EvidenceRepositoryConfigurationError(
                    "The evidence repository permissions could not be secured."
                ) from exc
            self._initialized = True
            return self.database_path

    def _connect_verified(self) -> sqlite3.Connection:
        """Open one operation connection after bounded schema initialization."""

        with self._initialization_lock:
            if not self._initialized or not self.database_path.is_file():
                self.initialize()
        connection = self._connect()
        try:
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if schema_version != EVIDENCE_REPOSITORY_SCHEMA_VERSION:
                raise EvidenceRepositoryIntegrityError(
                    "The evidence repository schema is unsupported."
                )
            self._validate_schema(connection)
            return connection
        except EvidenceRepositoryError:
            connection.close()
            raise
        except sqlite3.Error as exc:
            connection.close()
            raise EvidenceRepositoryStorageError(
                "The evidence repository schema could not be verified."
            ) from exc

    def store_provider_observation(
        self,
        *,
        canonical_variant: Mapping[str, object],
        assembly: object,
        semantic_node: object,
        provider: object,
        provider_role: object,
        source: object,
        query_identity: Mapping[str, object],
        normalized_payload: Mapping[str, object],
        observation_status: object,
        retrieved_at: object,
    ) -> EvidenceRepositoryRecord:
        """Store one immutable normalized provider observation."""

        if not isinstance(observation_status, str):
            raise EvidenceRepositoryValidationError(
                "Repository observation status is invalid."
            )
        status = observation_status.strip().casefold()
        if status in _OPERATIONAL_FAILURE_STATUSES:
            raise EvidenceRepositoryValidationError(
                "Provider operational failures cannot be stored."
            )
        if status not in _STORABLE_OBSERVATION_STATUSES:
            raise EvidenceRepositoryValidationError(
                "Only explicit success or no_match observations may be stored."
            )
        if not isinstance(canonical_variant, Mapping):
            raise EvidenceRepositoryValidationError(
                "Repository canonical identity is invalid."
            )
        (
            canonical_digest,
            normalized_assembly,
            chromosome,
            position,
            reference,
            alternate,
        ) = _canonical_identity(canonical_variant, assembly=assembly)
        normalized_semantic_node = _identifier(
            semantic_node,
            field="semantic node",
        )
        normalized_provider = _identifier(provider, field="provider")
        normalized_provider_role = _identifier(
            provider_role,
            field="provider role",
        )
        if normalized_provider_role not in {"primary", "fallback"}:
            raise EvidenceRepositoryValidationError(
                "Repository provider role must be primary or fallback."
            )
        normalized_source = _identifier(source, field="source")
        if not isinstance(query_identity, Mapping):
            raise EvidenceRepositoryValidationError(
                "Repository query identity must be a mapping."
            )
        safe_query_identity = _validate_global_provider_data(
            query_identity,
            context="Evidence repository query identity",
        )
        if not isinstance(normalized_payload, Mapping):
            raise EvidenceRepositoryValidationError(
                "Repository normalized payload must be a mapping."
            )
        safe_payload = _validate_global_provider_data(
            normalized_payload,
            context="Evidence repository payload",
        )
        if _contains_operational_failure(safe_payload):
            raise EvidenceRepositoryValidationError(
                "Provider operational failures cannot be stored."
            )
        if status == "no_match" and safe_payload.get("status") != "no_match":
            raise EvidenceRepositoryValidationError(
                "A no_match observation must be explicit in its payload."
            )
        payload_bytes = _canonical_json_bytes(safe_payload)
        payload_sha256 = _sha256(payload_bytes)
        query_identity_hash = _sha256(
            _canonical_json_bytes(safe_query_identity)
        )
        normalized_retrieved_at = _timestamp(
            retrieved_at,
            field="retrieval timestamp",
        )
        stored_at = _timestamp(
            datetime.now(UTC),
            field="storage timestamp",
        )
        record_id = _record_id(
            canonical_digest=canonical_digest,
            assembly=normalized_assembly,
            chromosome=chromosome,
            position=position,
            reference_allele=reference,
            alternate_allele=alternate,
            semantic_node=normalized_semantic_node,
            provider=normalized_provider,
            provider_role=normalized_provider_role,
            source=normalized_source,
            query_identity_hash=query_identity_hash,
            observation_status=status,
            retrieved_at=normalized_retrieved_at,
            payload_sha256=payload_sha256,
        )
        values = (
            record_id,
            canonical_digest,
            normalized_assembly,
            chromosome,
            position,
            reference,
            alternate,
            normalized_semantic_node,
            normalized_provider,
            normalized_provider_role,
            normalized_source,
            query_identity_hash,
            status,
            payload_bytes.decode("utf-8"),
            normalized_retrieved_at,
            stored_at,
            payload_sha256,
            EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION,
        )

        connection = self._connect_verified()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"""
                    INSERT INTO {_REPOSITORY_TABLE} (
                        {', '.join(_REPOSITORY_COLUMNS)}
                    ) VALUES ({', '.join('?' for _ in _REPOSITORY_COLUMNS)})
                    """,
                    values,
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
            row = connection.execute(
                f"SELECT * FROM {_REPOSITORY_TABLE} WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise EvidenceRepositoryStorageError(
                    "The evidence repository record could not be stored."
                )
            return self._verified_record(row)
        except EvidenceRepositoryError:
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise EvidenceRepositoryStorageError(
                "The evidence repository record could not be stored."
            ) from exc
        finally:
            connection.close()

    def _verified_record(
        self,
        row: sqlite3.Row,
    ) -> EvidenceRepositoryRecord:
        try:
            row_fields = set(row.keys())
            if row_fields != set(_REPOSITORY_COLUMNS):
                raise EvidenceRepositoryIntegrityError(
                    "Repository record integrity verification failed."
                )
            payload = json.loads(str(row["normalized_payload_json"]))
            if not isinstance(payload, Mapping):
                raise EvidenceRepositoryIntegrityError(
                    "Repository record integrity verification failed."
                )
            safe_payload = _validate_global_provider_data(
                payload,
                context="Stored evidence repository payload",
            )
            payload_sha256 = _sha256(_canonical_json_bytes(safe_payload))
            if not hmac.compare_digest(
                payload_sha256,
                str(row["payload_sha256"]),
            ):
                raise EvidenceRepositoryIntegrityError(
                    "Repository record integrity verification failed."
                )
            variant = {
                "chrom": row["chromosome"],
                "pos": row["position"],
                "ref": row["reference_allele"],
                "alt": row["alternate_allele"],
            }
            (
                canonical_digest,
                assembly,
                chromosome,
                position,
                reference,
                alternate,
            ) = _canonical_identity(variant, assembly=row["assembly"])
            if not hmac.compare_digest(
                canonical_digest,
                str(row["canonical_digest"]),
            ):
                raise EvidenceRepositoryIntegrityError(
                    "Repository record integrity verification failed."
                )
            semantic_node = _identifier(
                row["semantic_node"], field="semantic node"
            )
            provider = _identifier(row["provider"], field="provider")
            provider_role = _identifier(
                row["provider_role"], field="provider role"
            )
            source = _identifier(row["source"], field="source")
            query_identity_hash = str(row["query_identity_hash"])
            observation_status = str(row["observation_status"])
            retrieved_at = _timestamp(
                row["retrieved_at"], field="retrieval timestamp"
            )
            stored_at = _timestamp(
                row["stored_at"], field="storage timestamp"
            )
            schema_version = str(row["schema_version"])
            if (
                provider_role not in {"primary", "fallback"}
                or observation_status not in _STORABLE_OBSERVATION_STATUSES
                or _SHA256_PATTERN.fullmatch(query_identity_hash) is None
                or schema_version
                != EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION
                or _CANONICAL_DIGEST_PATTERN.fullmatch(canonical_digest) is None
            ):
                raise EvidenceRepositoryIntegrityError(
                    "Repository record integrity verification failed."
                )
            expected_record_id = _record_id(
                canonical_digest=canonical_digest,
                assembly=assembly,
                chromosome=chromosome,
                position=position,
                reference_allele=reference,
                alternate_allele=alternate,
                semantic_node=semantic_node,
                provider=provider,
                provider_role=provider_role,
                source=source,
                query_identity_hash=query_identity_hash,
                observation_status=observation_status,
                retrieved_at=retrieved_at,
                payload_sha256=payload_sha256,
            )
            record_id = str(row["record_id"])
            if (
                _RECORD_ID_PATTERN.fullmatch(record_id) is None
                or not hmac.compare_digest(record_id, expected_record_id)
            ):
                raise EvidenceRepositoryIntegrityError(
                    "Repository record integrity verification failed."
                )
            return EvidenceRepositoryRecord(
                record_id=record_id,
                canonical_digest=canonical_digest,
                assembly=assembly,
                chromosome=chromosome,
                position=position,
                reference_allele=reference,
                alternate_allele=alternate,
                semantic_node=semantic_node,
                provider=provider,
                provider_role=provider_role,
                source=source,
                query_identity_hash=query_identity_hash,
                observation_status=observation_status,
                normalized_payload=deepcopy(safe_payload),
                retrieved_at=retrieved_at,
                stored_at=stored_at,
                payload_sha256=payload_sha256,
                schema_version=schema_version,
            )
        except EvidenceRepositoryError:
            raise
        except (EvidenceRepositoryValidationError, json.JSONDecodeError) as exc:
            raise EvidenceRepositoryIntegrityError(
                "Repository record integrity verification failed."
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceRepositoryIntegrityError(
                "Repository record integrity verification failed."
            ) from exc

    def find_by_canonical_identity(
        self,
        *,
        canonical_variant: Mapping[str, object],
        assembly: object,
        semantic_node: object | None = None,
        provider: object | None = None,
        query_identity: Mapping[str, object] | None = None,
    ) -> list[EvidenceRepositoryRecord]:
        """Return verified snapshots for one exact canonical identity."""

        canonical_digest, _, _, _, _, _ = _canonical_identity(
            canonical_variant,
            assembly=assembly,
        )
        clauses = ["canonical_digest = ?"]
        parameters: list[object] = [canonical_digest]
        if semantic_node is not None:
            clauses.append("semantic_node = ?")
            parameters.append(
                _identifier(semantic_node, field="semantic node")
            )
        if provider is not None:
            clauses.append("provider = ?")
            parameters.append(_identifier(provider, field="provider"))
        if query_identity is not None:
            if not isinstance(query_identity, Mapping):
                raise EvidenceRepositoryValidationError(
                    "Repository query identity must be a mapping."
                )
            safe_query_identity = _validate_global_provider_data(
                query_identity,
                context="Evidence repository query identity",
            )
            clauses.append("query_identity_hash = ?")
            parameters.append(
                _sha256(_canonical_json_bytes(safe_query_identity))
            )
        connection = self._connect_verified()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM {_REPOSITORY_TABLE}
                WHERE {' AND '.join(clauses)}
                ORDER BY retrieved_at, stored_at, record_id
                """,
                parameters,
            ).fetchall()
            return [self._verified_record(row) for row in rows]
        except EvidenceRepositoryError:
            raise
        except sqlite3.Error as exc:
            raise EvidenceRepositoryStorageError(
                "The evidence repository records could not be retrieved."
            ) from exc
        finally:
            connection.close()

    def scan_integrity(self) -> EvidenceRepositoryIntegrityScan:
        """Inspect all records while withholding corrupted stored content."""

        connection = self._connect_verified()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM {_REPOSITORY_TABLE}
                ORDER BY stored_at, record_id
                """
            ).fetchall()
            verified: list[EvidenceRepositoryRecord] = []
            invalid_records = 0
            for row in rows:
                try:
                    verified.append(self._verified_record(row))
                except EvidenceRepositoryIntegrityError:
                    invalid_records += 1
            return EvidenceRepositoryIntegrityScan(
                records=tuple(verified),
                total_records=len(rows),
                invalid_records=invalid_records,
            )
        except EvidenceRepositoryError:
            raise
        except sqlite3.Error as exc:
            raise EvidenceRepositoryStorageError(
                "The evidence repository integrity scan failed."
            ) from exc
        finally:
            connection.close()

    def remove_invalid_records(self) -> EvidenceRepositoryInvalidCleanup:
        """Delete only records that fail deterministic integrity checks."""

        connection = self._connect_verified()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT * FROM {_REPOSITORY_TABLE}
                ORDER BY stored_at, record_id
                """
            ).fetchall()
            invalid_record_ids: list[str] = []
            for row in rows:
                try:
                    self._verified_record(row)
                except EvidenceRepositoryIntegrityError:
                    invalid_record_ids.append(str(row["record_id"]))
            for record_id in invalid_record_ids:
                connection.execute(
                    f"DELETE FROM {_REPOSITORY_TABLE} WHERE record_id = ?",
                    (record_id,),
                )
            connection.commit()
            removed = len(invalid_record_ids)
            return EvidenceRepositoryInvalidCleanup(
                records_evaluated=len(rows),
                records_removed=removed,
                records_retained=len(rows) - removed,
            )
        except EvidenceRepositoryError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise EvidenceRepositoryStorageError(
                "Invalid evidence repository records could not be removed."
            ) from exc
        finally:
            connection.close()


__all__ = [
    "EVIDENCE_REPOSITORY_RECORD_SCHEMA_VERSION",
    "EVIDENCE_REPOSITORY_SCHEMA_VERSION",
    "EvidenceRepository",
    "EvidenceRepositoryConfigurationError",
    "EvidenceRepositoryError",
    "EvidenceRepositoryIntegrityError",
    "EvidenceRepositoryIntegrityScan",
    "EvidenceRepositoryInvalidCleanup",
    "EvidenceRepositoryRecord",
    "EvidenceRepositoryStorageError",
    "EvidenceRepositoryValidationError",
]
