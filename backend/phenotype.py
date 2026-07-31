"""Phenotype and Human Phenotype Ontology input handling."""

import csv
import hashlib
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterable
from datetime import date
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict

import requests

from config import settings


HPO_ID_PATTERN = re.compile(r"HP:[0-9]{7}")
HPO_SYNONYM_PATTERN = re.compile(
    r'^"((?:[^"\\]|\\.)*)"\s+[A-Z]+\s+'
)
HPO_RELEASE_PATTERN = re.compile(
    r"hp/releases/([0-9]{4}-[0-9]{2}-[0-9]{2})"
)
HPO_ONTOLOGY_FILENAME = "hp.obo"
HPO_GENE_ASSOCIATIONS_FILENAME = "phenotype_to_genes.txt"
HPO_DISEASE_ANNOTATIONS_FILENAME = "phenotype.hpoa"
HPO_GENE_ASSOCIATION_HEADER = (
    "hpo_id",
    "hpo_name",
    "ncbi_gene_id",
    "gene_symbol",
    "disease_id",
)
HPO_DISEASE_ANNOTATION_HEADER = (
    "database_id",
    "disease_name",
    "qualifier",
    "hpo_id",
    "reference",
    "evidence",
    "onset",
    "frequency",
    "sex",
    "modifier",
    "aspect",
    "biocuration",
)
HPO_ANNOTATION_ASPECTS = {"P", "C", "I", "H", "M"}
MAX_HPO_ONTOLOGY_BYTES = 50 * 1024 * 1024
MAX_HPO_GENE_ASSOCIATIONS_BYTES = 100 * 1024 * 1024
MAX_HPO_DISEASE_ANNOTATIONS_BYTES = 75 * 1024 * 1024
MIN_HPO_ACTIVE_TERMS = 10_000
MIN_HPO_GENE_ASSOCIATION_TERMS = 10_000
MIN_HPO_DISEASE_ANNOTATION_TERMS = 10_000
MAX_PATIENT_HPO_TERMS = 50
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


class HPOSearchCandidate(TypedDict):
    """One ontology-backed suggestion for physician-entered text."""

    id: str
    name: str
    matched_label: str
    match_type: str


class HPOUpdateResult(TypedDict):
    """JSON-serializable result returned to a future frontend button."""

    status: str
    previous_version: str | None
    current_version: str
    active_term_count: int
    lookup_id_count: int
    backup_path: str | None


class HPOGeneResult(TypedDict):
    """Standardized phenotype-to-gene lookup result."""

    hpo_term: HPOTerm
    genes: list[str]
    gene_count: int


class HPOGeneSimilarityResult(TypedDict):
    """Explainable exact-overlap phenotype score for one gene."""

    hpo_terms: list[str]
    gene: str
    matched_hpo_terms: list[str]
    match_count: int
    phenotype_score: float


class HPODisease(TypedDict):
    """Minimal disease association exposed to later stages."""

    id: str
    name: str


class HPODiseaseResult(TypedDict):
    """Standardized phenotype-to-disease lookup result."""

    hpo_term: HPOTerm
    diseases: list[HPODisease]
    disease_count: int


class HPODataUpdateResult(TypedDict):
    """Result of a coordinated ontology and association refresh."""

    status: str
    previous_version: str | None
    current_version: str
    active_term_count: int
    ontology_lookup_id_count: int
    association_term_count: int
    associated_gene_count: int
    disease_annotation_term_count: int
    associated_disease_count: int
    ontology_backup_path: str | None
    associations_backup_path: str | None
    disease_annotations_backup_path: str | None


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


def _normalize_hpo_search_text(value: str) -> str:
    """Normalize human-entered phenotype text for local matching."""
    return " ".join(
        re.sub(r"[^\w]+", " ", value.casefold()).split()
    )


def _decode_hpo_synonym(value: str, hpo_id: str) -> str:
    """Extract one quoted synonym from an OBO synonym field."""
    match = HPO_SYNONYM_PATTERN.match(value)
    if match is None:
        raise HPODataError(
            f"HPO ontology term {hpo_id} has a malformed synonym."
        )
    return re.sub(r"\\(.)", r"\1", match.group(1)).strip()


