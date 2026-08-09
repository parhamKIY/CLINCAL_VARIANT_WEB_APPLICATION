"""Bounded Persian clinical-text to HPO-candidate LLM contract."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TypedDict

from backend.llm import (
    LLMClient,
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


PHENOTYPE_EXTRACTION_SCHEMA_VERSION = "1.0"
PHENOTYPE_EXTRACTION_PROMPT_VERSION = "phenotype-extraction-v1.0"
MAX_PHENOTYPE_CANDIDATES = 25
MAX_HPO_LABEL_CHARACTERS = 200
MAX_SOURCE_PHRASE_CHARACTERS = 500
HPO_ID_PATTERN = re.compile(r"^HP:\d{7}$")

PHENOTYPE_EXTRACTION_SYSTEM_PROMPT = """You extract candidate Human Phenotype Ontology terms from a short de-identified Persian clinical description.

Hard constraints:
1. Identify phenotypic abnormalities explicitly supported by the supplied text only.
2. Do not diagnose a disease or infer a disease name when it is not explicitly supported.
3. Do not interpret genetic variants, recommend treatment, or provide medical advice.
4. Do not invent HPO identifiers. If no sufficiently supported candidate exists, return an empty candidates list.
5. Preserve a short Persian source phrase for each candidate.
6. Return only the required structured response. Do not add prose or URLs.

The application will independently validate every HPO identifier against its local ontology and the user must review all suggestions before use."""

PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA = LLMJSONSchema(
    name="phenotype_hpo_candidates",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
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
            }
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


class PhenotypeExtractionResult(TypedDict):
    """Validated output and bounded model provenance."""

    schema_version: str
    task: str
    prompt_version: str
    model: str
    candidates: list[PhenotypeCandidate]


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


def _parse_candidates(response: LLMResponse) -> list[PhenotypeCandidate]:
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
    if not isinstance(payload, Mapping) or set(payload) != {"candidates"}:
        raise PhenotypeExtractionError(
            "The phenotype-extraction response has invalid fields."
        )
    raw_candidates = payload["candidates"]
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
    validate_llm_payload({"candidates": candidates})
    return candidates


def extract_hpo_candidates(
    clinical_text_fa: object,
    *,
    model: str | None = None,
    client: LLMClient | None = None,
    max_retries: int | None = None,
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
        response_format=PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA,
    )
    return {
        "schema_version": PHENOTYPE_EXTRACTION_SCHEMA_VERSION,
        "task": PHENOTYPE_EXTRACTION_TASK,
        "prompt_version": PHENOTYPE_EXTRACTION_PROMPT_VERSION,
        "model": response.model,
        "candidates": _parse_candidates(response),
    }


__all__ = [
    "HPO_ID_PATTERN",
    "MAX_PHENOTYPE_CANDIDATES",
    "PHENOTYPE_EXTRACTION_PROMPT_VERSION",
    "PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA",
    "PHENOTYPE_EXTRACTION_SCHEMA_VERSION",
    "PHENOTYPE_EXTRACTION_SYSTEM_PROMPT",
    "PHENOTYPE_EXTRACTION_TASK",
    "PhenotypeCandidate",
    "PhenotypeExtractionError",
    "PhenotypeExtractionResult",
    "extract_hpo_candidates",
]
