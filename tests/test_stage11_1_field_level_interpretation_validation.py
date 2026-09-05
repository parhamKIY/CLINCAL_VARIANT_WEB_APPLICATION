"""Stage 11.1 field-level interpretation validation contract."""

from __future__ import annotations

import json

import pytest

from backend.llm import LLMClient, LLMResponse
from backend.report_data_projection import build_report_data_from_draft
from backend.variant_interpretation import (
    interpret_variant,
    interpret_variants,
    regenerate_variant_interpretation,
    validate_variant_interpretation_result,
)
from backend.variant_report import build_draft_variant_report
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
)


_TIMESTAMP = "2026-08-24T18:00:00Z"


def _response(**overrides: object) -> LLMResponse:
    payload: dict[str, object] = {
        "ai_classification": "Likely pathogenic",
        "interpretation": "Strong evidence supports this draft assessment.",
        "conflict_assessment": "No major source conflict was identified.",
        "phenotype_conclusion": "partially supported",
        "warnings": [],
    }
    payload.update(overrides)
    return LLMResponse(
        content=json.dumps(payload),
        model="stage11-1-model",
        finish_reason="stop",
    )


@pytest.mark.parametrize(
    "phenotype_conclusion",
    [
        "unsupported free-form conclusion",
        ["supported"],
        {"value": "supported"},
        None,
        1,
        True,
    ],
    ids=["text", "list", "object", "null", "number", "boolean"],
)
def test_invalid_phenotype_conclusion_preserves_valid_core_without_repair(
    phenotype_conclusion: object,
) -> None:
    adapter = FakeLLMAdapter(
        _response(phenotype_conclusion=phenotype_conclusion)
    )

    evidence = EvidenceFactory._complete_evidence_object()
    result = interpret_variant(
        evidence,
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["ai_classification"] == "Likely pathogenic"
    assert result["interpretation"] == (
        "Strong evidence supports this draft assessment."
    )
    assert result["conflict_assessment"] == (
        "No major source conflict was identified."
    )
    assert result["phenotype_conclusion"] is None
    assert result["field_validation"]["ai_classification"] == "valid"
    assert result["field_validation"]["interpretation"] == "valid"
    assert result["field_validation"]["conflict_assessment"] == "valid"
    assert result["field_validation"]["phenotype_conclusion"] == "invalid"
    assert len(adapter.requests) == 1

    draft = build_draft_variant_report(evidence, result, variant_index=0)
    report_data = build_report_data_from_draft(
        draft, analysis_id="analysis-" + "4" * 32
    )
    assert report_data["conclusive_result"]["classification"] == "Likely pathogenic"
    assert report_data["interpretation"]["current_reviewer_interpretation"] == (
        "Strong evidence supports this draft assessment."
    )


def test_regeneration_preserves_core_when_phenotype_conclusion_is_malformed() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    prior = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(_response())),
        timestamp=_TIMESTAMP,
    )
    adapter = FakeLLMAdapter(
        _response(
            phenotype_conclusion=["supported"],
            interpretation="The retained evidence supports the regenerated draft.",
        )
    )
    result = regenerate_variant_interpretation(
        evidence,
        prior,
        client=LLMClient(adapter),
        timestamp="2026-08-24T18:01:00Z",
    )

    assert result["status"] == "success"
    assert result["ai_classification"] == "Likely pathogenic"
    assert result["interpretation"] == (
        "The retained evidence supports the regenerated draft."
    )
    assert result["phenotype_conclusion"] is None
    assert result["field_validation"]["phenotype_conclusion"] == "invalid"
    assert result["generation_history"][0]["prior_result"] == prior
    assert len(adapter.requests) == 1


def test_malformed_auxiliary_citation_excludes_only_affected_field() -> None:
    adapter = FakeLLMAdapter(
        _response(
            conflict_assessment=(
                "The auxiliary assessment used malformed citations [R1; R2]."
            )
        )
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["ai_classification"] == "Likely pathogenic"
    assert result["interpretation"] == (
        "Strong evidence supports this draft assessment."
    )
    assert result["conflict_assessment"] is None
    assert result["cited_reference_ids"] == []
    assert result["field_validation"]["conflict_assessment"] == "invalid"
    assert result["field_validation"]["citations"] == "invalid"
    assert len(adapter.requests) == 1


def test_unknown_citation_gets_one_strict_repair_without_losing_core() -> None:
    invalid = _response(
        conflict_assessment="Unsupported evidence reference [R99]."
    )
    adapter = SequenceLLMAdapter([invalid, _response()])

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["ai_classification"] == "Likely pathogenic"
    assert result["interpretation"] == (
        "Strong evidence supports this draft assessment."
    )
    assert len(adapter.requests) == 2
    assert "unknown citation" in adapter.requests[1].messages[1].content


def test_repeated_unknown_citation_still_fails_closed() -> None:
    invalid = _response(
        interpretation="Unsupported evidence reference [R99]."
    )
    adapter = SequenceLLMAdapter([invalid, invalid])

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "output_schema_failure"
    assert result["ai_classification"] is None
    assert result["interpretation"] is None
    assert len(adapter.requests) == 2


@pytest.mark.parametrize(
    "missing_field",
    ["ai_classification", "interpretation"],
)
def test_missing_core_field_still_requires_repair_or_failure(
    missing_field: str,
) -> None:
    payload = json.loads(_response().content)
    payload.pop(missing_field)
    invalid = LLMResponse(
        content=json.dumps(payload),
        model="stage11-1-model",
        finish_reason="stop",
    )
    adapter = SequenceLLMAdapter([invalid, invalid])

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "output_schema_failure"
    assert result["ai_classification"] is None
    assert result["interpretation"] is None
    assert len(adapter.requests) == 2


def test_completely_invalid_json_keeps_recovery_and_failure_behavior() -> None:
    invalid = LLMResponse(
        content="not-json",
        model="stage11-1-model",
        finish_reason="stop",
    )
    adapter = SequenceLLMAdapter([invalid, invalid])

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "output_parse_failure"
    assert len(adapter.requests) == 2


def test_historical_v12_result_loads_with_inferred_field_statuses() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    historical = dict(
        interpret_variant(
            evidence,
            client=LLMClient(FakeLLMAdapter(_response())),
            timestamp=_TIMESTAMP,
        )
    )
    historical["schema_version"] = "1.2"
    historical.pop("phenotype_conclusion")
    historical.pop("field_validation")

    normalized = validate_variant_interpretation_result(
        historical,
        evidence=evidence,
    )

    assert normalized["schema_version"] == "1.2"
    assert normalized["ai_classification"] == "Likely pathogenic"
    assert normalized["interpretation"] == (
        "Strong evidence supports this draft assessment."
    )
    assert normalized["phenotype_conclusion"] is None
    assert normalized["field_validation"]["phenotype_conclusion"] == (
        "not_available"
    )