@lru_cache(maxsize=4)
def _load_hpo_search_index(
    ontology_path: Path,
) -> tuple[tuple[HPOTerm, tuple[str, ...]], ...]:
    """Load canonical active terms and searchable synonyms from OBO."""
    ontology_index = _load_hpo_index(ontology_path)
    entries: list[tuple[HPOTerm, tuple[str, ...]]] = []
    identifier: str | None = None
    synonyms: list[str] = []
    in_term = False

    def store_current_term() -> None:
        if identifier is None:
            return
        term = ontology_index.get(identifier)
        if term is None or term["id"] != identifier:
            return
        unique_synonyms = tuple(
            sorted(
                {
                    synonym
                    for synonym in synonyms
                    if synonym
                    and synonym.casefold() != term["name"].casefold()
                },
                key=lambda synonym: synonym.casefold(),
            )
        )
        entries.append((term, unique_synonyms))

    try:
        with ontology_path.open("r", encoding="utf-8") as ontology_file:
            for raw_line in ontology_file:
                line = raw_line.strip()
                if line == "[Term]":
                    if in_term:
                        store_current_term()
                    in_term = True
                    identifier = None
                    synonyms = []
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
                elif field == "synonym" and identifier is not None:
                    synonyms.append(
                        _decode_hpo_synonym(value, identifier)
                    )

            if in_term:
                store_current_term()
    except (OSError, UnicodeError) as exc:
        raise HPODataError(
            f"Unable to read HPO ontology: {ontology_path}."
        ) from exc

    entries.sort(
        key=lambda entry: (
            entry[0]["name"].casefold(),
            entry[0]["id"],
        )
    )
    return tuple(entries)


