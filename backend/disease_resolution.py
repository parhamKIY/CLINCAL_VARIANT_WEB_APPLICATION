"""Deterministic resolution and routing for explicit disease entities."""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

from backend.clinical_entities import (
    ClinicalAssertion,
    ClinicalEntity,
    ClinicalEntityError,
    normalize_clinical_entity_text,
    validate_clinical_entities,
    validate_clinical_entity,
)
from backend.privacy import validate_llm_payload
from config import settings


HPO_DISEASE_ANNOTATIONS_FILENAME = "phenotype.hpoa"
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
HPO_ID_PATTERN = re.compile(r"HP:[0-9]{7}")
DISEASE_IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
)
MAX_DISEASE_ANNOTATION_BYTES = 75 * 1024 * 1024
MAX_DISEASE_LIMITATIONS = 10
MAX_DISEASE_RESOLUTION_RESULTS = 50
DISEASE_RESOLUTION_FIELDS = frozenset(
    {
        "original_text",
        "normalized_name",
        "identifier",
        "preferred_label",
        "resolution_status",
        "resolver_source",
        "resolver_version",
        "match_type",
        "assertion",
        "limitations",
    }
)
DISEASE_RESOLVER_SOURCE = "HPO disease annotations"
DISEASE_RESOLUTION_STATUSES = frozenset(
    {"RESOLVED", "UNRESOLVED", "AMBIGUOUS", "UNAVAILABLE"}
)
DISEASE_RESOLUTION_LIMITATIONS = frozenset(
    {
        "no_exact_disease_match",
        "multiple_exact_disease_matches",
        "resolver_data_unavailable",
    }
)

DiseaseResolutionStatus = Literal[
    "RESOLVED",
    "UNRESOLVED",
    "AMBIGUOUS",
    "UNAVAILABLE",
]
DiseaseMatchType = Literal["EXACT"]


class DiseaseResolutionError(ValueError):
    """Raised when disease-resolution input or output is unsafe."""


class DiseaseResolverDataError(RuntimeError):
    """Raised when local disease-resolution data cannot be trusted."""


class DiseaseResolutionResult(TypedDict):
    """Bounded case-context result for one explicit disease mention."""

    original_text: str
    normalized_name: str
    identifier: str | None
    preferred_label: str | None
    resolution_status: DiseaseResolutionStatus
    resolver_source: str
    resolver_version: str | None
    match_type: DiseaseMatchType | None
    assertion: ClinicalAssertion
    limitations: list[str]


class ClinicalEntityRoutingResult(TypedDict):
    """Separate phenotype entities from resolved disease case context."""

    phenotype_entities: list[ClinicalEntity]
    disease_resolutions: list[DiseaseResolutionResult]


class DiseaseResolver(Protocol):
    """Minimal disease resolver boundary used by clinical-entity routing."""

    def resolve(self, entity: ClinicalEntity) -> DiseaseResolutionResult:
        """Resolve one explicit disease entity without clinical inference."""


@dataclass(frozen=True)
class _DiseaseRecord:
    identifier: str
    preferred_label: str


@dataclass(frozen=True)
class _DiseaseIndex:
    version: str | None
    records_by_name: Mapping[str, tuple[_DiseaseRecord, ...]]


def normalize_disease_name(value: object) -> str:
    """Normalize formatting only; never translate or fuzzily alter a term."""

    normalized = normalize_clinical_entity_text(value)
    characters = []
    for character in normalized:
        category = unicodedata.category(character)
        characters.append(" " if category[0] in {"P", "Z"} else character)
    result = " ".join("".join(characters).split())
    if not result:
        raise DiseaseResolutionError(
            "Disease name is empty after deterministic normalization."
        )
    return result


def _disease_key(value: object) -> str:
    return normalize_disease_name(value).casefold()


def _bounded_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise DiseaseResolutionError(f"Disease resolution {field} is invalid.")
    stripped = value.strip()
    if not stripped or len(stripped) > 500:
        raise DiseaseResolutionError(f"Disease resolution {field} is invalid.")
    return stripped


def _parse_version(line: str) -> str | None:
    prefix = "#version:"
    if not line.casefold().startswith(prefix):
        return None
    value = line[len(prefix):].strip().strip('"')
    return value if value and len(value) <= 100 else None


