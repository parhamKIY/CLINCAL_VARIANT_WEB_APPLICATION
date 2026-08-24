"""Deterministic acceptance audit for the interpretation-model boundary."""

from __future__ import annotations

import json

import pytest

from backend.llm import LLMClient, LLMResponse, LLMTimeoutError
from backend.report import build_evidence_object
from backend.variant_interpretation import (
    SUPPORTED_VARIANT_INTERPRETATION_PROMPT_VERSIONS,
    VARIANT_INTERPRETATION_RESPONSE_SCHEMA,
    interpret_variant,
    interpret_variants,
)
from test_live_run_stage2_evidence_resilience import (
    _oversized_optional_candidate,
)
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
)


_TIMESTAMP = "2026-08-22T12:00:00Z"


def _response(
    *,
    interpretation: str = "Available evidence supports cautious review.",
    warnings: list[str] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Uncertain significance",
                "interpretation": interpretation,
                "conflict_assessment": "No meaningful conflict is present.",
                "phenotype_conclusion": "partially supported",
                "warnings": warnings or [],
            }
        ),
        model="boundary-audit-model",
        finish_reason="stop",
    )


def _live_sparse_evidence(index: int = 2) -> dict[str, object]:
    return build_evidence_object(_oversized_optional_candidate(index))


def _prompt(adapter: FakeLLMAdapter | SequenceLLMAdapter, index: int = 0) -> str:
    return "\n".join(
        message.content for message in adapter.requests[index].messages
    )


def test_output_schema_is_strict_and_has_no_model_confidence_field() -> None:
    schema = VARIANT_INTERPRETATION_RESPONSE_SCHEMA.schema

    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "ai_classification",
        "interpretation",
        "conflict_assessment",
        "phenotype_conclusion",
        "warnings",
    ]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == set(schema["required"])
    assert set(properties["phenotype_conclusion"]["enum"]) == {
        "supported",
        "partially supported",
        "no supported association found",
        "phenotype evidence unavailable",
    }
    assert set(properties["ai_classification"]["enum"]) == {
        "Pathogenic",
        "Likely pathogenic",
        "Uncertain significance",
        "Likely benign",
        "Benign",
    }
    assert "confidence" not in properties
    assert "variant-interpretation-v1.3" in (
        SUPPORTED_VARIANT_INTERPRETATION_PROMPT_VERSIONS
    )


def test_a_valid_evidence_object_returns_structured_interpretation() -> None:
    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(FakeLLMAdapter(_response())),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["interpretation"] == (
        "Available evidence supports cautious review."
    )
    assert result["error_type"] is None


def test_b_sparse_live_shape_continues_with_limits_and_safe_projection() -> None:
    candidate = _oversized_optional_candidate(2)
    population = candidate["conditional_enrichment"]["population_frequency"]
    assert isinstance(population, dict)
    population.update(
        {
            "http_status": 200,
            "request_attempts": 2,
            "circuit_open": False,
            "primary_http_status": 503,
            "primary_request_attempts": 2,
            "primary_circuit_open": True,
            "fallback_http_status": 200,
            "intermediate_http_status": 503,
        }
    )
    evidence = build_evidence_object(candidate)
    adapter = FakeLLMAdapter(
        _response(warnings=["Provider coverage is incomplete."])
    )

    result = interpret_variant(
        evidence,
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    prompt = _prompt(adapter)
    assert '"provider":"VariantValidator"' in prompt
    assert '"provider":"GeneBe","status":"unavailable"' in prompt
    assert '"provider_role":"fallback"' in prompt
    assert '"primary_failure":"forbidden"' in prompt
    assert '"omitted_item_count":' in prompt
    for forbidden in (
        '"attempts":',
        '"http_status":',
        '"request_attempts":',
        '"circuit_open":',
        '"primary_http_status":',
        '"primary_request_attempts":',
        '"primary_circuit_open":',
        '"fallback_http_status":',
        '"intermediate_http_status":',
        '"candidate_diagnostics":',
        '"enrichment_decision":',
        '"human_review":',
        '"post_review":',
        '"omitted_content_sha256":',
        '"section_budget_bytes":',
        '"shadow_composition":',
        '"annotation_promotion":',
    ):
        assert forbidden not in prompt


def test_c_missing_optional_evidence_is_unknown_not_negative() -> None:
    adapter = FakeLLMAdapter(
        _response(
            interpretation=(
                "Available evidence is limited; unavailable and no-match "
                "observations remain limitations rather than negative evidence."
            ),
            warnings=["GeneBe and ClinVar evidence are unavailable or absent."],
        )
    )

    result = interpret_variant(
        _live_sparse_evidence(),
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert "rather than negative evidence" in result["interpretation"]
    prompt = _prompt(adapter)
    assert (
        "Missing evidence is unknown, not benign or negative evidence."
        in prompt
    )
    assert "case context, not independent scientific evidence" in prompt
    assert "Operational unavailability is not biological absence" in prompt
    assert '"status":"unavailable"' in prompt
    assert '"status":"no_match"' in prompt


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("not-json", "output_parse_failure"),
        (
            json.dumps(
                {
                    "interpretation": "Missing required fields.",
                    "warnings": [],
                }
            ),
            "output_schema_failure",
        ),
    ],
    ids=("d-invalid-json", "e-schema-invalid-json"),
)
def test_d_e_invalid_responses_fail_safely_after_one_repair(
    content: str,
    expected_error: str,
) -> None:
    invalid = LLMResponse(
        content=content,
        model="boundary-audit-model",
        finish_reason="stop",
    )
    adapter = SequenceLLMAdapter([invalid, invalid])

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == expected_error
    assert result["interpretation"] is None
    assert len(adapter.requests) == 2


def test_f_timeout_is_bounded_and_returns_safe_variant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.llm.sleep", lambda _: None)
    adapter = SequenceLLMAdapter(
        [LLMTimeoutError("first timeout"), LLMTimeoutError("last timeout")]
    )

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        max_retries=1,
        timestamp=_TIMESTAMP,
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "request_timeout"
    assert result["interpretation"] is None
    assert len(adapter.requests) == 2


def test_g_hallucinated_unsupported_output_field_is_rejected() -> None:
    payload = json.loads(_response().content)
    payload["acmg_criteria"] = ["PS1"]
    hallucinated = LLMResponse(
        content=json.dumps(payload),
        model="boundary-audit-model",
        finish_reason="stop",
    )
    adapter = SequenceLLMAdapter([hallucinated, hallucinated])

    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )[0]

    assert result["status"] == "failed"
    assert result["error_type"] == "output_schema_failure"
    assert result["interpretation"] is None
    assert len(adapter.requests) == 2


def test_recent_multi_variant_live_shape_preserves_order_and_provenance() -> None:
    evidence_objects = [
        _live_sparse_evidence(0),
        _live_sparse_evidence(2),
    ]
    adapter = SequenceLLMAdapter([_response(), _response()])

    results = interpret_variants(
        evidence_objects,
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert [result["status"] for result in results] == ["success", "success"]
    assert [result["variant"]["pos"] for result in results] == [
        evidence["variant"]["pos"] for evidence in evidence_objects
    ]
    assert len(adapter.requests) == 2
    for request_index in range(2):
        prompt = _prompt(adapter, request_index)
        assert '"provider":"VariantValidator"' in prompt
        assert '"provider":"GeneBe","status":"unavailable"' in prompt
        assert '"provider_role":"fallback"' in prompt
        assert '"provider":"NCBI MedGen"' in prompt
        assert '"provider":"MyDisease.info"' in prompt
