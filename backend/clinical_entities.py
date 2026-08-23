"""Bounded case-specific clinical entity contract."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict, cast

from backend.privacy import validate_llm_payload


MAX_CLINICAL_ENTITIES = 50
MAX_CLINICAL_ENTITY_TEXT_CHARACTERS = 500
ClinicalEntityType = Literal["PHENOTYPE", "DISEASE"]
ClinicalAssertion = Literal[
    "PRESENT",
    "SUSPECTED",
    "NEGATED",
    "HISTORICAL",
]
CLINICAL_ENTITY_TYPES = frozenset({"PHENOTYPE", "DISEASE"})
CLINICAL_ASSERTIONS = frozenset(
    {"PRESENT", "SUSPECTED", "NEGATED", "HISTORICAL"}
)
CLINICAL_ENTITY_FIELDS = frozenset(
    {"original_text", "normalized_text", "entity_type", "assertion"}
)


class ClinicalEntityError(ValueError):
    """Raised when case-specific entity context violates its contract."""


class ClinicalEntity(TypedDict):
    """One explicit, unresolved clinical mention from case narrative."""

    original_text: str
    normalized_text: str
    entity_type: ClinicalEntityType
    assertion: ClinicalAssertion


def _bounded_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ClinicalEntityError(f"Clinical entity {field} must be text.")
    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > MAX_CLINICAL_ENTITY_TEXT_CHARACTERS
        or any(
            ord(character) < 32
            and character not in {"\n", "\r", "\t"}
            for character in stripped
        )
    ):
        raise ClinicalEntityError(
            f"Clinical entity {field} is empty or invalid."
        )
    return stripped


def normalize_clinical_entity_text(value: object) -> str:
    """Return deterministic Persian surface normalization for one mention."""

    text = _bounded_text(value, field="text")
    normalized = unicodedata.normalize("NFKC", text).translate(
        str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ـ": ""})
    )
    normalized = "".join(
        character
        for character in normalized
        if not (
            "\u064b" <= character <= "\u065f"
            or character == "\u0670"
        )
    )
    normalized = normalized.replace("\u200c", " ").replace("\u200d", " ")
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ClinicalEntityError("Clinical entity text is empty after normalization.")
    return normalized


def validate_clinical_entity(value: object) -> ClinicalEntity:
    """Validate one unresolved entity without performing ontology lookup."""

    if not isinstance(value, Mapping) or set(value) != CLINICAL_ENTITY_FIELDS:
        raise ClinicalEntityError("Clinical entity fields are invalid.")
    original_text = _bounded_text(value["original_text"], field="original_text")
    normalized_text = _bounded_text(
        value["normalized_text"],
        field="normalized_text",
    )
    canonical_original = normalize_clinical_entity_text(original_text)
    canonical_normalized = normalize_clinical_entity_text(normalized_text)
    if (
        normalized_text != canonical_normalized
        or canonical_normalized not in canonical_original
    ):
        raise ClinicalEntityError("Clinical entity normalized_text is invalid.")
    entity_type = value["entity_type"]
    if entity_type not in CLINICAL_ENTITY_TYPES:
        raise ClinicalEntityError("Clinical entity type is unsupported.")
    assertion = value["assertion"]
    if assertion not in CLINICAL_ASSERTIONS:
        raise ClinicalEntityError("Clinical entity assertion is unsupported.")
    entity: ClinicalEntity = {
        "original_text": original_text,
        "normalized_text": normalized_text,
        "entity_type": cast(ClinicalEntityType, entity_type),
        "assertion": cast(ClinicalAssertion, assertion),
    }
    validate_llm_payload(entity)
    return entity


def validate_clinical_entities(value: object) -> list[ClinicalEntity]:
    """Validate one bounded entity collection in stable source order."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise ClinicalEntityError("Clinical entities must be a sequence.")
    if len(value) > MAX_CLINICAL_ENTITIES:
        raise ClinicalEntityError("Clinical entity count exceeds the limit.")
    entities = [validate_clinical_entity(item) for item in value]
    identities = [entity["normalized_text"] for entity in entities]
    if len(identities) != len(set(identities)):
        raise ClinicalEntityError("Clinical entities contain a duplicate.")
    return entities


__all__ = [
    "CLINICAL_ASSERTIONS",
    "CLINICAL_ENTITY_TYPES",
    "ClinicalAssertion",
    "ClinicalEntity",
    "ClinicalEntityError",
    "ClinicalEntityType",
    "MAX_CLINICAL_ENTITIES",
    "MAX_CLINICAL_ENTITY_TEXT_CHARACTERS",
    "normalize_clinical_entity_text",
    "validate_clinical_entities",
    "validate_clinical_entity",
]
