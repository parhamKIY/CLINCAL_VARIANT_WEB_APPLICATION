"""Stage 9.2.6 canonical citation-group normalization."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.llm import LLMClient, LLMResponse
from backend.variant_interpretation import (
    VariantInterpretationError,
    interpret_variant,
)
from test_pipeline import FakeLLMAdapter, TestEvidenceObject as EvidenceFactory


def _evidence_with_three_references() -> dict[str, object]:
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    evidence["pathogenicity"]["cspec_context"] = [
        {
            "specification_id": "GN001",
            "title": "First test specification",
            "specification_url": (
                "https://cspec.genome.network/"
                "SequenceVariantInterpretation/id/GN001"
            ),
            "concept_doi": "10.1000/stage9.2.6-one",
        },
        {
            "specification_id": "GN002",
            "title": "Second test specification",
            "specification_url": (
                "https://cspec.genome.network/"
                "SequenceVariantInterpretation/id/GN002"
            ),
            "concept_doi": "10.1000/stage9.2.6-two",
        },
    ]
    return evidence


def _response(interpretation: str) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Uncertain significance",
                "interpretation": interpretation,
                "conflict_assessment": "Uncertainty remains explicit.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="stage9-2-6-citation-model",
        finish_reason="stop",
    )


def test_existing_single_token_citations_remain_unchanged() -> None:
    adapter = FakeLLMAdapter(_response("Supported by [R1][R2]."))

    result = interpret_variant(
        _evidence_with_three_references(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T15:00:00Z",
    )

    assert result["interpretation"] == "Supported by [R1][R2]."
    assert result["cited_reference_ids"] == ["R1", "R2"]


@pytest.mark.parametrize(
    ("grouped", "canonical", "reference_ids"),
    [
        ("[R1, R2]", "[R1][R2]", ["R1", "R2"]),
        ("[R1, R2, R3]", "[R1][R2][R3]", ["R1", "R2", "R3"]),
    ],
)
def test_comma_separated_citations_normalize_to_canonical_tokens(
    grouped: str,
    canonical: str,
    reference_ids: list[str],
) -> None:
    adapter = FakeLLMAdapter(_response(f"Supported by {grouped}."))

    result = interpret_variant(
        _evidence_with_three_references(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T15:00:00Z",
    )

    assert result["interpretation"] == f"Supported by {canonical}."
    assert result["cited_reference_ids"] == reference_ids


def test_truly_malformed_citation_still_fails_closed() -> None:
    adapter = FakeLLMAdapter(_response("Invalid group [R1; R2]."))

    with pytest.raises(
        VariantInterpretationError,
        match="malformed reference citation",
    ):
        interpret_variant(
            _evidence_with_three_references(),
            client=LLMClient(adapter),
            timestamp="2026-08-24T15:00:00Z",
        )


def test_normalizable_citation_does_not_trigger_structured_repair() -> None:
    adapter = FakeLLMAdapter(_response("Supported by [R1, R2]."))

    result = interpret_variant(
        _evidence_with_three_references(),
        client=LLMClient(adapter),
        timestamp="2026-08-24T15:00:00Z",
    )

    assert result["status"] == "success"
    assert len(adapter.requests) == 1
