"""Bounded Persian clinical-text to HPO-candidate LLM contract."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import TypedDict

from backend.llm import (
    LLMClient,
    LLMJSONObject,
    LLMJSONSchema,
    LLMResponse,
    call_llm,
)
from backend.privacy import (
    PHENOTYPE_EXTRACTION_TASK,
    sanitize_phenotype_clinical_text,
    validate_llm_payload,
    validate_phenotype_extraction_payload,
)
from config import settings


PHENOTYPE_EXTRACTION_SCHEMA_VERSION = "2.0"
PHENOTYPE_EXTRACTION_PROMPT_VERSION = "phenotype-extraction-v2.0"
MAX_PHENOTYPE_CANDIDATES = 25
MAX_NON_HPO_MENTIONS = 25
MAX_TOTAL_EXTRACTED_MENTIONS = 50
MAX_HPO_LABEL_CHARACTERS = 200
MAX_SOURCE_PHRASE_CHARACTERS = 500
HPO_ID_PATTERN = re.compile(r"^HP:\d{7}$")
PHENOTYPE_EXTRACTION_OUTPUT_FIELDS = (
    "phenotype_candidates",
    "disease_mentions",
    "negated_phenotype_mentions",
    "uncertain_phenotype_mentions",
    "unmapped_clinical_phrases",
)

PHENOTYPE_EXTRACTION_SYSTEM_PROMPT = """You classify all independent clinical mentions in a short de-identified Persian clinical description and extract candidate Human Phenotype Ontology terms only for affirmed phenotypic abnormalities.

Hard constraints:
1. Examine every line, sentence, and clause. Return all independent clinical mentions, not only the first or most salient mention.
2. Put each explicit mention in exactly one output category.
3. phenotype_candidates contains only affirmed/current phenotypic abnormalities that are explicitly supported by the text.
4. A disease or syndrome name, including Waardenburg syndrome, belongs in disease_mentions and must not be converted into an HPO phenotype unless a separate phenotype is explicitly stated.
5. Put explicitly absent or denied findings in negated_phenotype_mentions. Put possible, suspected, or uncertain findings in uncertain_phenotype_mentions. Neither category may produce an HPO candidate.
6. Put relevant text that cannot safely be mapped or is not a phenotype/disease mention in unmapped_clinical_phrases.
7. Do not diagnose a disease or infer a disease name when it is not explicitly supported.
8. Do not interpret genetic variants, recommend treatment, or provide medical advice.
9. Do not invent HPO identifiers. If no affirmed phenotype is sufficiently supported, return an empty phenotype_candidates list.
10. Preserve a short verbatim Persian source phrase from the supplied text for every returned item.
11. Do not duplicate an HPO identifier or classify the same source phrase in multiple categories.
12. Return only the required structured response. Do not add prose or URLs.

