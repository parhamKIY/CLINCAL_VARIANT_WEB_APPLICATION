"""Phenotype and Human Phenotype Ontology input handling."""

import os
import re
import shutil
import tempfile
from datetime import date
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import TypedDict

import requests

from config import settings


HPO_ID_PATTERN = re.compile(r"HP:[0-9]{7}")
HPO_RELEASE_PATTERN = re.compile(
    r"hp/releases/([0-9]{4}-[0-9]{2}-[0-9]{2})"
)
HPO_ONTOLOGY_FILENAME = "hp.obo"
MAX_HPO_ONTOLOGY_BYTES = 50 * 1024 * 1024
MIN_HPO_ACTIVE_TERMS = 10_000
HPO_DOWNLOAD_CHUNK_SIZE = 64 * 1024
_HPO_UPDATE_LOCK = Lock()


class PhenotypeError(ValueError):
    """Raised when phenotype input is invalid."""


class HPODataError(RuntimeError):
    """Raised when the configured HPO ontology cannot be loaded."""


class HPOTerm(TypedDict):
    """Minimal ontology term exposed to later pipeline stages."""

    id: str
    name: str


class HPOUpdateResult(TypedDict):
    """JSON-serializable result returned to a future frontend button."""

    status: str
    previous_version: str | None
    current_version: str
    active_term_count: int
    lookup_id_count: int
    backup_path: str | None


def validate_hpo_id(hpo_id: str) -> str:
    """Validate and return one canonical Human Phenotype Ontology ID.

    This function validates syntax only. Confirming that the term exists in
    the configured HPO dataset belongs to the next phenotype implementation
    step.
    """
    if not isinstance(hpo_id, str):
        raise PhenotypeError(
            "HPO ID must be a string in HP:0000000 format."
        )

    normalized_id = hpo_id.strip()
    if HPO_ID_PATTERN.fullmatch(normalized_id) is None:
        raise PhenotypeError(
            "HPO ID must use HP: followed by exactly seven digits."
        )

    return normalized_id


def _store_hpo_term(
    index: dict[str, HPOTerm],
    identifier: str | None,
    name: str | None,
    alternate_ids: list[str],
    is_obsolete: bool,
) -> None:
    """Store one active OBO term and its aliases in the lookup index."""
    if identifier is None:
        return

    if HPO_ID_PATTERN.fullmatch(identifier) is None:
        raise HPODataError(
            f"HPO ontology contains an invalid term ID: {identifier}."
        )

    if is_obsolete:
        return

    if name is None or not name.strip():
        raise HPODataError(
            f"HPO ontology term {identifier} has no name."
        )

    term: HPOTerm = {
        "id": identifier,
        "name": name.strip(),
    }
    for lookup_id in (identifier, *alternate_ids):
        if HPO_ID_PATTERN.fullmatch(lookup_id) is None:
            raise HPODataError(
                "HPO ontology contains an invalid alternate ID: "
                f"{lookup_id}."
            )

        existing = index.get(lookup_id)
        if existing is not None and existing["id"] != identifier:
            raise HPODataError(
                f"HPO ontology contains duplicate ID {lookup_id}."
            )
        index[lookup_id] = term


