"""Stage 9.2.5 AI draft-classification policy contract."""

from __future__ import annotations

import json

import pytest

from backend.llm import LLMClient, LLMResponse
from backend.variant_interpretation import (
    VARIANT_INTERPRETATION_RESPONSE_SCHEMA,
    interpret_variant,
)
from test_pipeline import (
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
)


ALLOWED_AI_CLASSIFICATIONS = {
    "Pathogenic",
    "Likely pathogenic",
    "Uncertain significance",
    "Likely benign",
    "Benign",
}


def _response(
    classification: object,
    *,
    include_classification: bool = True,
) -> LLMResponse:
    payload: dict[str, object] = {
        "interpretation": "A narrative synthesis remains available for review.",
        "conflict_assessment": "Uncertainty remains explicit.",
        "phenotype_conclusion": "partially supported",
        "warnings": [],
    }
    if include_classification:
        payload["ai_classification"] = classification
    return LLMResponse(
        content=json.dumps(payload),
        model="stage9-2-5-policy-model",
        finish_reason="stop",
    )


def test_success_contract_requires_exact_five_value_classification() -> None:
    schema = VARIANT_INTERPRETATION_RESPONSE_SCHEMA.schema

    assert "ai_classification" in schema["required"]
    assert set(schema["properties"]["ai_classification"]["enum"]) == (
        ALLOWED_AI_CLASSIFICATIONS
    )


def test_prompt_requires_draft_and_calibrates_uncertainty() -> None:
    adapter = SequenceLLMAdapter([_response("Uncertain significance")])

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T12:00:00Z",
    )

    system_prompt = adapter.requests[0].messages[0].content
    assert "must return exactly one" in system_prompt.casefold()
    assert "cannot independently classify" in system_prompt
    assert "must not" in system_prompt[
        system_prompt.index("cannot independently classify") - 20 :
        system_prompt.index("cannot independently classify")
    ].casefold()
    assert "conflicting or insufficient" in system_prompt.casefold()
    assert "Uncertain significance" in system_prompt
    assert "source assertions" in system_prompt
    assert result["ai_classification"] == "Uncertain significance"
    assert result["interpretation"] is not None


@pytest.mark.parametrize(
    "invalid_classification",
    ["cannot independently classify", "Insufficient evidence"],
)
def test_refusal_style_classification_requires_structured_repair(
    invalid_classification: str,
) -> None:
    adapter = SequenceLLMAdapter(
        [
            _response(invalid_classification),
            _response("Uncertain significance"),
        ]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T12:00:00Z",
    )

    assert len(adapter.requests) == 2
    assert "RECOVERY INSTRUCTION" in adapter.requests[1].messages[1].content
    assert result["status"] == "success"
    assert result["ai_classification"] == "Uncertain significance"
    assert result["interpretation"] is not None


def test_missing_classification_requires_structured_repair() -> None:
    adapter = SequenceLLMAdapter(
        [
            _response(None, include_classification=False),
            _response("Uncertain significance"),
        ]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T12:00:00Z",
    )

    assert len(adapter.requests) == 2
    assert result["status"] == "success"
    assert result["ai_classification"] == "Uncertain significance"
    assert result["interpretation"] is not None


def test_semantically_exact_classification_casing_is_canonicalized_locally() -> None:
    adapter = SequenceLLMAdapter([_response("Likely Pathogenic")])

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T12:00:00Z",
    )

    assert result["status"] == "success"
    assert result["ai_classification"] == "Likely pathogenic"
    assert result["interpretation"] is not None
    assert len(adapter.requests) == 1


def test_classification_repair_is_targeted_to_the_failed_core_field() -> None:
    adapter = SequenceLLMAdapter(
        [
            _response("cannot independently classify"),
            _response("Uncertain significance"),
        ]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T12:00:00Z",
    )

    repaired_prompt = adapter.requests[1].messages[1].content
    assert result["status"] == "success"
    assert "ai_classification" in repaired_prompt
    assert "Uncertain significance" in repaired_prompt
    assert "classification outside the five allowed values" in repaired_prompt