The application will independently validate every HPO identifier against its local ontology and the user must review all suggestions before use."""

_SOURCE_MENTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_phrase_fa"],
    "properties": {
        "source_phrase_fa": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SOURCE_PHRASE_CHARACTERS,
        }
    },
}

PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA = LLMJSONSchema(
    name="phenotype_hpo_candidates",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": list(PHENOTYPE_EXTRACTION_OUTPUT_FIELDS),
        "properties": {
            "phenotype_candidates": {
                "type": "array",
                "maxItems": MAX_PHENOTYPE_CANDIDATES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "hpo_id",
                        "label",
                        "source_phrase_fa",
                    ],
                    "properties": {
                        "hpo_id": {
                            "type": "string",
                            "pattern": r"^HP:\d{7}$",
                        },
                        "label": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_HPO_LABEL_CHARACTERS,
                        },
                        "source_phrase_fa": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_SOURCE_PHRASE_CHARACTERS,
                        },
                    },
                },
            },
            "disease_mentions": {
                "type": "array",
                "maxItems": MAX_NON_HPO_MENTIONS,
                "items": _SOURCE_MENTION_SCHEMA,
            },
            "negated_phenotype_mentions": {
                "type": "array",
                "maxItems": MAX_NON_HPO_MENTIONS,
                "items": _SOURCE_MENTION_SCHEMA,
            },
            "uncertain_phenotype_mentions": {
                "type": "array",
                "maxItems": MAX_NON_HPO_MENTIONS,
                "items": _SOURCE_MENTION_SCHEMA,
            },
            "unmapped_clinical_phrases": {
                "type": "array",
                "maxItems": MAX_NON_HPO_MENTIONS,
                "items": _SOURCE_MENTION_SCHEMA,
            },
        },
    },
)


class PhenotypeExtractionError(RuntimeError):
    """Raised when phenotype extraction input or output is unsafe."""


class PhenotypeCandidate(TypedDict):
    """One format-valid, not-yet-ontology-validated HPO candidate."""

    hpo_id: str
    label: str
    source_phrase_fa: str


class ClinicalMention(TypedDict):
    """One grounded non-HPO clinical mention."""

    source_phrase_fa: str


class PhenotypeExtractionResult(TypedDict):
    """Validated output and bounded model provenance."""

    schema_version: str
    task: str
    prompt_version: str
    model: str
    phenotype_candidates: list[PhenotypeCandidate]
    disease_mentions: list[ClinicalMention]
    negated_phenotype_mentions: list[ClinicalMention]
    uncertain_phenotype_mentions: list[ClinicalMention]
    unmapped_clinical_phrases: list[ClinicalMention]


def _bounded_response_text(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str:
    """Return one safe non-empty structured-response string."""

    if not isinstance(value, str):
        raise PhenotypeExtractionError(f"{field} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise PhenotypeExtractionError(
            f"{field} is empty or exceeds its size limit."
        )
    if any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"}
        for character in normalized
    ):
        raise PhenotypeExtractionError(
            f"{field} contains invalid control characters."
        )
    return normalized


def _normalize_persian_for_grounding(value: str) -> str:
    """Normalize Persian orthographic variants for source-span comparison."""

    normalized = unicodedata.normalize("NFKC", value).translate(
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
    return " ".join(normalized.split())


def _grounded_source_phrase(
    value: object,
    *,
    field: str,
    clinical_text_fa: str,
) -> str:
    phrase = _bounded_response_text(
        value,
        field=field,
        maximum=MAX_SOURCE_PHRASE_CHARACTERS,
    )
    if _normalize_persian_for_grounding(phrase) not in (
        _normalize_persian_for_grounding(clinical_text_fa)
    ):
        raise PhenotypeExtractionError(
            f"{field} is not grounded in the supplied clinical text."
        )
    return phrase


def _parse_extraction(
    response: LLMResponse,
    *,
    clinical_text_fa: str,
) -> dict[str, object]:
    """Validate structured model output without trusting schema support."""

    if response.finish_reason not in {None, "stop"}:
        raise PhenotypeExtractionError(
            "The phenotype-extraction response did not finish safely."
        )

    def reject_nonstandard_constant(value: str) -> object:
        raise ValueError(f"Unsupported JSON constant: {value}")

    try:
        payload = json.loads(
            response.content,
            parse_constant=reject_nonstandard_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise PhenotypeExtractionError(
            "The phenotype-extraction response is not valid JSON."
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) != set(
        PHENOTYPE_EXTRACTION_OUTPUT_FIELDS
    ):
        raise PhenotypeExtractionError(
            "The phenotype-extraction response has invalid fields."
        )
    raw_candidates = payload["phenotype_candidates"]
    if not isinstance(raw_candidates, list):
        raise PhenotypeExtractionError(
            "The phenotype-extraction candidates must be a list."
        )
    if len(raw_candidates) > MAX_PHENOTYPE_CANDIDATES:
        raise PhenotypeExtractionError(
            "The phenotype-extraction response has too many candidates."
        )

    candidates: list[PhenotypeCandidate] = []
    seen_hpo_ids: set[str] = set()
    phrase_categories: dict[str, str] = {}
    expected_fields = {"hpo_id", "label", "source_phrase_fa"}
    for index, raw_candidate in enumerate(raw_candidates):
        if (
            not isinstance(raw_candidate, Mapping)
            or set(raw_candidate) != expected_fields
        ):
            raise PhenotypeExtractionError(
                f"Phenotype candidate {index + 1} has invalid fields."
            )
        hpo_id = raw_candidate["hpo_id"]
        if (
            not isinstance(hpo_id, str)
            or HPO_ID_PATTERN.fullmatch(hpo_id) is None
        ):
            raise PhenotypeExtractionError(
                f"Phenotype candidate {index + 1} has an invalid HPO ID."
            )
        if hpo_id in seen_hpo_ids:
            raise PhenotypeExtractionError(
                "The phenotype-extraction response contains duplicate HPO IDs."
            )
        seen_hpo_ids.add(hpo_id)
        candidates.append(
            {
                "hpo_id": hpo_id,
                "label": _bounded_response_text(
                    raw_candidate["label"],
                    field=f"candidates[{index}].label",
                    maximum=MAX_HPO_LABEL_CHARACTERS,
                ),
                "source_phrase_fa": _bounded_response_text(
                    raw_candidate["source_phrase_fa"],
                    field=f"candidates[{index}].source_phrase_fa",
                    maximum=MAX_SOURCE_PHRASE_CHARACTERS,
                ),
            }
        )
    for index, candidate in enumerate(candidates):
        phrase = _grounded_source_phrase(
            candidate["source_phrase_fa"],
            field=f"phenotype_candidates[{index}].source_phrase_fa",
            clinical_text_fa=clinical_text_fa,
        )
        normalized_phrase = _normalize_persian_for_grounding(phrase)
        if re.search(
            r"(?:^|\s)(?:سندر(?:و)?م|syndrome)(?:\s|$)",
            normalized_phrase,
            flags=re.IGNORECASE,
        ):
            raise PhenotypeExtractionError(
                "A disease or syndrome mention cannot be returned as an "
                "HPO phenotype candidate."
            )
        candidate["source_phrase_fa"] = phrase
        phrase_categories.setdefault(
            normalized_phrase,
            "phenotype_candidates",
        )

    parsed: dict[str, object] = {"phenotype_candidates": candidates}
    total_mentions = len(candidates)
    for field in PHENOTYPE_EXTRACTION_OUTPUT_FIELDS[1:]:
        raw_mentions = payload[field]
        if not isinstance(raw_mentions, list):
            raise PhenotypeExtractionError(f"{field} must be a list.")
        if len(raw_mentions) > MAX_NON_HPO_MENTIONS:
            raise PhenotypeExtractionError(f"{field} has too many items.")
        mentions: list[ClinicalMention] = []
        seen_in_field: set[str] = set()
        for index, raw_mention in enumerate(raw_mentions):
            if (
                not isinstance(raw_mention, Mapping)
                or set(raw_mention) != {"source_phrase_fa"}
            ):
                raise PhenotypeExtractionError(
                    f"{field}[{index}] has invalid fields."
                )
            phrase = _grounded_source_phrase(
                raw_mention["source_phrase_fa"],
                field=f"{field}[{index}].source_phrase_fa",
                clinical_text_fa=clinical_text_fa,
            )
            normalized_phrase = _normalize_persian_for_grounding(phrase)
            existing_category = phrase_categories.get(normalized_phrase)
            if existing_category is not None and existing_category != field:
                raise PhenotypeExtractionError(
                    "The same source phrase appears in multiple categories."
                )
            if normalized_phrase in seen_in_field:
                raise PhenotypeExtractionError(
                    f"{field} contains a duplicate source phrase."
                )
            seen_in_field.add(normalized_phrase)
            phrase_categories[normalized_phrase] = field
            mentions.append({"source_phrase_fa": phrase})
        parsed[field] = mentions
        total_mentions += len(mentions)
    if total_mentions > MAX_TOTAL_EXTRACTED_MENTIONS:
        raise PhenotypeExtractionError(
            "The phenotype-extraction response has too many total mentions."
        )
    validate_llm_payload(parsed)
    return parsed


def extract_hpo_candidates(
    clinical_text_fa: object,
    *,
    model: str | None = None,
    client: LLMClient | None = None,
    max_retries: int | None = None,
    response_format: LLMJSONSchema | LLMJSONObject | None = None,
) -> PhenotypeExtractionResult:
    """Extract format-valid HPO candidates from sanitized Persian text."""

    if client is not None and model is not None:
        raise PhenotypeExtractionError(
            "Choose either a custom LLM client or a model override."
        )
    sanitized_text = sanitize_phenotype_clinical_text(clinical_text_fa)
    request_payload = {
        "clinical_text_fa": sanitized_text,
        "task": PHENOTYPE_EXTRACTION_TASK,
    }
    request_payload = validate_phenotype_extraction_payload(
        request_payload
    )
    user_prompt = json.dumps(
        request_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    selected_model = (
        settings.PHENOTYPE_EXTRACTION_MODEL
        if model is None
        else model
    )
    if not isinstance(selected_model, str) or not selected_model.strip():
        raise PhenotypeExtractionError(
            "The Phenotype Extraction Model must be non-empty."
        )

    response = call_llm(
        PHENOTYPE_EXTRACTION_SYSTEM_PROMPT,
        user_prompt,
        temperature=0.0,
        max_tokens=settings.PHENOTYPE_EXTRACTION_MAX_TOKENS,
        client=client,
        model=(None if client is not None else selected_model.strip()),
        max_retries=max_retries,
        response_format=(
            PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA
            if response_format is None
            else response_format
        ),
    )
    parsed = _parse_extraction(response, clinical_text_fa=sanitized_text)
    return {
        "schema_version": PHENOTYPE_EXTRACTION_SCHEMA_VERSION,
        "task": PHENOTYPE_EXTRACTION_TASK,
        "prompt_version": PHENOTYPE_EXTRACTION_PROMPT_VERSION,
        "model": response.model,
        "phenotype_candidates": parsed["phenotype_candidates"],
        "disease_mentions": parsed["disease_mentions"],
        "negated_phenotype_mentions": parsed[
            "negated_phenotype_mentions"
        ],
        "uncertain_phenotype_mentions": parsed[
            "uncertain_phenotype_mentions"
        ],
        "unmapped_clinical_phrases": parsed[
            "unmapped_clinical_phrases"
        ],
    }


__all__ = [
    "HPO_ID_PATTERN",
    "LLMJSONObject",
    "MAX_PHENOTYPE_CANDIDATES",
    "MAX_NON_HPO_MENTIONS",
    "MAX_TOTAL_EXTRACTED_MENTIONS",
    "PHENOTYPE_EXTRACTION_OUTPUT_FIELDS",
    "PHENOTYPE_EXTRACTION_PROMPT_VERSION",
    "PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA",
    "PHENOTYPE_EXTRACTION_SCHEMA_VERSION",
    "PHENOTYPE_EXTRACTION_SYSTEM_PROMPT",
    "PHENOTYPE_EXTRACTION_TASK",
    "PhenotypeCandidate",
    "ClinicalMention",
    "PhenotypeExtractionError",
    "PhenotypeExtractionResult",
    "extract_hpo_candidates",
]