@lru_cache(maxsize=4)
def _load_disease_index(annotations_path: Path) -> _DiseaseIndex:
    try:
        if annotations_path.stat().st_size > MAX_DISEASE_ANNOTATION_BYTES:
            raise DiseaseResolverDataError(
                "Disease annotations exceed the bounded file size."
            )
        annotation_file = annotations_path.open(
            "r",
            encoding="utf-8",
            newline="",
        )
    except OSError as exc:
        raise DiseaseResolverDataError(
            "Disease annotations are unavailable."
        ) from exc

    version: str | None = None
    records: dict[str, dict[str, Counter[str]]] = {}
    try:
        with annotation_file:
            data_lines: list[str] = []
            for line in annotation_file:
                if line.startswith("#"):
                    version = _parse_version(line) or version
                else:
                    data_lines.append(line)
            rows = csv.reader(data_lines, delimiter="\t")
            header = next(rows, None)
            if tuple(header or ()) != HPO_DISEASE_ANNOTATION_HEADER:
                raise DiseaseResolverDataError(
                    "Disease annotations have an unexpected header."
                )
            for line_number, row in enumerate(rows, start=2):
                if len(row) != len(HPO_DISEASE_ANNOTATION_HEADER):
                    raise DiseaseResolverDataError(
                        "Disease annotation row has an unexpected structure."
                    )
                (
                    identifier,
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
                ) = (item.strip() for item in row)
                if (
                    DISEASE_IDENTIFIER_PATTERN.fullmatch(identifier) is None
                    or not disease_name
                    or HPO_ID_PATTERN.fullmatch(hpo_id) is None
                    or qualifier not in {"", "NOT"}
                    or not reference
                    or not evidence
                    or not biocuration
                ):
                    raise DiseaseResolverDataError(
                        f"Disease annotation row {line_number} is invalid."
                    )
                if qualifier == "NOT" or aspect != "P":
                    continue
                key = _disease_key(disease_name)
                records.setdefault(key, {}).setdefault(
                    identifier,
                    Counter(),
                )[disease_name] += 1
    except (OSError, UnicodeError, csv.Error, ClinicalEntityError) as exc:
        raise DiseaseResolverDataError(
            "Disease annotations could not be read safely."
        ) from exc

    if not records:
        raise DiseaseResolverDataError(
            "Disease annotations contain no usable records."
        )
    return _DiseaseIndex(
        version=version,
        records_by_name={
            key: tuple(
                _DiseaseRecord(
                    identifier=identifier,
                    preferred_label=min(
                        labels,
                        key=lambda label: (-labels[label], label),
                    ),
                )
                for identifier, labels in sorted(by_identifier.items())
            )
            for key, by_identifier in records.items()
        },
    )


def _result(
    entity: ClinicalEntity,
    *,
    normalized_name: str,
    status: DiseaseResolutionStatus,
    version: str | None,
    identifier: str | None = None,
    preferred_label: str | None = None,
    match_type: DiseaseMatchType | None = None,
    limitations: Sequence[str] = (),
) -> DiseaseResolutionResult:
    return validate_disease_resolution_result(
        {
            "original_text": entity["original_text"],
            "normalized_name": normalized_name,
            "identifier": identifier,
            "preferred_label": preferred_label,
            "resolution_status": status,
            "resolver_source": DISEASE_RESOLVER_SOURCE,
            "resolver_version": version,
            "match_type": match_type,
            "assertion": entity["assertion"],
            "limitations": list(limitations),
        }
    )


class HpoDiseaseAnnotationResolver:
    """Exact-only resolver backed by the approved local HPOA release."""

    def __init__(self, annotations_path: str | Path | None = None) -> None:
        self.annotations_path = (
            Path(annotations_path)
            if annotations_path is not None
            else settings.HPO_DATA_DIR / HPO_DISEASE_ANNOTATIONS_FILENAME
        ).resolve()

    def resolve(self, entity: ClinicalEntity) -> DiseaseResolutionResult:
        normalized_entity = validate_clinical_entity(entity)
        if normalized_entity["entity_type"] != "DISEASE":
            raise DiseaseResolutionError(
                "The disease resolver accepts only DISEASE entities."
            )
        normalized_name = normalize_disease_name(
            normalized_entity["normalized_text"]
        )
        try:
            index = _load_disease_index(self.annotations_path)
        except DiseaseResolverDataError:
            return _result(
                normalized_entity,
                normalized_name=normalized_name,
                status="UNAVAILABLE",
                version=None,
                limitations=["resolver_data_unavailable"],
            )
        matches = index.records_by_name.get(normalized_name.casefold(), ())
        if not matches:
            return _result(
                normalized_entity,
                normalized_name=normalized_name,
                status="UNRESOLVED",
                version=index.version,
                limitations=["no_exact_disease_match"],
            )
        if len(matches) != 1:
            return _result(
                normalized_entity,
                normalized_name=normalized_name,
                status="AMBIGUOUS",
                version=index.version,
                limitations=["multiple_exact_disease_matches"],
            )
        match = matches[0]
        return _result(
            normalized_entity,
            normalized_name=normalized_name,
            status="RESOLVED",
            version=index.version,
            identifier=match.identifier,
            preferred_label=match.preferred_label,
            match_type="EXACT",
        )