def search_hpo_terms(
    text: str,
    *,
    limit: int = 10,
    ontology_path: str | Path | None = None,
) -> list[HPOSearchCandidate]:
    """Return deterministic local HPO suggestions for one text phrase."""
    if not isinstance(text, str):
        raise PhenotypeError(
            "Phenotype search text must be a string."
        )
    query = _normalize_hpo_search_text(text)
    if not query:
        raise PhenotypeError(
            "Phenotype search text must not be empty."
        )
    if len(text) > 200:
        raise PhenotypeError(
            "Phenotype search text must not exceed 200 characters."
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise PhenotypeError("HPO search limit must be an integer.")
    if not 1 <= limit <= 50:
        raise PhenotypeError(
            "HPO search limit must be between 1 and 50."
        )

    resolved_path = (
        Path(ontology_path)
        if ontology_path is not None
        else settings.HPO_DATA_DIR / HPO_ONTOLOGY_FILENAME
    ).resolve()
    stripped_text = text.strip()
    if HPO_ID_PATTERN.fullmatch(stripped_text) is not None:
        term = lookup_hpo_term(
            stripped_text,
            ontology_path=resolved_path,
        )
        return [
            {
                "id": term["id"],
                "name": term["name"],
                "matched_label": stripped_text,
                "match_type": "id",
            }
        ]

    query_tokens = set(query.split())
    ranked: list[
        tuple[int, int, str, str, HPOSearchCandidate]
    ] = []

    for term, synonyms in _load_hpo_search_index(resolved_path):
        best_match: tuple[int, int, str, str] | None = None
        for match_type, label in (
            ("name", term["name"]),
            *(("synonym", synonym) for synonym in synonyms),
        ):
            normalized_label = _normalize_hpo_search_text(label)
            if normalized_label == query:
                rank = 0 if match_type == "name" else 1
            elif normalized_label.startswith(query):
                rank = 2 if match_type == "name" else 3
            elif query in normalized_label:
                rank = 4 if match_type == "name" else 5
            elif query_tokens.issubset(set(normalized_label.split())):
                rank = 6 if match_type == "name" else 7
            else:
                continue

            candidate_rank = (
                rank,
                abs(len(normalized_label) - len(query)),
                label.casefold(),
                label,
            )
            if best_match is None or candidate_rank < best_match:
                best_match = candidate_rank

        if best_match is None:
            continue
        rank, length_difference, _, matched_label = best_match
        candidate: HPOSearchCandidate = {
            "id": term["id"],
            "name": term["name"],
            "matched_label": matched_label,
            "match_type": (
                "name"
                if matched_label == term["name"]
                else "synonym"
            ),
        }
        ranked.append(
            (
                rank,
                length_difference,
                term["name"].casefold(),
                term["id"],
                candidate,
            )
        )

    ranked.sort(key=lambda item: item[:4])
    return [item[4] for item in ranked[:limit]]


def normalize_phenotypes(
    hpo_ids: list[str] | tuple[str, ...],
    *,
    ontology_path: str | Path | None = None,
) -> list[HPOTerm]:
    """Validate and canonicalize a patient's selected HPO terms."""
    if not isinstance(hpo_ids, (list, tuple)):
        raise PhenotypeError(
            "Phenotypes must be provided as a list of HPO IDs."
        )
    if not hpo_ids:
        raise PhenotypeError(
            "At least one HPO phenotype must be provided."
        )
    if len(hpo_ids) > MAX_PATIENT_HPO_TERMS:
        raise PhenotypeError(
            "No more than 50 HPO phenotypes may be provided."
        )

    normalized_terms: list[HPOTerm] = []
    seen_ids: set[str] = set()
    for hpo_id in hpo_ids:
        term = lookup_hpo_term(
            hpo_id,
            ontology_path=ontology_path,
        )
        if term["id"] in seen_ids:
            continue
        seen_ids.add(term["id"])
        normalized_terms.append(term)

    return normalized_terms


@lru_cache(maxsize=4)
def _load_hpo_gene_index(
    associations_path: Path,
) -> dict[str, tuple[str, ...]]:
    """Load a deterministic HPO-to-gene index from the official TSV."""
    try:
        association_file = associations_path.open(
            "r",
            encoding="utf-8",
            newline="",
        )
    except OSError as exc:
        raise HPODataError(
            "Unable to open HPO gene associations: "
            f"{associations_path}."
        ) from exc

    genes_by_hpo: dict[str, set[str]] = {}
    try:
        with association_file:
            rows = csv.reader(association_file, delimiter="\t")
            header = next(rows, None)
            if tuple(header or ()) != HPO_GENE_ASSOCIATION_HEADER:
                raise HPODataError(
                    "HPO gene associations have an unexpected header."
                )

            for line_number, row in enumerate(rows, start=2):
                if len(row) != len(HPO_GENE_ASSOCIATION_HEADER):
                    raise HPODataError(
                        "HPO gene association row "
                        f"{line_number} has an unexpected structure."
                    )

                hpo_id, hpo_name, ncbi_gene_id, gene_symbol, _ = (
                    value.strip()
                    for value in row
                )
                if HPO_ID_PATTERN.fullmatch(hpo_id) is None:
                    raise HPODataError(
                        "HPO gene association row "
                        f"{line_number} has an invalid HPO ID."
                    )
                if (
                    not hpo_name
                    or not ncbi_gene_id.isdigit()
                    or not gene_symbol
                ):
                    raise HPODataError(
                        "HPO gene association row "
                        f"{line_number} is incomplete."
                    )

                genes_by_hpo.setdefault(hpo_id, set()).add(
                    gene_symbol
                )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise HPODataError(
            "Unable to read HPO gene associations: "
            f"{associations_path}."
        ) from exc

    if not genes_by_hpo:
        raise HPODataError(
            "HPO gene associations contain no records: "
            f"{associations_path}."
        )

    return {
        hpo_id: tuple(sorted(gene_symbols))
        for hpo_id, gene_symbols in genes_by_hpo.items()
    }


def get_genes_for_hpo(
    hpo_id: str,
    *,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
) -> HPOGeneResult:
    """Return unique genes associated with one existing HPO term."""
    term = lookup_hpo_term(
        hpo_id,
        ontology_path=ontology_path,
    )
    resolved_associations_path = (
        Path(associations_path)
        if associations_path is not None
        else (
            settings.HPO_DATA_DIR
            / HPO_GENE_ASSOCIATIONS_FILENAME
        )
    ).resolve()
    genes = list(
        _load_hpo_gene_index(resolved_associations_path).get(
            term["id"],
            (),
        )
    )

    return {
        "hpo_term": term,
        "genes": genes,
        "gene_count": len(genes),
    }


def calculate_hpo_similarity(
    hpo_ids: list[str] | tuple[str, ...],
    gene: str,
    *,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
) -> HPOGeneSimilarityResult:
    """Score exact HPO overlap between a patient and one gene."""
    if not isinstance(gene, str):
        raise PhenotypeError("Gene symbol must be a string.")
    requested_gene = gene.strip()
    if not requested_gene:
        raise PhenotypeError("Gene symbol must not be empty.")
    if (
        len(requested_gene) > 64
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*",
            requested_gene,
        )
        is None
    ):
        raise PhenotypeError("Gene symbol has an invalid format.")

    terms = normalize_phenotypes(
        hpo_ids,
        ontology_path=ontology_path,
    )
    resolved_associations_path = (
        Path(associations_path)
        if associations_path is not None
        else (
            settings.HPO_DATA_DIR
            / HPO_GENE_ASSOCIATIONS_FILENAME
        )
    ).resolve()
    gene_index = _load_hpo_gene_index(resolved_associations_path)
    requested_gene_key = requested_gene.casefold()
    canonical_gene = requested_gene.upper()
    matched_hpo_terms: list[str] = []

    for term in terms:
        matching_gene = next(
            (
                gene_symbol
                for gene_symbol in gene_index.get(term["id"], ())
                if gene_symbol.casefold() == requested_gene_key
            ),
            None,
        )
        if matching_gene is None:
            continue
        canonical_gene = matching_gene
        matched_hpo_terms.append(term["id"])

    return {
        "hpo_terms": [term["id"] for term in terms],
        "gene": canonical_gene,
        "matched_hpo_terms": matched_hpo_terms,
        "match_count": len(matched_hpo_terms),
        "phenotype_score": round(
            len(matched_hpo_terms) / len(terms),
            4,
        ),
    }


