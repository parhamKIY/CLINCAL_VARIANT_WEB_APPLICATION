"""Stage 2 evidence compaction, isolation, and recovery acceptance tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from backend.database import (
    DATABASE_SCHEMA_VERSION,
    load_pipeline_state,
    save_complete_analysis,
    save_pipeline_state,
)
from backend.evidence_readiness import build_evidence_readiness_audit
from backend.final_disposition import build_final_disposition
from backend.llm import LLMClient, LLMRequest, LLMResponse
from backend.pipeline import (
    PIPELINE_SCHEMA_VERSION,
    _build_evidence_and_report,
    _finish_exception,
    create_pipeline_result,
    migrate_pipeline_schema33_to34,
    validate_pipeline_result,
)
from backend.report import (
    EVIDENCE_CONSTRUCTION_OUTCOME_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    MAX_EVIDENCE_SERIALIZED_BYTES,
    EvidenceObjectError,
    build_evidence_object,
    build_evidence_objects_isolated,
    sanitize_evidence_object,
)
from backend.variant_integrity import (
    build_variant_integrity_records,
    index_input_variants,
)
from backend.variant_interpretation import interpret_variant
from frontend.execution import RECOVERY_REQUEST_SCHEMA_VERSION
from tests.test_live_run_stage1_evidence_audit import (
    _live_shape_candidate,
    _population_fallback,
)


class _RecordingAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(
                {
                    "interpretation": "The supplied evidence supports cautious review.",
                    "conflict_assessment": "No meaningful conflict is present.",
                    "phenotype_conclusion": "partially supported",
                    "warnings": [],
                }
            ),
            model="stage2-test-model",
            finish_reason="stop",
        )


def _serialized_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    )


def _oversized_optional_candidate(index: int = 3) -> dict[str, object]:
    candidate = _live_shape_candidate(index)
    diseases = candidate["mydisease"]["diseases"]
    for synonym_index in range(120):
        diseases[synonym_index % len(diseases)]["synonyms"].append(
            f"Synthetic bounded disease synonym {synonym_index:03d} "
            + ("context " * 40)
        )
    candidate["conditional_enrichment"] = _population_fallback()
    candidate["sources"]["clingen"] = {
        "status": "unavailable",
        "provider": "ClinGen GenCC",
    }
    candidate["conditional_enrichment"]["literature"] = {
        "status": "no_match",
        "providers": {
            "litvar": {"status": "no_match"},
            "europe_pmc": {"status": "no_match"},
            "pubmed": {"status": "no_match"},
        },
        "articles": [],
        "warnings": [],
    }
    return candidate


def _isolated_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    candidates = [_live_shape_candidate(index) for index in range(3)]
    failed_position = int(candidates[1]["variant"]["pos"])
    real_builder = build_evidence_object

    def fail_one(candidate: object) -> dict[str, object]:
        assert isinstance(candidate, dict)
        variant = candidate["variant"]
        assert isinstance(variant, dict)
        if variant["pos"] == failed_position:
            raise EvidenceObjectError(
                "Synthetic per-variant contract failure.",
                evidence_step="evidence_object_construction",
                evidence_field="evidence",
                failure_code="contract_violation",
                failure_scope="per_variant",
            )
        return real_builder(candidate)

    monkeypatch.setattr("backend.report.build_evidence_object", fail_one)
    batch = build_evidence_objects_isolated(candidates)
    return candidates, batch


def test_live_oversized_optional_context_is_compacted_deterministically() -> None:
    candidate = _oversized_optional_candidate()
    original = deepcopy(candidate)

    first = build_evidence_object(candidate)
    second = build_evidence_object(candidate)

    assert first == second
    assert candidate == original
    assert _serialized_size(first) <= MAX_EVIDENCE_SERIALIZED_BYTES
    mydisease = first["phenotype_relationship"]["mydisease"]
    compaction = mydisease["compaction"]
    assert set(compaction) == {
        "schema_version",
        "policy",
        "applied",
        "section_budget_bytes",
        "omitted_item_count",
        "omitted_content_sha256",
    }
    assert compaction["schema_version"] == "1.0"
    assert compaction["policy"] == "mydisease_optional_context_v1"
    assert compaction["applied"] is True
    assert compaction["section_budget_bytes"] == 16384
    assert compaction["omitted_item_count"] > 0
    assert compaction["omitted_content_sha256"].startswith("sha256:")
    assert any(
        "MyDisease optional context was compacted" in warning
        for warning in mydisease["warnings"]
    )

    retained = mydisease["diseases"]
    source = candidate["mydisease"]["diseases"]
    assert [item["disease_id"] for item in retained] == [
        item["disease_id"] for item in source
    ]
    assert [item["gene_disease_relation"] for item in retained] == [
        item["gene_disease_relation"] for item in source
    ]
    assert [item["matched_patient_hpo_terms"] for item in retained] == [
        item["matched_patient_hpo_terms"] for item in source
    ]


def test_global_serialization_bound_remains_enforced() -> None:
    evidence = build_evidence_object(_live_shape_candidate(0))
    evidence["annotations"]["predictors"] = {
        f"required_predictor_{index:03d}": "context " * 70
        for index in range(200)
    }

    with pytest.raises(EvidenceObjectError) as raised:
        sanitize_evidence_object(evidence)

    assert raised.value.failure_code == "serialized_size_exceeded"
    assert raised.value.evidence_step == "serialization_bounds"


def test_isolated_construction_preserves_successful_siblings_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates, batch = _isolated_batch(monkeypatch)

    assert [item["variant"]["pos"] for item in batch["evidence_objects"]] == [
        candidates[0]["variant"]["pos"],
        candidates[2]["variant"]["pos"],
    ]
    assert [outcome["status"] for outcome in batch["outcomes"]] == [
        "success",
        "failed",
        "success",
    ]
    assert [outcome["evidence_object_index"] for outcome in batch["outcomes"]] == [
        0,
        None,
        1,
    ]
    assert batch["outcomes"][1]["failure_code"] == "contract_violation"
    assert all(
        outcome["schema_version"]
        == EVIDENCE_CONSTRUCTION_OUTCOME_SCHEMA_VERSION
        for outcome in batch["outcomes"]
    )
    assert len(batch["evidence_objects"]) == 2


def test_production_evidence_stage_retains_siblings_in_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [_live_shape_candidate(index) for index in range(3)]
    for candidate in candidates:
        candidate["variant"].pop("genotype", None)
        candidate.pop("raw_api_payload", None)
        for reference in candidate.get("references", []):
            if isinstance(reference, dict):
                reference.pop("raw_internal_detail", None)
    variants = index_input_variants(
        [
            {
                field: candidate["variant"][field]
                for field in ("chrom", "pos", "ref", "alt")
            }
            for candidate in candidates
        ]
    )
    result = create_pipeline_result()
    result["analysis_context"]["input_type"] = "manual"
    result["variant_count"] = len(variants)
    result["variants"] = variants
    result["phenotype_results"] = candidates
    result["variant_integrity_records"] = [
        dict(record)
        for record in build_variant_integrity_records(
            variants,
            variants,
            assembly="GRCh38",
        )
    ]
    failed_position = candidates[1]["variant"]["pos"]
    real_builder = build_evidence_object

    def fail_one(candidate: object) -> dict[str, object]:
        assert isinstance(candidate, dict)
        if candidate["variant"]["pos"] == failed_position:
            raise EvidenceObjectError("Synthetic per-variant contract failure.")
        return real_builder(candidate)

    monkeypatch.setattr("backend.report.build_evidence_object", fail_one)

    with pytest.raises(EvidenceObjectError) as raised:
        _build_evidence_and_report(
            result,
            llm_client=None,
            llm_model=None,
            report_dir=None,
        )
    finished = _finish_exception(
        result,
        stage="evidence",
        error=raised.value,
        default_code="evidence_object_failed",
        default_message="Evidence construction could not be completed.",
        default_recoverable=False,
    )

    assert finished["status"] == "partial"
    assert finished["workflow_state"] == "failed"
    assert [item["variant"]["pos"] for item in finished["evidence_objects"]] == [
        candidates[0]["variant"]["pos"],
        candidates[2]["variant"]["pos"],
    ]
    assert [
        item["status"]
        for item in finished["evidence_construction_outcomes"]
    ] == [
        "success",
        "failed",
        "success",
    ]
    assert finished["evidence_readiness"] == []
    assert finished["variant_interpretation_results"] == []
    assert finished["draft_variant_reports"] == []


def test_structurally_unsafe_identity_never_creates_fake_evidence() -> None:
    unsafe = _live_shape_candidate(0)
    unsafe["variant"]["ref"] = ""

    batch = build_evidence_objects_isolated([unsafe])

    assert batch["evidence_objects"] == []
    assert batch["outcomes"][0]["status"] == "failed"
    assert batch["outcomes"][0]["canonical_variant_identity"] is None
    assert batch["outcomes"][0]["evidence_object_index"] is None


def test_live_sparse_shape_reaches_ready_with_limitations_and_sanitized_llm() -> None:
    candidate = _oversized_optional_candidate(2)
    evidence = build_evidence_object(candidate)
    before = build_evidence_readiness_audit(evidence, variant_index=0)
    after = build_evidence_readiness_audit(
        evidence,
        variant_index=0,
        before_rescue=before,
    )
    disposition = build_final_disposition(evidence, after)
    adapter = _RecordingAdapter()

    result = interpret_variant(
        evidence,
        variant_index=0,
        client=LLMClient(adapter),
        timestamp="2026-08-20T00:00:00Z",
        readiness_audit=after,
    )

    assert after["readiness_after_rescue"] == "READY_WITH_LIMITATIONS"
    assert disposition["state"] == "READY_WITH_LIMITATIONS"
    assert evidence["annotations"]["vep"]["provider"] == "VariantValidator"
    assert evidence["annotations"]["vep"]["fallback_used"] is True
    assert evidence["consequence"] is None
    assert evidence["annotations"]["genebe"]["status"] == "unavailable"
    assert evidence["source_statuses"]["clingen"] == "unavailable"
    assert evidence["capability_results"]["clinvar_evidence"]["status"] == (
        "no_match"
    )
    assert evidence["capability_results"]["cspec_context"]["status"] == (
        "no_match"
    )
    assert evidence["capability_results"]["population_frequency"]["status"] == (
        "success"
    )
    assert evidence["capability_results"]["population_frequency"][
        "fallback_used"
    ] is True
    assert evidence["capability_results"]["literature"]["status"] == "no_match"
    assert evidence["phenotype_relationship"]["medgen_disease_hpo_context"][
        "status"
    ] == "success"
    assert evidence["phenotype_relationship"]["mydisease"]["diseases"][0][
        "phenotype_match_status"
    ] == "partial_match"
    assert result["status"] == "success"
    assert len(adapter.requests) == 1
    prompt = "\n".join(message.content for message in adapter.requests[0].messages)
    assert "candidate_diagnostics" not in prompt
    assert "shadow_composition" not in prompt
    assert "annotation_promotion" not in prompt
    assert "genotype" not in prompt.lower()


def test_partial_construction_outcomes_survive_persistence_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidates, batch = _isolated_batch(monkeypatch)
    variants = index_input_variants(
        [
            {
                field: candidate["variant"][field]
                for field in ("chrom", "pos", "ref", "alt")
            }
            for candidate in candidates
        ]
    )
    evidence_objects = batch["evidence_objects"]
    outcomes = batch["outcomes"]
    integrity = build_variant_integrity_records(
        variants,
        variants,
        assembly="GRCh38",
        evidence_objects=evidence_objects,
        evidence_construction_outcomes=outcomes,
    )
    result = create_pipeline_result()
    result["analysis_context"]["input_type"] = "manual"
    result["variant_count"] = len(variants)
    result["variants"] = variants
    result["evidence_objects"] = [dict(item) for item in evidence_objects]
    result["evidence_construction_outcomes"] = [dict(item) for item in outcomes]
    result["variant_integrity_records"] = [dict(item) for item in integrity]
    result["status"] = "partial"
    result["workflow_state"] = "failed"
    result["current_stage"] = "evidence"
    result["errors"] = [
        {
            "stage": "evidence",
            "code": "evidence_object_partial_failure",
            "message": "Evidence construction failed for one canonical variant.",
            "recoverable": False,
        }
    ]
    validated = validate_pipeline_result(result)
    assert [
        record["evidence_allele_identity"] is not None
        for record in validated["variant_integrity_records"]
    ] == [True, False, True]

    database_path = tmp_path / "stage2.sqlite3"
    analysis = save_complete_analysis(
        status=validated["status"],
        candidates=validated["variants"],
        evidence_objects=validated["evidence_objects"],
        database_path=database_path,
    )
    validated["analysis_id"] = analysis["analysis_id"]
    saved = save_pipeline_state(validated, database_path=database_path)
    loaded = load_pipeline_state(analysis["analysis_id"], database_path=database_path)

    assert loaded == saved
    assert [
        item["status"] for item in loaded["evidence_construction_outcomes"]
    ] == ["success", "failed", "success"]
    assert loaded["evidence_objects"] == evidence_objects


def test_pipeline_33_migration_adds_success_outcomes_without_rerun() -> None:
    evidence = build_evidence_object(_live_shape_candidate(0))
    variants = index_input_variants([dict(evidence["variant"])])
    current = create_pipeline_result()
    current["analysis_context"]["input_type"] = "manual"
    current["variant_count"] = 1
    current["variants"] = variants
    current["evidence_objects"] = [dict(evidence)]
    current["variant_integrity_records"] = [
        dict(record)
        for record in build_variant_integrity_records(
            variants,
            variants,
            assembly="GRCh38",
            evidence_objects=[evidence],
        )
    ]
    legacy = deepcopy(current)
    legacy["schema_version"] = "3.3"
    legacy.pop("evidence_construction_outcomes")

    migrated = migrate_pipeline_schema33_to34(legacy)

    assert migrated is not None
    assert migrated["schema_version"] == "3.4"
    assert migrated["evidence_objects"] == [evidence]
    assert [
        item["status"] for item in migrated["evidence_construction_outcomes"]
    ] == ["success"]


def test_stage2_schema_versions_are_bounded_to_the_approved_extension() -> None:
    assert PIPELINE_SCHEMA_VERSION == "3.4"
    assert EVIDENCE_CONSTRUCTION_OUTCOME_SCHEMA_VERSION == "1.0"
    assert EVIDENCE_SCHEMA_VERSION == "2.5"
    assert DATABASE_SCHEMA_VERSION == 4
    assert RECOVERY_REQUEST_SCHEMA_VERSION == 3
