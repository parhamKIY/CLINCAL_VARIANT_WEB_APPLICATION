"""Focused deterministic Stage 10 redacted failure-flow audit."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.evidence_readiness import build_evidence_readiness_audit, validate_evidence_readiness_audit
from backend.llm import LLMClient
from backend.report import build_evidence_object
from backend.variant_interpretation import interpret_variants
from tests.stage10_trace import TRACE_SCHEMA_VERSION, build_redacted_trace
from tests.test_pipeline import FakeLLMAdapter, TestEvidenceObject as EvidenceFactory, _variant_interpretation_response
from tests.test_stage6b_active_promotion import _fallback_candidate, _genebe_source


pytestmark = pytest.mark.stage59_testing_v3


def _audit(evidence: dict[str, object], state: str | None = None) -> dict[str, object]:
    audit = dict(build_evidence_readiness_audit(evidence, variant_index=0))
    if state is not None:
        audit["readiness_after_rescue"] = state
        audit = validate_evidence_readiness_audit(audit)
    return audit


def test_safe_fallback_and_valid_no_match_trace_remain_non_biological_missingness() -> None:
    fallback = build_evidence_object(_fallback_candidate())
    fallback_trace = build_redacted_trace(
        scenario_id="A", evidence=fallback, readiness=_audit(fallback),
        first_loss_stage="RETRIEVAL", cause="PROVIDER_OPERATIONAL_FAILURE", loss_expected=True,
    )
    assert fallback_trace["coverage"]["annotation"] == "DEGRADED"
    assert fallback_trace["final_disposition"] == "READY_WITH_LIMITATIONS"

    no_match = deepcopy(EvidenceFactory._complete_evidence_object())
    no_match["pathogenicity"]["expert_curated_variant_context"] = {"status": "no_match", "records": []}
    trace = build_redacted_trace(
        scenario_id="B", evidence=no_match, readiness=_audit(no_match, "READY_WITH_LIMITATIONS"),
        first_loss_stage="RETRIEVAL", cause="VALID_NO_MATCH", loss_expected=True,
    )
    assert trace["final_disposition"] == "READY_WITH_LIMITATIONS"


def test_transcript_conflict_and_identity_block_are_distinct_from_sparse_evidence() -> None:
    conflict = build_evidence_object(_fallback_candidate(genebe=_genebe_source(transcript="NM_000001.6")))
    trace = build_redacted_trace(
        scenario_id="D", evidence=conflict, readiness=_audit(conflict, "READY_WITH_LIMITATIONS"),
        first_loss_stage="COMPOSITION", cause="TRANSCRIPT_CONFLICT", loss_expected=True,
    )
    assert trace["coverage"]["annotation"] == "DEGRADED"
    assert trace["llm_manifest"]["shadow_composition"] == "EXCLUDED_BY_DESIGN"

    blocked = deepcopy(EvidenceFactory._complete_evidence_object())
    blocked_trace = build_redacted_trace(
        scenario_id="E", evidence=blocked, readiness=_audit(blocked, "MINIMUM_IDENTITY_FAILURE"),
        first_loss_stage="IDENTITY", cause="IDENTITY_FAILURE", loss_expected=True,
    )
    adapter = FakeLLMAdapter(_variant_interpretation_response())
    results = interpret_variants(
        [blocked],
        client=LLMClient(adapter),
        readiness_audits=[_audit(blocked, "MINIMUM_IDENTITY_FAILURE")],
    )
    assert blocked_trace["final_disposition"] == "BLOCKED"
    assert results[0]["status"] == "failed"
    assert results[0]["error_type"] == "invalid_request"
    assert adapter.requests == []


def test_trace_schema_and_interpretation_failure_are_isolated() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    traces = build_redacted_trace(
        scenario_id="LLM_FAILURE", evidence=evidence, readiness=_audit(evidence),
        first_loss_stage="LLM_INVOCATION", cause="LLM_INVOCATION_FAILURE", loss_expected=True,
    )
    assert traces["schema_version"] == TRACE_SCHEMA_VERSION
    assert traces["llm_manifest"]["stage7_coverage"] == "EXCLUDED_BY_DESIGN"
    assert traces["llm_manifest"]["stage8_technical_diagnostics"] == "EXCLUDED_BY_DESIGN"
