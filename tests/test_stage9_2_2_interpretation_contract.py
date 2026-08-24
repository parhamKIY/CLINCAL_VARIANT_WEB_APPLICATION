"""Stage 9.2.2 interpretation repair and reviewer-failure semantics."""

import json
import logging

import pytest

from backend.llm import LLMClient, LLMRequest, LLMRequestError, LLMResponse
from backend.logging_config import APP_LOGGER_NAME
from backend.variant_interpretation import interpret_variant, interpret_variants
from backend.variant_report import build_draft_variant_report
from frontend.warning_semantics import build_warning_notices
from test_pipeline import (
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _variant_interpretation_response,
)


def _malformed_citation_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Uncertain significance",
                "interpretation": "The supplied evidence is cited as [R1, R2].",
                "conflict_assessment": "The source assertions remain distinct.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="stage9-2-2-model",
        finish_reason="stop",
    )


class CitationAwareRepairAdapter:
    """Model double that repairs only when the retry explains citation syntax."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return _malformed_citation_response()
        if "one reference per bracket" in request.messages[1].content:
            return _variant_interpretation_response(model="stage9-2-2-repaired")
        return _malformed_citation_response()


def test_malformed_citation_repair_explains_the_rejected_contract(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = CitationAwareRepairAdapter()

    with caplog.at_level(logging.WARNING, logger=APP_LOGGER_NAME):
        result = interpret_variant(
            EvidenceFactory._complete_evidence_object(),
            client=LLMClient(adapter),
            timestamp="2026-08-24T08:30:00Z",
        )

    assert result["status"] == "success"
    assert result["response_model"] == "stage9-2-2-repaired"
    assert len(adapter.requests) == 2
    assert "schema_error=malformed_citation" in caplog.text


def test_reviewer_notice_distinguishes_schema_and_service_failures() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    schema_failure = interpret_variants(
        [evidence],
        client=LLMClient(
            SequenceLLMAdapter(
                [_malformed_citation_response(), _malformed_citation_response()]
            )
        ),
        timestamp="2026-08-24T08:30:00Z",
    )[0]
    service_failure = interpret_variants(
        [evidence],
        client=LLMClient(
            SequenceLLMAdapter(
                [LLMRequestError("provider unavailable", http_status=500)]
            )
        ),
        max_retries=0,
        timestamp="2026-08-24T08:30:00Z",
    )[0]

    schema_report = build_draft_variant_report(
        evidence,
        schema_failure,
        variant_index=0,
    )
    service_report = build_draft_variant_report(
        evidence,
        service_failure,
        variant_index=0,
    )
    schema_message = build_warning_notices(schema_report)[0]["message"]
    service_message = build_warning_notices(service_report)[0]["message"]

    assert "valid structured response" in schema_message
    assert "temporarily unavailable" in service_message
    assert schema_message != service_message
    assert "output_schema_failure" not in schema_message
    assert "http_5xx" not in service_message