def validate_disease_resolution_result(
    value: object,
) -> DiseaseResolutionResult:
    """Validate one case-context result without converting it to evidence."""

    if not isinstance(value, Mapping) or set(value) != DISEASE_RESOLUTION_FIELDS:
        raise DiseaseResolutionError("Disease resolution fields are invalid.")
    original_text = _bounded_text(value["original_text"], field="original_text")
    normalized_name = _bounded_text(
        value["normalized_name"],
        field="normalized_name",
    )
    if normalize_disease_name(normalized_name) != normalized_name:
        raise DiseaseResolutionError(
            "Disease resolution normalized_name is invalid."
        )
    status = value["resolution_status"]
    if status not in DISEASE_RESOLUTION_STATUSES:
        raise DiseaseResolutionError("Disease resolution status is invalid.")
    identifier = value["identifier"]
    preferred_label = value["preferred_label"]
    match_type = value["match_type"]
    if status == "RESOLVED":
        if (
            not isinstance(identifier, str)
            or DISEASE_IDENTIFIER_PATTERN.fullmatch(identifier) is None
            or not isinstance(preferred_label, str)
            or not preferred_label.strip()
            or match_type != "EXACT"
        ):
            raise DiseaseResolutionError(
                "Resolved disease identity is incomplete."
            )
    elif (
        identifier is not None
        or preferred_label is not None
        or match_type is not None
    ):
        raise DiseaseResolutionError(
            "Unresolved disease context must not carry an identity."
        )
    resolver_source = _bounded_text(
        value["resolver_source"],
        field="resolver_source",
    )
    if resolver_source != DISEASE_RESOLVER_SOURCE:
        raise DiseaseResolutionError("Disease resolver source is unsupported.")
    resolver_version = value["resolver_version"]
    if resolver_version is not None:
        resolver_version = _bounded_text(
            resolver_version,
            field="resolver_version",
        )
    assertion = value["assertion"]
    if assertion not in {"PRESENT", "SUSPECTED", "NEGATED", "HISTORICAL"}:
        raise DiseaseResolutionError("Disease assertion is invalid.")
    limitations = value["limitations"]
    if (
        not isinstance(limitations, list)
        or len(limitations) > MAX_DISEASE_LIMITATIONS
        or len(limitations) != len(set(limitations))
        or any(item not in DISEASE_RESOLUTION_LIMITATIONS for item in limitations)
    ):
        raise DiseaseResolutionError("Disease limitations are invalid.")
    expected_limitations = {
        "RESOLVED": [],
        "UNRESOLVED": ["no_exact_disease_match"],
        "AMBIGUOUS": ["multiple_exact_disease_matches"],
        "UNAVAILABLE": ["resolver_data_unavailable"],
    }
    if limitations != expected_limitations[cast(DiseaseResolutionStatus, status)]:
        raise DiseaseResolutionError(
            "Disease limitations do not match the resolution state."
        )
    result: DiseaseResolutionResult = {
        "original_text": original_text,
        "normalized_name": normalized_name,
        "identifier": cast(str | None, identifier),
        "preferred_label": cast(str | None, preferred_label),
        "resolution_status": cast(DiseaseResolutionStatus, status),
        "resolver_source": resolver_source,
        "resolver_version": cast(str | None, resolver_version),
        "match_type": cast(DiseaseMatchType | None, match_type),
        "assertion": cast(ClinicalAssertion, assertion),
        "limitations": list(limitations),
    }
    validate_llm_payload(result)
    return result


def validate_disease_resolution_results(
    value: object,
) -> list[DiseaseResolutionResult]:
    """Validate bounded disease resolutions in stable source order."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > MAX_DISEASE_RESOLUTION_RESULTS
    ):
        raise DiseaseResolutionError("Disease resolutions are invalid.")
    results = [validate_disease_resolution_result(item) for item in value]
    names = [item["normalized_name"].casefold() for item in results]
    if len(names) != len(set(names)):
        raise DiseaseResolutionError("Disease resolutions contain a duplicate.")
    return results


def route_clinical_entities(
    entities: object,
    *,
    disease_resolver: DiseaseResolver | None = None,
) -> ClinicalEntityRoutingResult:
    """Keep phenotype and disease routes separate in case-context metadata."""

    normalized_entities = validate_clinical_entities(entities)
    phenotype_entities = [
        entity for entity in normalized_entities
        if entity["entity_type"] == "PHENOTYPE"
    ]
    disease_entities = [
        entity for entity in normalized_entities
        if entity["entity_type"] == "DISEASE"
    ]
    resolver = disease_resolver or HpoDiseaseAnnotationResolver()
    disease_resolutions = validate_disease_resolution_results(
        [resolver.resolve(entity) for entity in disease_entities]
    )
    return {
        "phenotype_entities": phenotype_entities,
        "disease_resolutions": disease_resolutions,
    }


__all__ = [
    "ClinicalEntityRoutingResult",
    "DISEASE_RESOLUTION_STATUSES",
    "DiseaseMatchType",
    "DiseaseResolutionError",
    "DiseaseResolutionResult",
    "DiseaseResolutionStatus",
    "DiseaseResolver",
    "HpoDiseaseAnnotationResolver",
    "normalize_disease_name",
    "route_clinical_entities",
    "validate_disease_resolution_result",
    "validate_disease_resolution_results",
]