@lru_cache(maxsize=4)
def _load_hpo_index(ontology_path: Path) -> dict[str, HPOTerm]:
    """Load active HPO terms from one local OBO ontology file."""
    try:
        ontology_file = ontology_path.open(
            "r",
            encoding="utf-8",
        )
    except OSError as exc:
        raise HPODataError(
            f"Unable to open HPO ontology: {ontology_path}."
        ) from exc

    index: dict[str, HPOTerm] = {}
    in_term = False
    identifier: str | None = None
    name: str | None = None
    alternate_ids: list[str] = []
    is_obsolete = False

    def store_current_term() -> None:
        _store_hpo_term(
            index,
            identifier,
            name,
            alternate_ids,
            is_obsolete,
        )

    try:
        with ontology_file:
            for raw_line in ontology_file:
                line = raw_line.strip()

                if line == "[Term]":
                    if in_term:
                        store_current_term()
                    in_term = True
                    identifier = None
                    name = None
                    alternate_ids = []
                    is_obsolete = False
                    continue

                if line.startswith("["):
                    if in_term:
                        store_current_term()
                    in_term = False
                    continue

                if not in_term:
                    continue

                field, separator, value = line.partition(":")
                if not separator:
                    continue
                value = value.strip()

                if field == "id":
                    identifier = value
                elif field == "name":
                    name = value
                elif field == "alt_id":
                    alternate_ids.append(value)
                elif field == "is_obsolete":
                    is_obsolete = value.casefold() == "true"

            if in_term:
                store_current_term()
    except (OSError, UnicodeError) as exc:
        raise HPODataError(
            f"Unable to read HPO ontology: {ontology_path}."
        ) from exc

    if not index:
        raise HPODataError(
            f"HPO ontology contains no active terms: {ontology_path}."
        )

    return index


def lookup_hpo_term(
    hpo_id: str,
    *,
    ontology_path: str | Path | None = None,
) -> HPOTerm:
    """Return the canonical ID and name for one existing HPO term."""
    normalized_id = validate_hpo_id(hpo_id)
    resolved_path = (
        Path(ontology_path)
        if ontology_path is not None
        else settings.HPO_DATA_DIR / HPO_ONTOLOGY_FILENAME
    ).resolve()
    term = _load_hpo_index(resolved_path).get(normalized_id)

    if term is None:
        raise PhenotypeError(
            f"HPO ID {normalized_id} was not found in the ontology."
        )

    return {
        "id": term["id"],
        "name": term["name"],
    }


def _read_hpo_release(
    ontology_path: Path,
    *,
    required: bool,
) -> tuple[str | None, date | None]:
    """Read and validate the release identifier in an OBO header."""
    try:
        with ontology_path.open("r", encoding="utf-8") as ontology_file:
            for raw_line in ontology_file:
                line = raw_line.strip()
                if line == "[Term]":
                    break
                if not line.startswith("data-version:"):
                    continue

                version = line.partition(":")[2].strip()
                match = HPO_RELEASE_PATTERN.fullmatch(version)
                if match is None:
                    raise HPODataError(
                        "HPO ontology has an invalid data-version."
                    )

                try:
                    release_date = date.fromisoformat(match.group(1))
                except ValueError as exc:
                    raise HPODataError(
                        "HPO ontology has an invalid release date."
                    ) from exc
                return version, release_date
    except (OSError, UnicodeError) as exc:
        raise HPODataError(
            f"Unable to read HPO ontology header: {ontology_path}."
        ) from exc

    if required:
        raise HPODataError(
            "Downloaded HPO ontology has no data-version."
        )
    return None, None