def match_phenotypes(
    annotations: Iterable[dict[str, Any]],
    hpo_ids: list[str] | tuple[str, ...],
    *,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Attach explainable phenotype scores to annotated candidates."""
    if isinstance(annotations, (str, bytes, dict)):
        raise PhenotypeError(
            "Annotations must be an iterable of dictionaries."
        )
    try:
        iterator = iter(annotations)
    except TypeError as exc:
        raise PhenotypeError(
            "Annotations must be an iterable of dictionaries."
        ) from exc

    terms = normalize_phenotypes(
        hpo_ids,
        ontology_path=ontology_path,
    )
    canonical_hpo_ids = [term["id"] for term in terms]
    matched_annotations: list[dict[str, Any]] = []

    for index, annotation in enumerate(iterator):
        if not isinstance(annotation, dict):
            raise PhenotypeError(
                f"Annotation at index {index} must be a dictionary."
            )

        scored_annotation = dict(annotation)
        gene = annotation.get("gene")
        if not isinstance(gene, str) or not gene.strip():
            scored_annotation.update(
                {
                    "hpo_terms": list(canonical_hpo_ids),
                    "matched_hpo_terms": [],
                    "phenotype_match_count": 0,
                    "phenotype_score": 0.0,
                }
            )
            matched_annotations.append(scored_annotation)
            continue

        similarity = calculate_hpo_similarity(
            canonical_hpo_ids,
            gene,
            ontology_path=ontology_path,
            associations_path=associations_path,
        )
        scored_annotation.update(
            {
                "hpo_terms": similarity["hpo_terms"],
                "matched_hpo_terms": similarity[
                    "matched_hpo_terms"
                ],
                "phenotype_match_count": similarity["match_count"],
                "phenotype_score": similarity["phenotype_score"],
            }
        )
        matched_annotations.append(scored_annotation)

    return matched_annotations


@lru_cache(maxsize=4)
def _load_hpo_disease_index(
    annotations_path: Path,
) -> dict[str, tuple[HPODisease, ...]]:
    """Load positive phenotypic-abnormality disease annotations."""
    try:
        annotation_file = annotations_path.open(
            "r",
            encoding="utf-8",
            newline="",
        )
    except OSError as exc:
        raise HPODataError(
            "Unable to open HPO disease annotations: "
            f"{annotations_path}."
        ) from exc

    diseases_by_hpo: dict[
        str,
        dict[str, Counter[str]],
    ] = {}
    try:
        with annotation_file:
            data_lines = (
                line
                for line in annotation_file
                if not line.startswith("#")
            )
            rows = csv.reader(data_lines, delimiter="\t")
            header = next(rows, None)
            if tuple(header or ()) != HPO_DISEASE_ANNOTATION_HEADER:
                raise HPODataError(
                    "HPO disease annotations have an unexpected header."
                )

            for line_number, row in enumerate(rows, start=2):
                if len(row) != len(HPO_DISEASE_ANNOTATION_HEADER):
                    raise HPODataError(
                        "HPO disease annotation row "
                        f"{line_number} has an unexpected structure."
                    )

                (
                    database_id,
                    disease_name,
                    qualifier,
                    hpo_id,
                    reference,
                    evidence,
                    _,
                    _,
                    _,
                    _,
                    aspect,
                    biocuration,
                ) = (value.strip() for value in row)
                if HPO_ID_PATTERN.fullmatch(hpo_id) is None:
                    raise HPODataError(
                        "HPO disease annotation row "
                        f"{line_number} has an invalid HPO ID."
                    )
                if qualifier not in {"", "NOT"}:
                    raise HPODataError(
                        "HPO disease annotation row "
                        f"{line_number} has an invalid qualifier."
                    )
                if aspect not in HPO_ANNOTATION_ASPECTS:
                    raise HPODataError(
                        "HPO disease annotation row "
                        f"{line_number} has an invalid aspect."
                    )
                if (
                    not database_id
                    or not disease_name
                    or not reference
                    or not evidence
                    or not biocuration
                ):
                    raise HPODataError(
                        "HPO disease annotation row "
                        f"{line_number} is incomplete."
                    )

                # Negated findings and non-phenotypic branches must not
                # contribute positive phenotype-to-disease evidence.
                if qualifier == "NOT" or aspect != "P":
                    continue

                hpo_diseases = diseases_by_hpo.setdefault(
                    hpo_id,
                    {},
                )
                hpo_diseases.setdefault(
                    database_id,
                    Counter(),
                )[disease_name] += 1
    except (OSError, UnicodeError, csv.Error) as exc:
        raise HPODataError(
            "Unable to read HPO disease annotations: "
            f"{annotations_path}."
        ) from exc

    if not diseases_by_hpo:
        raise HPODataError(
            "HPO disease annotations contain no positive records: "
            f"{annotations_path}."
        )

    return {
        hpo_id: tuple(
            {
                "id": disease_id,
                "name": min(
                    name_counts,
                    key=lambda name: (
                        -name_counts[name],
                        name,
                    ),
                ),
            }
            for disease_id, name_counts in sorted(diseases.items())
        )
        for hpo_id, diseases in diseases_by_hpo.items()
    }


def get_diseases_for_hpo(
    hpo_id: str,
    *,
    ontology_path: str | Path | None = None,
    annotations_path: str | Path | None = None,
) -> HPODiseaseResult:
    """Return unique positive disease associations for one HPO term."""
    term = lookup_hpo_term(
        hpo_id,
        ontology_path=ontology_path,
    )
    resolved_annotations_path = (
        Path(annotations_path)
        if annotations_path is not None
        else (
            settings.HPO_DATA_DIR
            / HPO_DISEASE_ANNOTATIONS_FILENAME
        )
    ).resolve()
    diseases = [
        {
            "id": disease["id"],
            "name": disease["name"],
        }
        for disease in _load_hpo_disease_index(
            resolved_annotations_path
        ).get(term["id"], ())
    ]

    return {
        "hpo_term": term,
        "diseases": diseases,
        "disease_count": len(diseases),
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


def _download_hpo_file(
    session: requests.Session,
    source_url: str,
    destination: Path,
    *,
    maximum_bytes: int,
    data_label: str,
) -> None:
    """Stream one bounded HPO data download into a temporary file."""
    try:
        response = session.get(
            source_url,
            headers={"Accept": "text/plain"},
            stream=True,
            timeout=settings.REQUEST_TIMEOUT,
            verify=True,
        )
    except requests.RequestException as exc:
        raise HPODataError(
            f"{data_label} download failed: {exc}"
        ) from exc

    try:
        if not 200 <= response.status_code < 300:
            raise HPODataError(
                f"{data_label} download returned HTTP "
                f"{response.status_code}."
            )

        raw_content_length = response.headers.get("Content-Length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError as exc:
                raise HPODataError(
                    f"{data_label} download returned an invalid size."
                ) from exc
            if (
                content_length < 1
                or content_length > maximum_bytes
            ):
                raise HPODataError(
                    f"{data_label} download size is outside the "
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
                    if downloaded_bytes > maximum_bytes:
                        raise HPODataError(
                            f"{data_label} download exceeded the "
                            "maximum size."
                        )
                    ontology_file.write(chunk)
        except requests.RequestException as exc:
            raise HPODataError(
                f"{data_label} download failed: {exc}"
            ) from exc
        except OSError as exc:
            raise HPODataError(
                f"Unable to save {data_label}: {destination}."
            ) from exc

        if downloaded_bytes == 0:
            raise HPODataError(
                f"{data_label} download was empty."
            )
    finally:
        response.close()


def _download_hpo_ontology(
    session: requests.Session,
    source_url: str,
    destination: Path,
) -> None:
    """Stream one bounded ontology download into a temporary file."""
    _download_hpo_file(
        session,
        source_url,
        destination,
        maximum_bytes=MAX_HPO_ONTOLOGY_BYTES,
        data_label="HPO ontology",
    )


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
        _load_hpo_search_index.cache_clear()

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
        _load_hpo_search_index.cache_clear()
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


def _sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for one local data file."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as data_file:
            for chunk in iter(
                lambda: data_file.read(HPO_DOWNLOAD_CHUNK_SIZE),
                b"",
            ):
                digest.update(chunk)
    except OSError as exc:
        raise HPODataError(
            f"Unable to verify HPO data file: {path}."
        ) from exc
    return digest.hexdigest()


def _same_file_content(
    installed_path: Path,
    downloaded_path: Path,
) -> bool:
    """Compare two HPO data files without loading either fully."""
    if not installed_path.exists():
        return False
    try:
        if (
            installed_path.stat().st_size
            != downloaded_path.stat().st_size
        ):
            return False
    except OSError as exc:
        raise HPODataError(
            "Unable to inspect installed HPO data."
        ) from exc
    return _sha256_file(installed_path) == _sha256_file(
        downloaded_path
    )


def _create_hpo_temporary_file(
    parent: Path,
    suffix: str,
) -> Path:
    """Create one closed temporary file beside its install target."""
    parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=".hpo-",
        suffix=suffix,
    )
    os.close(file_descriptor)
    return Path(temporary_name)


def _backup_hpo_file(
    path: Path,
    backup_path: Path,
) -> Path | None:
    """Create a recoverable backup when an installed file exists."""
    if not path.exists():
        return None
    try:
        shutil.copy2(path, backup_path)
    except OSError as exc:
        raise HPODataError(
            f"Unable to back up HPO data file: {path}."
        ) from exc
    return backup_path


def _restore_hpo_file(
    path: Path,
    backup_path: Path | None,
) -> None:
    """Restore one target after a coordinated installation failure."""
    try:
        if backup_path is None:
            path.unlink(missing_ok=True)
        else:
            shutil.copy2(backup_path, path)
    except OSError as exc:
        raise HPODataError(
            f"Unable to restore HPO data file: {path}."
        ) from exc


def _perform_hpo_data_update(
    session: requests.Session,
    ontology_source_url: str,
    association_url_template: str,
    disease_url_template: str,
    ontology_path: Path,
    associations_path: Path,
    disease_annotations_path: Path,
) -> HPODataUpdateResult:
    """Validate and install one matching HPO data release."""
    temporary_ontology = _create_hpo_temporary_file(
        ontology_path.parent,
        ".obo.download",
    )
    temporary_associations = _create_hpo_temporary_file(
        associations_path.parent,
        ".txt.download",
    )
    temporary_disease_annotations = _create_hpo_temporary_file(
        disease_annotations_path.parent,
        ".hpoa.download",
    )

    try:
        _download_hpo_ontology(
            session,
            ontology_source_url,
            temporary_ontology,
        )
        current_version, current_release_date = _read_hpo_release(
            temporary_ontology,
            required=True,
        )
        if current_version is None or current_release_date is None:
            raise HPODataError(
                "Downloaded HPO ontology has no valid release."
        )

        release_text = current_release_date.isoformat()
        try:
            association_source_url = association_url_template.format(
                release=release_text
            )
        except (KeyError, ValueError) as exc:
            raise HPODataError(
                "HPO gene association URL template is invalid."
            ) from exc
        if not association_source_url.lower().startswith("https://"):
            raise HPODataError(
                "HPO gene association updates require HTTPS."
            )
        _download_hpo_file(
            session,
            association_source_url,
            temporary_associations,
            maximum_bytes=MAX_HPO_GENE_ASSOCIATIONS_BYTES,
            data_label="HPO gene associations",
        )
        try:
            disease_source_url = disease_url_template.format(
                release=release_text
            )
        except (KeyError, ValueError) as exc:
            raise HPODataError(
                "HPO disease annotation URL template is invalid."
            ) from exc
        if not disease_source_url.lower().startswith("https://"):
            raise HPODataError(
                "HPO disease annotation updates require HTTPS."
            )
        _download_hpo_file(
            session,
            disease_source_url,
            temporary_disease_annotations,
            maximum_bytes=MAX_HPO_DISEASE_ANNOTATIONS_BYTES,
            data_label="HPO disease annotations",
        )

        ontology_index = _load_hpo_index(temporary_ontology)
        active_term_count = len(
            {
                term["id"]
                for term in ontology_index.values()
            }
        )
        if active_term_count < MIN_HPO_ACTIVE_TERMS:
            raise HPODataError(
                "Downloaded HPO ontology has too few active terms."
            )

        association_index = _load_hpo_gene_index(
            temporary_associations
        )
        if (
            len(association_index)
            < MIN_HPO_GENE_ASSOCIATION_TERMS
        ):
            raise HPODataError(
                "Downloaded HPO gene associations contain too few terms."
            )

        unknown_association_ids = (
            association_index.keys() - ontology_index.keys()
        )
        if unknown_association_ids:
            example_id = min(unknown_association_ids)
            raise HPODataError(
                "HPO gene associations do not match the ontology; "
                f"unknown term {example_id}."
            )

        associated_gene_count = len(
            {
                gene
                for genes in association_index.values()
                for gene in genes
            }
        )
        disease_index = _load_hpo_disease_index(
            temporary_disease_annotations
        )
        if (
            len(disease_index)
            < MIN_HPO_DISEASE_ANNOTATION_TERMS
        ):
            raise HPODataError(
                "Downloaded HPO disease annotations contain too "
                "few terms."
            )
        unknown_disease_annotation_ids = (
            disease_index.keys() - ontology_index.keys()
        )
        if unknown_disease_annotation_ids:
            example_id = min(unknown_disease_annotation_ids)
            raise HPODataError(
                "HPO disease annotations do not match the ontology; "
                f"unknown term {example_id}."
            )
        associated_disease_count = len(
            {
                disease["id"]
                for diseases in disease_index.values()
                for disease in diseases
            }
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

        ontology_changed = not _same_file_content(
            ontology_path,
            temporary_ontology,
        )
        associations_changed = not _same_file_content(
            associations_path,
            temporary_associations,
        )
        disease_annotations_changed = not _same_file_content(
            disease_annotations_path,
            temporary_disease_annotations,
        )
        if (
            not ontology_changed
            and not associations_changed
            and not disease_annotations_changed
        ):
            return {
                "status": "unchanged",
                "previous_version": previous_version,
                "current_version": current_version,
                "active_term_count": active_term_count,
                "ontology_lookup_id_count": len(ontology_index),
                "association_term_count": len(association_index),
                "associated_gene_count": associated_gene_count,
                "disease_annotation_term_count": len(disease_index),
                "associated_disease_count": associated_disease_count,
                "ontology_backup_path": None,
                "associations_backup_path": None,
                "disease_annotations_backup_path": None,
            }

        ontology_backup = (
            _backup_hpo_file(
                ontology_path,
                ontology_path.with_suffix(".previous.obo"),
            )
            if ontology_changed
            else None
        )
        associations_backup = (
            _backup_hpo_file(
                associations_path,
                associations_path.with_suffix(".previous.txt"),
            )
            if associations_changed
            else None
        )
        disease_annotations_backup = (
            _backup_hpo_file(
                disease_annotations_path,
                disease_annotations_path.with_suffix(
                    ".previous.hpoa"
                ),
            )
            if disease_annotations_changed
            else None
        )

        ontology_installed = False
        associations_installed = False
        disease_annotations_installed = False
        try:
            if ontology_changed:
                os.replace(temporary_ontology, ontology_path)
                ontology_installed = True
            if associations_changed:
                os.replace(
                    temporary_associations,
                    associations_path,
                )
                associations_installed = True
            if disease_annotations_changed:
                os.replace(
                    temporary_disease_annotations,
                    disease_annotations_path,
                )
                disease_annotations_installed = True
        except OSError as exc:
            try:
                if disease_annotations_installed:
                    _restore_hpo_file(
                        disease_annotations_path,
                        disease_annotations_backup,
                    )
                if associations_installed:
                    _restore_hpo_file(
                        associations_path,
                        associations_backup,
                    )
                if ontology_installed:
                    _restore_hpo_file(
                        ontology_path,
                        ontology_backup,
                    )
            except HPODataError as rollback_exc:
                raise HPODataError(
                    "Coordinated HPO update failed and rollback "
                    "was incomplete."
                ) from rollback_exc
            raise HPODataError(
                "Coordinated HPO data installation failed; previous "
                "files were restored."
            ) from exc

        _load_hpo_index.cache_clear()
        _load_hpo_search_index.cache_clear()
        _load_hpo_gene_index.cache_clear()
        _load_hpo_disease_index.cache_clear()
        return {
            "status": "updated",
            "previous_version": previous_version,
            "current_version": current_version,
            "active_term_count": active_term_count,
            "ontology_lookup_id_count": len(ontology_index),
            "association_term_count": len(association_index),
            "associated_gene_count": associated_gene_count,
            "disease_annotation_term_count": len(disease_index),
            "associated_disease_count": associated_disease_count,
            "ontology_backup_path": (
                str(ontology_backup)
                if ontology_backup is not None
                else None
            ),
            "associations_backup_path": (
                str(associations_backup)
                if associations_backup is not None
                else None
            ),
            "disease_annotations_backup_path": (
                str(disease_annotations_backup)
                if disease_annotations_backup is not None
                else None
            ),
        }
    finally:
        _load_hpo_index.cache_clear()
        _load_hpo_search_index.cache_clear()
        _load_hpo_gene_index.cache_clear()
        _load_hpo_disease_index.cache_clear()
        for temporary_path in (
            temporary_ontology,
            temporary_associations,
            temporary_disease_annotations,
        ):
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def update_hpo_data(
    *,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
    disease_annotations_path: str | Path | None = None,
    ontology_source_url: str | None = None,
    association_url_template: str | None = None,
    disease_url_template: str | None = None,
    session: requests.Session | None = None,
) -> HPODataUpdateResult:
    """Refresh matching ontology, gene, and disease HPO data."""
    resolved_ontology_path = (
        Path(ontology_path)
        if ontology_path is not None
        else settings.HPO_DATA_DIR / HPO_ONTOLOGY_FILENAME
    ).resolve()
    resolved_associations_path = (
        Path(associations_path)
        if associations_path is not None
        else (
            settings.HPO_DATA_DIR
            / HPO_GENE_ASSOCIATIONS_FILENAME
        )
    ).resolve()
    resolved_disease_annotations_path = (
        Path(disease_annotations_path)
        if disease_annotations_path is not None
        else (
            settings.HPO_DATA_DIR
            / HPO_DISEASE_ANNOTATIONS_FILENAME
        )
    ).resolve()
    resolved_ontology_url = (
        ontology_source_url
        if ontology_source_url is not None
        else settings.HPO_ONTOLOGY_URL
    ).strip()
    resolved_association_template = (
        association_url_template
        if association_url_template is not None
        else settings.HPO_GENE_ASSOCIATIONS_URL_TEMPLATE
    ).strip()
    resolved_disease_template = (
        disease_url_template
        if disease_url_template is not None
        else settings.HPO_DISEASE_ANNOTATIONS_URL_TEMPLATE
    ).strip()
    if not resolved_ontology_url.lower().startswith("https://"):
        raise HPODataError(
            "HPO ontology updates require an HTTPS source URL."
        )
    if (
        not resolved_association_template.lower().startswith(
            "https://"
        )
        or "{release}" not in resolved_association_template
    ):
        raise HPODataError(
            "HPO gene association URL template must use HTTPS "
            "and contain {release}."
        )
    if (
        not resolved_disease_template.lower().startswith(
            "https://"
        )
        or "{release}" not in resolved_disease_template
    ):
        raise HPODataError(
            "HPO disease annotation URL template must use HTTPS "
            "and contain {release}."
        )

    active_session = session or requests.Session()
    owns_session = session is None
    try:
        with _HPO_UPDATE_LOCK:
            return _perform_hpo_data_update(
                active_session,
                resolved_ontology_url,
                resolved_association_template,
                resolved_disease_template,
                resolved_ontology_path,
                resolved_associations_path,
                resolved_disease_annotations_path,
            )
    finally:
        if owns_session:
            active_session.close()
