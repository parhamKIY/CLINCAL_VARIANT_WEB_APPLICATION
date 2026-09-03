"""Persistent, bounded AI-draft regeneration contract."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.llm import LLMClient, LLMResponse
from backend.variant_interpretation import (
    VariantInterpretationError,
    interpret_variant,
    retain_prior_interpretation_generation,
    validate_variant_interpretation_result,
)
from test_pipeline import FakeLLMAdapter, TestEvidenceObject as EvidenceFactory


def _generate(
    *,
    classification: str,
    narrative: str,
    timestamp: str,
) -> tuple[dict[str, object], dict[str, object]]:
    evidence = EvidenceFactory._complete_evidence_object()
    response = LLMResponse(
        content=json.dumps(
            {
                "ai_classification": classification,
                "interpretation": narrative,
                "conflict_assessment": "No material source conflict was identified.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="regeneration-contract-model",
        finish_reason="stop",
    )
    result = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(response)),
        timestamp=timestamp,
    )
    return evidence, result


def test_initial_generation_has_empty_persistent_history() -> None:
    _, result = _generate(
        classification="Uncertain significance",
        narrative="Initial evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )

    assert result["schema_version"] == "1.4"
    assert result["generation_history"] == []


def test_prior_successful_draft_is_retained_with_integrity_protection() -> None:
    evidence, prior = _generate(
        classification="Uncertain significance",
        narrative="Initial evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )
    _, replacement = _generate(
        classification="Likely benign",
        narrative="Regenerated evidence-based AI draft.",
        timestamp="2026-09-03T08:05:00Z",
    )

    result = retain_prior_interpretation_generation(
        prior,
        replacement,
        evidence=evidence,
    )

    assert result["ai_classification"] == "Likely benign"
    assert result["interpretation"] == "Regenerated evidence-based AI draft."
    assert len(result["generation_history"]) == 1
    record = result["generation_history"][0]
    assert record["reason"] == "user_requested_regeneration"
    assert record["prior_result"]["ai_classification"] == (
        "Uncertain significance"
    )
    assert record["prior_result"]["interpretation"] == (
        "Initial evidence-based AI draft."
    )

    corrupted = deepcopy(result)
    corrupted["generation_history"][0]["prior_result"]["interpretation"] = (
        "Tampered draft."
    )
    with pytest.raises(
        VariantInterpretationError,
        match="integrity check failed",
    ):
        validate_variant_interpretation_result(corrupted, evidence=evidence)


def test_historical_schema_1_3_loads_without_generation_history() -> None:
    evidence, current = _generate(
        classification="Uncertain significance",
        narrative="Historical evidence-based AI draft.",
        timestamp="2026-09-03T08:00:00Z",
    )
    historical = dict(current)
    historical["schema_version"] = "1.3"
    historical.pop("generation_history")

    normalized = validate_variant_interpretation_result(
        historical,
        evidence=evidence,
    )

    assert normalized["status"] == "success"
    assert normalized["interpretation"] == "Historical evidence-based AI draft."
    assert normalized["generation_history"] == []
