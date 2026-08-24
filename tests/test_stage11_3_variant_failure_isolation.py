"""Stage 11.3 variant-level failure-isolation acceptance tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

import backend.pipeline as pipeline_module
import backend.variant_interpretation as interpretation_module
from backend.database import (
    load_pipeline_state,
    save_complete_analysis,
    save_pipeline_state,
)
from backend.llm import LLMClient, LLMResponse
from backend.pipeline import _build_evidence_and_report, create_pipeline_result
from backend.pipeline import confirm_reviewed_evidence, finalize_reviewed_analysis
from backend.report import EvidenceObjectError, build_evidence_object
from backend.variant_integrity import (
    build_variant_integrity_records,
    index_input_variants,
)
from backend.variant_interpretation import interpret_variants
from frontend.results import build_partial_analysis_presentation
from test_pipeline import SequenceLLMAdapter, TestEvidenceObject as EvidenceFactory


pytestmark = pytest.mark.stage11_3_batch_resilience


def _valid_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Uncertain significance",
                "interpretation": "Available evidence supports cautious review.",
                "conflict_assessment": "No unresolved conflict was identified.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="stage11-3-test-model",
        finish_reason="stop",
    )


def _three_candidate_result() -> tuple[dict[str, object], list[dict[str, object]]]:
    candidates: list[dict[str, object]] = []
    variants: list[dict[str, object]] = []
    for index in range(3):
        candidate = deepcopy(EvidenceFactory._pipeline_candidate())
        variant = candidate["variant"]
        assert isinstance(variant, dict)
        variant["pos"] = 166848215 + index
        variant.pop("genotype", None)
        candidate.pop("raw_api_payload", None)
        for reference in candidate.get("references", []):
            if isinstance(reference, dict):
                reference.pop("raw_internal_detail", None)
        candidates.append(candidate)
        variants.append(
            {
                field: variant[field]
                for field in ("chrom", "pos", "ref", "alt")
            }
        )
    indexed = index_input_variants(variants)
    result = create_pipeline_result()
    result["analysis_context"]["input_type"] = "manual"
    result["variant_count"] = len(indexed)
    result["variants"] = indexed
    result["phenotype_results"] = candidates
    result["variant_integrity_records"] = [
        dict(item)
        for item in build_variant_integrity_records(
            indexed,
            indexed,
            assembly="GRCh38",
        )
    ]
    return result, candidates


def test_unexpected_interpretation_exception_isolated_from_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = EvidenceFactory._complete_evidence_object()
    second = deepcopy(first)
    second["variant"] = {"chrom": "1", "pos": 101, "ref": "C", "alt": "T"}
    real_interpret = interpretation_module.interpret_variant
    calls = 0

    def fail_first(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic variant-local defect")
        return real_interpret(*args, **kwargs)

    monkeypatch.setattr(interpretation_module, "interpret_variant", fail_first)
    results = interpret_variants(
        [first, second],
        client=LLMClient(SequenceLLMAdapter([_valid_response()])),
        max_retries=0,
    )

    assert [item["status"] for item in results] == ["failed", "success"]
    assert results[0]["error_type"] == "unknown_failure"
    assert results[1]["interpretation"] is not None


def test_evidence_failure_allows_sibling_interpretation_and_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, candidates = _three_candidate_result()
    failed_position = candidates[1]["variant"]["pos"]
    real_builder = build_evidence_object

    def fail_middle(candidate: object) -> dict[str, object]:
        assert isinstance(candidate, dict)
        if candidate["variant"]["pos"] == failed_position:
            raise EvidenceObjectError("synthetic per-variant construction failure")
        return real_builder(candidate)

    def keep_successes(
        selected: object,
        _evidence: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "variants": [deepcopy(item) for item in selected],
            "triggered_count": 0,
            "population_status": "skipped",
            "litvar_status": "skipped",
            "europe_pmc_status": "skipped",
            "pubmed_status": "skipped",
        }

    monkeypatch.setattr("backend.report.build_evidence_object", fail_middle)
    monkeypatch.setattr(pipeline_module, "enrich_conditionally", keep_successes)
    _build_evidence_and_report(
        result,
        llm_client=LLMClient(
            SequenceLLMAdapter([_valid_response(), _valid_response()])
        ),
        llm_model=None,
        report_dir=None,
    )

    assert result["status"] == "partial"
    assert result["workflow_state"] == "awaiting_final_review"
    assert [item["status"] for item in result["evidence_construction_outcomes"]] == [
        "success",
        "failed",
        "success",
    ]
    assert [item["variant_index"] for item in result["evidence_readiness"]] == [0, 2]
    assert [
        item["variant_index"] for item in result["variant_interpretation_results"]
    ] == [0, 2]
    assert [item["variant_index"] for item in result["draft_variant_reports"]] == [
        0,
        2,
    ]


def test_partial_report_projection_keeps_failed_variant_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, candidates = _three_candidate_result()
    failed_position = candidates[1]["variant"]["pos"]
    real_builder = build_evidence_object

    def fail_middle(candidate: object) -> dict[str, object]:
        assert isinstance(candidate, dict)
        if candidate["variant"]["pos"] == failed_position:
            raise EvidenceObjectError("synthetic per-variant construction failure")
        return real_builder(candidate)

    monkeypatch.setattr("backend.report.build_evidence_object", fail_middle)
    monkeypatch.setattr(
        pipeline_module,
        "enrich_conditionally",
        lambda selected, _evidence, **_kwargs: {
            "variants": [deepcopy(item) for item in selected],
            "triggered_count": 0,
            "population_status": "skipped",
            "litvar_status": "skipped",
            "europe_pmc_status": "skipped",
            "pubmed_status": "skipped",
        },
    )
    _build_evidence_and_report(
        result,
        llm_client=LLMClient(
            SequenceLLMAdapter([_valid_response(), _valid_response()])
        ),
        llm_model=None,
        report_dir=None,
    )

    presentation = build_partial_analysis_presentation(result)

    assert presentation["successful_variant_count"] == 2
    assert presentation["failed_variant_count"] == 1
    assert presentation["rows"][1]["Workflow result"] == "Failed"
    assert presentation["rows"][1]["Failure category"] == "Evidence construction"


def test_all_successful_batch_contract_is_unchanged() -> None:
    first = EvidenceFactory._complete_evidence_object()
    second = deepcopy(first)
    second["variant"] = {"chrom": "1", "pos": 101, "ref": "C", "alt": "T"}

    results = interpret_variants(
        [first, second],
        client=LLMClient(
            SequenceLLMAdapter([_valid_response(), _valid_response()])
        ),
        max_retries=0,
    )

    assert [item["status"] for item in results] == ["success", "success"]
    assert [item["variant_index"] for item in results] == [0, 1]


def test_partial_batch_final_report_keeps_failure_as_limitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, candidates = _three_candidate_result()
    failed_position = candidates[1]["variant"]["pos"]
    real_builder = build_evidence_object

    def fail_middle(candidate: object) -> dict[str, object]:
        assert isinstance(candidate, dict)
        if candidate["variant"]["pos"] == failed_position:
            raise EvidenceObjectError("synthetic per-variant construction failure")
        return real_builder(candidate)

    monkeypatch.setattr("backend.report.build_evidence_object", fail_middle)
    monkeypatch.setattr(
        pipeline_module,
        "enrich_conditionally",
        lambda selected, _evidence, **_kwargs: {
            "variants": [deepcopy(item) for item in selected],
            "triggered_count": 0,
            "population_status": "skipped",
            "litvar_status": "skipped",
            "europe_pmc_status": "skipped",
            "pubmed_status": "skipped",
        },
    )
    _build_evidence_and_report(
        result,
        llm_client=LLMClient(
            SequenceLLMAdapter([_valid_response(), _valid_response()])
        ),
        llm_model=None,
        report_dir=None,
    )
    confirmed = confirm_reviewed_evidence(
        result,
        result["evidence_review_reports"],
        timestamp="2027-08-24T00:00:00Z",
    )
    finalized = finalize_reviewed_analysis(
        confirmed,
        timestamp="2027-08-24T00:01:00Z",
    )

    report = finalized["final_clinical_report"]
    assert report is not None
    assert [item["variant_index"] for item in report["main_findings"]] == [0, 2]
    assert any(
        "Variant 2 was not included because evidence construction failed."
        == limitation
        for limitation in report["limitations"]
    )
    assert finalized["status"] == "partial"
    assert finalized["workflow_state"] == "completed"


def test_sparse_downstream_records_survive_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result, candidates = _three_candidate_result()
    failed_position = candidates[1]["variant"]["pos"]
    real_builder = build_evidence_object

    def fail_middle(candidate: object) -> dict[str, object]:
        assert isinstance(candidate, dict)
        if candidate["variant"]["pos"] == failed_position:
            raise EvidenceObjectError("synthetic per-variant construction failure")
        return real_builder(candidate)

    monkeypatch.setattr("backend.report.build_evidence_object", fail_middle)
    monkeypatch.setattr(
        pipeline_module,
        "enrich_conditionally",
        lambda selected, _evidence, **_kwargs: {
            "variants": [deepcopy(item) for item in selected],
            "triggered_count": 0,
            "population_status": "skipped",
            "litvar_status": "skipped",
            "europe_pmc_status": "skipped",
            "pubmed_status": "skipped",
        },
    )
    _build_evidence_and_report(
        result,
        llm_client=LLMClient(
            SequenceLLMAdapter([_valid_response(), _valid_response()])
        ),
        llm_model=None,
        report_dir=None,
    )
    database_path = tmp_path / "stage11_3.sqlite3"
    analysis = save_complete_analysis(
        status=result["status"],
        candidates=result["variants"],
        evidence_objects=result["evidence_objects"],
        database_path=database_path,
    )
    result["analysis_id"] = analysis["analysis_id"]
    pipeline_module._sync_variant_report_records(result)
    pipeline_module._sync_variant_integrity_records(result)

    saved = save_pipeline_state(result, database_path=database_path)
    loaded = load_pipeline_state(
        analysis["analysis_id"], database_path=database_path
    )

    assert loaded == saved
    assert [
        item["variant_index"] for item in loaded["draft_variant_reports"]
    ] == [0, 2]
    assert [
        item["draft_report_id"] is not None
        for item in loaded["variant_integrity_records"]
    ] == [True, False, True]
