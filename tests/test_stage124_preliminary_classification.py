"""Stage 124 preliminary-classification contract acceptance checks."""

from __future__ import annotations

import json

from backend.llm import LLMClient, LLMResponse
from backend.variant_interpretation import (
    interpret_variant,
    interpret_variants,
    validate_variant_interpretation_result,
)
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestStage50SingleModelInterpretation as InterpretationFactory,
    TestEvidenceObject as EvidenceFactory,
)


def _response(
    *,
    classification_status: str = "classified",
    classification: str | None = "Pathogenic",
) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "preliminary_classification_status": classification_status,
                "preliminary_classification": classification,
                "classification_rationale": (
                    "The validated source evidence supports this preliminary "
                    "classification."
                ),
                "interpretation": (
                    "The supplied source evidence supports a concise, "
                    "review-required interpretation."
                ),
                "conflict_assessment": "No meaningful conflict is present.",
                "phenotype_conclusion": "partially supported",
                "limitations": ["Human review remains required."],
                "warnings": [],
            }
        ),
        model="stage124-test-model",
        finish_reason="stop",
    )


def test_clear_evidence_returns_a_preliminary_classification() -> None:
    adapter = FakeLLMAdapter(_response())

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-14T10:00:00Z",
    )

    assert result["status"] == "success"
    assert result["preliminary_classification_status"] == "classified"
    assert result["preliminary_classification"] == "Pathogenic"
    assert result["classification_rationale"] is not None
    assert result["limitations"] == ["Human review remains required."]
    assert "preliminary_classification_status" in adapter.requests[0].messages[1].content


def test_material_conflict_returns_ambiguous_without_a_forced_label() -> None:
    result = interpret_variant(
        InterpretationFactory._conflicting_evidence(),
        client=LLMClient(
            FakeLLMAdapter(
                _response(
                    classification_status="ambiguous",
                    classification=None,
                )
            )
        ),
        timestamp="2026-08-14T10:00:00Z",
    )

    assert result["status"] == "success"
    assert result["preliminary_classification_status"] == "ambiguous"
    assert result["preliminary_classification"] is None


def test_material_conflict_rejects_a_forced_preliminary_label() -> None:
    result = interpret_variants(
        [InterpretationFactory._conflicting_evidence()],
        client=LLMClient(SequenceLLMAdapter([_response(), _response()])),
        timestamp="2026-08-14T10:00:00Z",
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "output_schema_failure"


def test_missing_classification_fields_are_repaired_once() -> None:
    invalid = LLMResponse(
        content=json.dumps({"interpretation": "incomplete"}),
        model="stage124-invalid-model",
        finish_reason="stop",
    )
    adapter = SequenceLLMAdapter([invalid, _response()])

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp="2026-08-14T10:00:00Z",
    )

    assert result["status"] == "success"
    assert len(adapter.requests) == 2
    assert "RECOVERY INSTRUCTION" in adapter.requests[1].messages[1].content


def test_legacy_result_remains_reviewable_without_an_invented_label() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    current = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(_response())),
        timestamp="2026-08-14T10:00:00Z",
    )
    legacy = dict(current)
    legacy["schema_version"] = "1.1"
    for field in (
        "preliminary_classification_status",
        "preliminary_classification",
        "classification_rationale",
        "limitations",
    ):
        legacy.pop(field)

    validated = validate_variant_interpretation_result(legacy, evidence=evidence)

    assert validated["schema_version"] == "1.1"
    assert validated["preliminary_classification_status"] == "ambiguous"
    assert validated["preliminary_classification"] is None