def _download_hpo_ontology(
    session: requests.Session,
    source_url: str,
    destination: Path,
) -> None:
    """Stream one bounded ontology download into a temporary file."""
    try:
        response = session.get(
            source_url,
            headers={"Accept": "text/plain"},
            stream=True,
            timeout=settings.REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HPODataError(
            f"HPO ontology download failed: {exc}"
        ) from exc

    try:
        if not 200 <= response.status_code < 300:
            raise HPODataError(
                "HPO ontology download returned HTTP "
                f"{response.status_code}."
            )

        raw_content_length = response.headers.get("Content-Length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError as exc:
                raise HPODataError(
                    "HPO ontology download returned an invalid size."
                ) from exc
            if (
                content_length < 1
                or content_length > MAX_HPO_ONTOLOGY_BYTES
            ):
                raise HPODataError(
                    "HPO ontology download size is outside the "
                    "allowed range."
                )

        downloaded_bytes = 0
        try:
            with destination.open("wb") as ontology_file:
                for chunk in response.iter_content(
                    chunk_size=HPO_DOWNLOAD_CHUNK_SIZE
                ):
                    if not chunk:
                        continue
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > MAX_HPO_ONTOLOGY_BYTES:
                        raise HPODataError(
                            "HPO ontology download exceeded the "
                            "maximum size."
                        )
                    ontology_file.write(chunk)
        except requests.RequestException as exc:
            raise HPODataError(
                f"HPO ontology download failed: {exc}"
            ) from exc
        except OSError as exc:
            raise HPODataError(
                f"Unable to save HPO ontology: {destination}."
            ) from exc

        if downloaded_bytes == 0:
            raise HPODataError(
                "HPO ontology download was empty."
            )
    finally:
        response.close()


def _perform_hpo_update(
    session: requests.Session,
    source_url: str,
    ontology_path: Path,
) -> HPOUpdateResult:
    """Download, validate, and atomically install one HPO release."""
    ontology_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=ontology_path.parent,
        prefix=".hp-",
        suffix=".obo.download",
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)

    try:
        _download_hpo_ontology(
            session,
            source_url,
            temporary_path,
        )
        current_version, current_release_date = _read_hpo_release(
            temporary_path,
            required=True,
        )
        if current_version is None or current_release_date is None:
            raise HPODataError(
                "Downloaded HPO ontology has no valid release."
            )
        downloaded_index = _load_hpo_index(temporary_path)
        active_term_count = len(
            {
                term["id"]
                for term in downloaded_index.values()
            }
        )
        if active_term_count < MIN_HPO_ACTIVE_TERMS:
            raise HPODataError(
                "Downloaded HPO ontology has too few active terms."
            )

        previous_version: str | None = None
        previous_release_date: date | None = None
        if ontology_path.exists():
            previous_version, previous_release_date = _read_hpo_release(
                ontology_path,
                required=False,
            )

        if (
            previous_release_date is not None
            and current_release_date < previous_release_date
        ):
            raise HPODataError(
                "Downloaded HPO release is older than the installed release."
            )

        if current_version == previous_version:
            return {
                "status": "unchanged",
                "previous_version": previous_version,
                "current_version": current_version,
                "active_term_count": active_term_count,
                "lookup_id_count": len(downloaded_index),
                "backup_path": None,
            }

        backup_path: Path | None = None
        if ontology_path.exists():
            backup_path = ontology_path.with_suffix(".previous.obo")
            try:
                shutil.copy2(ontology_path, backup_path)
            except OSError as exc:
                raise HPODataError(
                    "Unable to create the previous HPO release backup."
                ) from exc

        try:
            os.replace(temporary_path, ontology_path)
        except OSError as exc:
            raise HPODataError(
                "Unable to install the downloaded HPO ontology."
            ) from exc
        _load_hpo_index.cache_clear()

        return {
            "status": "updated",
            "previous_version": previous_version,
            "current_version": current_version,
            "active_term_count": active_term_count,
            "lookup_id_count": len(downloaded_index),
            "backup_path": (
                str(backup_path)
                if backup_path is not None
                else None
            ),
        }
    finally:
        _load_hpo_index.cache_clear()
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def update_hpo_ontology(
    *,
    ontology_path: str | Path | None = None,
    source_url: str | None = None,
    session: requests.Session | None = None,
) -> HPOUpdateResult:
    """Safely refresh the local HPO ontology from the configured source."""
    resolved_path = (
        Path(ontology_path)
        if ontology_path is not None
        else settings.HPO_DATA_DIR / HPO_ONTOLOGY_FILENAME
    ).resolve()
    resolved_url = (
        source_url
        if source_url is not None
        else settings.HPO_ONTOLOGY_URL
    ).strip()
    if not resolved_url.lower().startswith("https://"):
        raise HPODataError(
            "HPO ontology updates require an HTTPS source URL."
        )

    active_session = session or requests.Session()
    owns_session = session is None
    try:
        with _HPO_UPDATE_LOCK:
            return _perform_hpo_update(
                active_session,
                resolved_url,
                resolved_path,
            )
    finally:
        if owns_session:
            active_session.close()
