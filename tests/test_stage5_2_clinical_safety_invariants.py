"""Stage 5.2 clinical safety and invariant regression audit."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.error_handling import PipelineResultError
from backend.llm import LLMClient, LLMResponse
from backend.pipeline import validate_pipeline_result
from backend.provider_resilience import (
    ProviderContractError,
    build_capability_result,
    capability_availability,
    should_trigger_fallback,
)
from backend.report import build_evidence_object, validate_evidence_object
from backend.variant_interpretation import interpret_variant, interpret_variants
from backend.variant_report import (
    DraftVariantReportError,
    build_draft_variant_report,
    validate_draft_variant_report,
)
from frontend.results import build_partial_analysis_presentation
from test_live_run_stage2_evidence_resilience import (
    _oversized_optional_candidate,
)
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _variant_interpretation_response,
)
from test_stage5_1_clinical_workflow_acceptance import (
    _execute_complete_workflow,
)


_TIMESTAMP = "2026-08-23T00:00:00Z"


def _response_with_extra_field(field: str, value: object) -> LLMResponse:
    payload = json.loads(_variant_interpretation_response().content)
    payload[field] = value
    return LLMResponse(
        content=json.dumps(payload),
        model="stage5-2-invalid-model",
        finish_reason="stop",
    )


def _failed_interpretation(extra_field: str, value: object) -> dict[str, object]:
    invalid = _response_with_extra_field(extra_field, value)
    adapter = SequenceLLMAdapter([invalid, invalid])
    result = interpret_variants(
        [EvidenceFactory._complete_evidence_object()],
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )[0]
    assert len(adapter.requests) == 2
    return result


def _section(report: dict[str, object], source: str) -> dict[str, object]:
    sections = report["machine_original_report"]["evidence_sections"]
    return next(item for item in sections if item["source"] == source)


def test_missing_evidence_is_unknown_not_negative_across_llm_and_report() -> None:
    evidence = build_evidence_object(_oversized_optional_candidate(2))
    adapter = FakeLLMAdapter(
        _variant_interpretation_response(model="stage5-2-sparse-model")
    )

    interpretation = interpret_variant(
        evidence,
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )
    draft = build_draft_variant_report(
        evidence,
        interpretation,
        variant_index=0,
    )

    assert evidence["pathogenicity"]["automated_acmg_classification"] is None
    assert evidence["pathogenicity"]["clinvar_classification"] is None
    prompt = "\n".join(
        message.content for message in adapter.requests[0].messages
    )
    assert "Missing evidence is unknown, not benign or negative evidence." in prompt
    assert "no_match is not negative evidence" in prompt
    genebe = _section(draft, "GeneBe")
    clinvar = _section(draft, "NCBI ClinVar")
    assert genebe["status"] == "unavailable"
    assert clinvar["status"] == "no_match"
    assert all(
        item["label"] != "Automated ACMG classification"
        for item in genebe["items"]
    )
    assert all(item["label"] != "Significance" for item in clinvar["items"])


def test_provider_unavailable_is_not_no_match_and_only_outage_can_fallback() -> None:
    unavailable = build_capability_result(
        capability="population_frequency",
        status="unavailable",
        provider="gnomad",
        method="exact_allele_lookup",
    )
    no_match = build_capability_result(
        capability="population_frequency",
        status="no_match",
        provider="gnomad",
        method="exact_allele_lookup",
    )
    fallback = build_capability_result(
        capability="population_frequency",
        status="success",
        provider="ucsc_gnomad",
        provider_role="fallback",
        fallback_for="gnomad",
        primary_failure="timeout",
        method="exact_mapped_allele_lookup",
    )

    assert capability_availability(unavailable) == "unavailable"
    assert capability_availability(no_match) == "no_match"
    assert should_trigger_fallback("unavailable") is True
    assert should_trigger_fallback("timeout") is True
    assert should_trigger_fallback("no_match") is False
    assert fallback["fallback_used"] is True
    assert fallback["primary_failure"] == "timeout"
    with pytest.raises(ProviderContractError, match="operational primary failure"):
        build_capability_result(
            capability="population_frequency",
            status="success",
            provider="ucsc_gnomad",
            provider_role="fallback",
            fallback_for="gnomad",
            primary_failure="no_match",
            method="exact_mapped_allele_lookup",
        )


def test_unsupported_acmg_output_is_rejected_after_one_bounded_repair() -> None:
    result = _failed_interpretation("acmg_criteria", ["PS3"])

    assert result["status"] == "failed"
    assert result["error_type"] == "output_schema_failure"
    assert result["interpretation"] is None


def test_llm_hallucinated_provider_findings_field_is_rejected() -> None:
    result = _failed_interpretation(
        "provider_findings",
        {"GeneBe": "Pathogenic"},
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "output_schema_failure"
    assert result["interpretation"] is None


def test_failed_variant_report_remains_explicit_and_cannot_gain_machine_claims() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    failed = _failed_interpretation("diagnosis", "Unsupported diagnosis")

    draft = build_draft_variant_report(evidence, failed, variant_index=0)
    machine = draft["machine_original_report"]
    assert machine["variant_interpretation"]["status"] == "failed"
    assert machine["variant_interpretation"]["narrative"] is None
    assert machine["variant_interpretation"]["failure_type"] == (
        "output_schema_failure"
    )
    assert any(
        "interpretation is unavailable" in limitation
        for limitation in machine["limitations"]
    )

    fabricated = deepcopy(draft)
    fabricated["machine_original_report"]["variant_interpretation"][
        "narrative"
    ] = "Fabricated machine interpretation."
    fabricated["reviewed_report"] = deepcopy(
        fabricated["machine_original_report"]
    )
    with pytest.raises(DraftVariantReportError, match="integrity check"):
        validate_draft_variant_report(
            fabricated,
            evidence=evidence,
            interpretation=failed,
        )


def test_partial_analysis_remains_visible_and_cannot_be_relabelled_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _variant_interpretation_response(model="stage5-2-partial-model")
    invalid = _response_with_extra_field("diagnosis", "Unsupported diagnosis")
    adapter = SequenceLLMAdapter([valid, invalid, invalid, valid])

    result, _ = _execute_complete_workflow(
        tmp_path,
        monkeypatch,
        adapter=adapter,
    )

    assert result["status"] == "partial"
    assert [
        item["status"] for item in result["variant_interpretation_results"]
    ] == ["success", "failed", "success"]
    assert len(result["evidence_objects"]) == 3
    assert len(result["draft_variant_reports"]) == 3
    presentation = build_partial_analysis_presentation(result)
    assert presentation["successful_variant_count"] == 2
    assert presentation["failed_variant_count"] == 1
    assert result["final_clinical_report"]["main_findings"][1][
        "interpretation_status"
    ] == "failed"

    mislabeled = deepcopy(result)
    mislabeled["status"] = "success"
    with pytest.raises(
        PipelineResultError,
        match="failed variant interpretations must remain partial",
    ):
        validate_pipeline_result(mislabeled)


def test_compaction_preserves_identity_meaning_provenance_and_omission_audit() -> None:
    candidate = _oversized_optional_candidate(2)
    source_diseases = candidate["mydisease"]["diseases"]

    evidence = validate_evidence_object(build_evidence_object(candidate))

    assert evidence["variant"] == {
        field: candidate["variant"][field]
        for field in ("chrom", "pos", "ref", "alt")
    }
    assert evidence["assembly"] == "GRCh38"
    retained = evidence["phenotype_relationship"]["mydisease"]
    assert [item["disease_id"] for item in retained["diseases"]] == [
        item["disease_id"] for item in source_diseases
    ]
    assert [item["gene_disease_relation"] for item in retained["diseases"]] == [
        item["gene_disease_relation"] for item in source_diseases
    ]
    assert [item["matched_patient_hpo_terms"] for item in retained["diseases"]] == [
        item["matched_patient_hpo_terms"] for item in source_diseases
    ]
    assert retained["compaction"]["applied"] is True
    assert retained["compaction"]["omitted_item_count"] > 0
    assert retained["compaction"]["omitted_content_sha256"].startswith("sha256:")
    assert any("compacted deterministically" in item for item in retained["warnings"])

    capabilities = evidence["capability_results"]
    assert capabilities["clinvar_evidence"]["status"] == "no_match"
    assert capabilities["population_frequency"]["status"] == "success"
    assert capabilities["population_frequency"]["fallback_used"] is True
    assert capabilities["population_frequency"]["primary_failure"] == "forbidden"
    disease_lineage = next(
        item
        for item in evidence["provenance"]["lineage"]
        if item["evidence_path"] == "phenotype_relationship.mydisease"
    )
    assert disease_lineage["evidence_present"] is True
    assert disease_lineage["upstream_sources"] == ["HPO", "MONDO"]
