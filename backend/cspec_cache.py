"""Validated local last-known-good storage for bounded CSpec metadata."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlsplit


CSPEC_CACHE_SCHEMA_VERSION = 1
MAX_CSPEC_CACHE_BYTES = 2_000_000
MAX_CSPEC_CACHE_ENTRIES = 500
MAX_CSPEC_CACHE_SPECIFICATIONS = 10
CSPEC_ID_PATTERN = re.compile(r"^GN[0-9]+$")
MONDO_ID_PATTERN = re.compile(r"^MONDO:[0-9]{7}$")
GENE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,99}$")
_CACHE_LOCK = Lock()
_SPECIFICATION_FIELDS = frozenset(
    {
        "specification_id",
        "title",
        "short_title",
        "version",
        "status",
        "vcep",
        "approved_at",
        "modified_at",
        "source_document_url",
        "specification_url",
        "concept_doi",
        "document_doi",
        "matched_disease_ids",
        "scope_match",
        "applicable_to_disease_context",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "gene",
        "disease_ids",
        "source_retrieved_at",
        "cache_stored_at",
        "specifications",
    }
)
CSpecCacheStatus = Literal["success", "missing", "no_match", "invalid"]


class CSpecCacheError(ValueError):
    """Raised when CSpec cache content or persistence is unsafe."""


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CSpecCacheError("CSpec cache timestamp is invalid.")
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CSpecCacheError("CSpec cache timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        raise CSpecCacheError("CSpec cache timestamp is invalid.")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(value: object, *, maximum: int = 1_000) -> str:
    if not isinstance(value, str):
        raise CSpecCacheError("CSpec cache text field is invalid.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise CSpecCacheError("CSpec cache text field is invalid.")
    return normalized


def _optional_text(value: object, *, maximum: int = 1_000) -> str | None:
    if value is None:
        return None
    return _required_text(value, maximum=maximum)


def _safe_url(value: object) -> str | None:
    url = _optional_text(value, maximum=2_000)
    if url is None:
        return None
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not (
            hostname == "clinicalgenome.org"
            or hostname.endswith(".clinicalgenome.org")
            or hostname == "genome.network"
            or hostname.endswith(".genome.network")
        )
    ):
        raise CSpecCacheError("CSpec cache URL is invalid.")
    return url


def _disease_ids(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 10:
        raise CSpecCacheError("CSpec cache disease identifiers are invalid.")
    identifiers: list[str] = []
    for item in value:
        identifier = _required_text(item, maximum=20)
        if (
            not MONDO_ID_PATTERN.fullmatch(identifier)
            or identifier in identifiers
        ):
            raise CSpecCacheError(
                "CSpec cache disease identifiers are invalid."
            )
        identifiers.append(identifier)
    return identifiers


def _specification(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SPECIFICATION_FIELDS:
        raise CSpecCacheError("CSpec cache specification is invalid.")
    identifier = _required_text(value["specification_id"], maximum=32)
    if not CSPEC_ID_PATTERN.fullmatch(identifier):
        raise CSpecCacheError("CSpec cache specification ID is invalid.")
    status = _required_text(value["status"], maximum=20)
    scope = _required_text(value["scope_match"], maximum=30)
    matched_diseases = _disease_ids(value["matched_disease_ids"])
    applicable = value["applicable_to_disease_context"]
    if (
        status != "Released"
        or scope not in {"gene_only", "gene_and_disease"}
        or (
            applicable is not None
            and not isinstance(applicable, bool)
        )
        or (scope == "gene_and_disease") != bool(matched_diseases)
    ):
        raise CSpecCacheError("CSpec cache specification scope is invalid.")
    return {
        "specification_id": identifier,
        "title": _required_text(value["title"]),
        "short_title": _optional_text(value["short_title"]),
        "version": _required_text(value["version"], maximum=100),
        "status": status,
        "vcep": _optional_text(value["vcep"]),
        "approved_at": _optional_text(value["approved_at"], maximum=100),
        "modified_at": _optional_text(value["modified_at"], maximum=100),
        "source_document_url": _safe_url(value["source_document_url"]),
        "specification_url": _safe_url(value["specification_url"]),
        "concept_doi": _optional_text(value["concept_doi"], maximum=200),
        "document_doi": _optional_text(value["document_doi"], maximum=200),
        "matched_disease_ids": matched_diseases,
        "scope_match": scope,
        "applicable_to_disease_context": applicable,
    }


def _entry(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
        raise CSpecCacheError("CSpec cache entry is invalid.")
    gene = _required_text(value["gene"], maximum=100).upper()
    if not GENE_PATTERN.fullmatch(gene):
        raise CSpecCacheError("CSpec cache gene is invalid.")
    specifications = value["specifications"]
    if (
        not isinstance(specifications, list)
        or not specifications
        or len(specifications) > MAX_CSPEC_CACHE_SPECIFICATIONS
    ):
        raise CSpecCacheError("CSpec cache specifications are invalid.")
    return {
        "gene": gene,
        "disease_ids": _disease_ids(value["disease_ids"]),
        "source_retrieved_at": _timestamp(value["source_retrieved_at"]),
        "cache_stored_at": _timestamp(value["cache_stored_at"]),
        "specifications": [_specification(item) for item in specifications],
    }


def _document(value: object) -> list[dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "entries"}
        or value.get("schema_version") != CSPEC_CACHE_SCHEMA_VERSION
        or not isinstance(value.get("entries"), list)
        or len(value["entries"]) > MAX_CSPEC_CACHE_ENTRIES
    ):
        raise CSpecCacheError("CSpec cache document is invalid.")
    return [_entry(item) for item in value["entries"]]


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file() or path.stat().st_size > MAX_CSPEC_CACHE_BYTES:
        raise CSpecCacheError("CSpec cache file is invalid.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CSpecCacheError("CSpec cache file is invalid.") from exc
    return _document(payload)


def load_cspec_lkg(
    path: Path,
    *,
    gene: str,
    disease_ids: list[str],
) -> tuple[CSpecCacheStatus, dict[str, Any] | None]:
    """Load one exact gene/disease cache entry without trusting file content."""

    normalized_gene = _required_text(gene, maximum=100).upper()
    normalized_diseases = _disease_ids(disease_ids)
    with _CACHE_LOCK:
        try:
            entries = _read(path)
        except FileNotFoundError:
            return "missing", None
        except CSpecCacheError:
            return "invalid", None
    matches = [
        entry
        for entry in entries
        if entry["gene"] == normalized_gene
        and entry["disease_ids"] == normalized_diseases
    ]
    if not matches:
        return "no_match", None
    matches.sort(key=lambda item: item["cache_stored_at"], reverse=True)
    return "success", matches[0]


def store_cspec_lkg(
    path: Path,
    *,
    gene: str,
    disease_ids: list[str],
    source_retrieved_at: str,
    cache_stored_at: str,
    specifications: list[dict[str, Any]],
) -> None:
    """Atomically replace one exact last-known-good CSpec cache entry."""

    candidate = _entry(
        {
            "gene": gene,
            "disease_ids": disease_ids,
            "source_retrieved_at": source_retrieved_at,
            "cache_stored_at": cache_stored_at,
            "specifications": specifications,
        }
    )
    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _CACHE_LOCK:
        try:
            entries = _read(path)
        except (FileNotFoundError, CSpecCacheError):
            entries = []
        entries = [
            entry
            for entry in entries
            if not (
                entry["gene"] == candidate["gene"]
                and entry["disease_ids"] == candidate["disease_ids"]
            )
        ]
        entries.append(candidate)
        entries.sort(key=lambda item: item["cache_stored_at"], reverse=True)
        retained = entries[:MAX_CSPEC_CACHE_ENTRIES]
        while retained:
            payload = {
                "schema_version": CSPEC_CACHE_SCHEMA_VERSION,
                "entries": retained,
            }
            encoded = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
            )
            if len(encoded.encode("utf-8")) <= MAX_CSPEC_CACHE_BYTES:
                break
            retained.pop()
        if not retained:
            raise CSpecCacheError("CSpec cache entry exceeds the size limit.")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise CSpecCacheError("Could not persist the CSpec cache.") from exc
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass


__all__ = [
    "CSPEC_CACHE_SCHEMA_VERSION",
    "CSpecCacheError",
    "load_cspec_lkg",
    "store_cspec_lkg",
]
