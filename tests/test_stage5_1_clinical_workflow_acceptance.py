"""Stage 5.1 end-to-end clinical workflow acceptance scenarios."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
import json
import logging
from pathlib import Path

from openpyxl import Workbook
import pytest

from backend.database import load_pipeline_state, save_pipeline_state
from backend.excel_processing import parse_excel_input_records
from backend.final_clinical_report import (
    render_final_clinical_report_markdown,
    validate_final_clinical_report,
)
from backend.llm import LLMClient, LLMResponse
from backend.logging_config import (
    APP_LOGGER_NAME,
    configure_logging,
    shutdown_logging,
)
from backend.pipeline import (
    attach_input_preprocessing_results,
    confirm_reviewed_evidence,
    finalize_reviewed_analysis,
    run_analysis,
    run_annovar_like_input_processing,
    validate_pipeline_result,
)
from backend.variant_integrity import stable_allele_identity
from frontend.results import build_partial_analysis_presentation
from frontend.xlsx_selection import select_excel_input_records
from test_live_run_stage1_evidence_audit import _population_fallback
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _mydisease_evidence_stub,
    _variant_interpretation_response,
)
from test_stage4_3_acceptance import _fallback_annotation


pytestmark = [pytest.mark.regression, pytest.mark.stage51_acceptance]

_PRIVATE_SENTINEL = "stage51-private-patient-context"
_UNSUPPORTED_SENTINEL = "PS3_UNSUPPORTED_STAGE51"


def _workbook_payload() -> bytes:
    workbook = Workbook()
    selected = workbook.active
    selected.title = "Selected"
    selected.append(("Chr", "Start", "End", "Ref", "Alt", "Quality", "Filter"))
    selected.append(("1", 100, 100, "A", "G", 90.0, "PASS"))
    selected.append(("2", 200, 200, "C", "T", 80.0, "PASS"))
    selected.append(("3", 300, 300, "G", "A", 70.0, "PASS"))
    ignored = workbook.create_sheet("Ignored clinical context")
    ignored.append(("patient_name", _PRIVATE_SENTINEL))
    payload = BytesIO()
    workbook.save(payload)
    return payload.getvalue()


def _successful_annotation(variant: dict[str, object]) -> dict[str, object]:
    candidate = EvidenceFactory._pipeline_candidate()
    candidate["variant"] = deepcopy(variant)
    candidate["references"] = []
    candidate["warnings"] = []
    sources = candidate["sources"]
    sources["vep"].update(
        {
            "provider": "Ensembl VEP",
            "provider_role": "primary",
            "fallback_used": False,
        }
    )
    sources["myvariant"].update(
        {
            "provider": "MyVariant.info",
            "variant_id": (
                f"chr{variant['chrom']}:g.{variant['pos']}"
                f"{variant['ref']}>{variant['alt']}"
            ),
        }
    )
    sources["clinvar"]["provider"] = "NCBI ClinVar"
    sources["clingen"]["provider"] = "ClinGen/GenCC"
    sources["genebe"] = {
        "status": "success",
        "provider": "GeneBe",
        "provider_role": "primary",
        "fallback_used": False,
        "transcript": candidate["transcript"],
        "automated_acmg_classification": "Pathogenic",
        "automated_acmg_criteria": ["PS3", "PM2"],
    }
    sources["cspec"] = {
        "status": "success",
        "provider": "ClinGen CSpec Registry",
        "provider_role": "primary",
        "fallback_used": False,
        "specifications": [
            {
                "specification_id": "SCN1A-EP",
                "title": "SCN1A VCEP specification",
                "version": "1.0",
                "status": "Released",
                "matched_disease_ids": ["MONDO:0100062"],
                "scope_match": "gene_and_disease",
            }
        ],
        "rule_logic_applied": False,
    }
    return candidate


def _install_offline_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    degraded: bool,
) -> None:
    def fake_annotate(
        variants: object,
        **_: object,
    ) -> list[dict[str, object]]:
        factory = _fallback_annotation if degraded else _successful_annotation
        return [
            factory(dict(variant))
            for variant in variants  # type: ignore[union-attr]
        ]

    def fake_mydisease(
        variants: object,
        _patient_hpo_terms: object,
        **_: object,
    ) -> dict[str, object]:
        enriched: list[dict[str, object]] = []
        for variant in variants:  # type: ignore[union-attr]
            item = deepcopy(dict(variant))
            item["mydisease"] = _mydisease_evidence_stub(item.get("gene"))
            enriched.append(item)
        return {
            "variants": enriched,
            "status": "no_association",
            "message": "No direct gene-disease association was retained.",
            "request_attempts": len(enriched),
            "variant_count": len(enriched),
            "variants_with_evidence": 0,
            "no_association_count": len(enriched),
            "unsupported_count": 0,
            "unavailable_count": 0,
            "invalid_response_count": 0,
        }

    def fake_conditional(
        variants: object,
        _evidence: object,
        **_: object,
    ) -> dict[str, object]:
        enriched = []
        for variant in variants:  # type: ignore[union-attr]
            item = deepcopy(dict(variant))
            if degraded:
                item["conditional_enrichment"] = _population_fallback()
            enriched.append(item)
        return {
            "variants": enriched,
            "status": "partial" if degraded else "skipped",
            "message": (
                "gnomAD was unavailable; UCSC gnomAD fallback succeeded."
                if degraded
                else "No variant triggered conditional enrichment."
            ),
            "variant_count": len(enriched),
            "triggered_count": len(enriched) if degraded else 0,
            "population_status": "available" if degraded else "skipped",
            "litvar_status": "skipped",
            "europe_pmc_status": "skipped",
            "pubmed_status": "skipped",
        }

    monkeypatch.setattr("backend.pipeline.annotate_variants", fake_annotate)
    monkeypatch.setattr("backend.pipeline.enrich_with_mydisease", fake_mydisease)
    monkeypatch.setattr("backend.pipeline.enrich_conditionally", fake_conditional)


def _finalization_times(result: dict[str, object]) -> tuple[str, str]:
    reports = result["evidence_review_reports"]
    base = max(
        datetime.fromisoformat(report["updated_at"].replace("Z", "+00:00"))
        for report in reports  # type: ignore[union-attr]
    )
    return tuple(
        (base + timedelta(minutes=offset)).isoformat().replace("+00:00", "Z")
        for offset in (1, 2)
    )


def _execute_complete_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    adapter: FakeLLMAdapter | SequenceLLMAdapter,
    degraded: bool = False,
) -> tuple[dict[str, object], str]:
    _install_offline_boundaries(monkeypatch, degraded=degraded)
    records = parse_excel_input_records(
        _workbook_payload(),
        worksheet_name="Selected",
    )
    selected = select_excel_input_records(records, (2, 3, 4))
    prepared = run_annovar_like_input_processing(selected, phenotypes=[])
    canonical_variants = [
        {key: value for key, value in variant.items() if key != "input_index"}
        for variant in prepared["variants"]
    ]
    database_path = tmp_path / "stage5_1.sqlite3"
    log_path = tmp_path / "stage5_1.log"
    configure_logging(level="INFO", log_path=log_path, force=True)
    try:
        result = run_analysis(
            vcf_path=None,
            manual_variants=canonical_variants,
            phenotypes=[],
            llm_client=LLMClient(adapter),
            input_type="excel",
            database_path=database_path,
        )
        result = attach_input_preprocessing_results(
            result,
            prepared["input_preprocessing_results"],
        )
        confirmed_at, finalized_at = _finalization_times(result)
        confirmed = confirm_reviewed_evidence(
            result,
            result["evidence_review_reports"],
            timestamp=confirmed_at,
        )
        completed = finalize_reviewed_analysis(
            confirmed,
            timestamp=finalized_at,
        )
        save_pipeline_state(completed, database_path=database_path)
        restored = load_pipeline_state(
            completed["analysis_id"],
            database_path=database_path,
        )
        for handler in logging.getLogger(APP_LOGGER_NAME).handlers:
            handler.flush()
        log_text = log_path.read_text(encoding="utf-8")
    finally:
        shutdown_logging()
    return dict(validate_pipeline_result(restored)), log_text


def _assert_complete_traceability(result: dict[str, object]) -> None:
    assert result["variant_count"] == 3
    assert len(result["input_preprocessing_results"]) == 3
    assert len(result["variants"]) == 3
    assert len(result["annotations"]) == 3
    assert len(result["evidence_objects"]) == 3
    assert len(result["variant_interpretation_results"]) == 3
    assert len(result["draft_variant_reports"]) == 3
    assert len(result["variant_report_records"]) == 3
    assert len(result["reviewed_evidence_packages"]) == 3
    assert isinstance(result["analysis_id"], str)
    final_report = validate_final_clinical_report(result["final_clinical_report"])
    assert final_report["analysis_id"] == result["analysis_id"]
    assert final_report["metadata"]["selected_variant_indexes"] == [0, 1, 2]

    for index in range(3):
        evidence = result["evidence_objects"][index]
        identity = stable_allele_identity(
            evidence["variant"],
            assembly=evidence["assembly"],
        )
        assert result["input_preprocessing_results"][index][
            "canonical_variant_identity"
        ] == identity
        assert result["variant_integrity_records"][index][
            "evidence_allele_identity"
        ] == identity
        assert result["variant_interpretation_results"][index]["variant"] == (
            evidence["variant"]
        )
        draft = result["draft_variant_reports"][index]
        lifecycle = result["variant_report_records"][index]
        section = final_report["variant_sections"][index]
        assert draft["report_id"] == lifecycle["report_id"] == section["report_id"]
        assert lifecycle["analysis_id"] == result["analysis_id"]
        assert evidence["provenance"]["providers"]
        interpretation = result["variant_interpretation_results"][index]
        assert interpretation["configured_model"]
        if interpretation["status"] == "success":
            assert interpretation["response_model"]
        else:
            assert interpretation["response_model"] is None
        assert result["variant_interpretation_results"][index]["prompt_version"]


def _assert_safe_boundaries(
    result: dict[str, object],
    log_text: str,
    adapter: FakeLLMAdapter | SequenceLLMAdapter,
) -> None:
    serialized = json.dumps(result, ensure_ascii=False)
    markdown = render_final_clinical_report_markdown(
        result["final_clinical_report"]
    )
    prompts = "\n".join(
        message.content
        for request in adapter.requests
        for message in request.messages
    )
    assert "run_id=run-" in log_text
    assert f"analysis_id={result['analysis_id']}" in log_text
    assert "Missing evidence is unknown, not benign" not in log_text
    for prohibited in (_PRIVATE_SENTINEL, "raw_api_payload"):
        assert prohibited not in log_text
        assert prohibited not in serialized
        assert prohibited not in markdown
        assert prohibited not in prompts
    for internal_only in (
        "shadow_composition",
        "circuit_open",
        "request_attempts",
    ):
        assert internal_only not in log_text
        assert internal_only not in markdown
        assert internal_only not in prompts


def _unsupported_interpretation_response() -> LLMResponse:
    payload = json.loads(_variant_interpretation_response().content)
    payload["acmg_criteria"] = [_UNSUPPORTED_SENTINEL]
    return LLMResponse(
        content=json.dumps(payload),
        model="stage5-1-invalid-model",
        finish_reason="stop",
    )


def test_scenario_a_fully_successful_workflow_reaches_final_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeLLMAdapter(
        _variant_interpretation_response(model="stage5-1-success-model")
    )

    result, log_text = _execute_complete_workflow(
        tmp_path,
        monkeypatch,
        adapter=adapter,
    )

    assert result["status"] == "success"
    assert result["workflow_state"] == "completed"
    assert [
        item["status"] for item in result["variant_interpretation_results"]
    ] == ["success", "success", "success"]
    assert len(adapter.requests) == 3
    _assert_complete_traceability(result)
    _assert_safe_boundaries(result, log_text, adapter)
    assert "acmg_criteria" not in json.dumps(
        result["variant_interpretation_results"]
    )


def test_scenario_b_provider_degradation_preserves_semantics_to_final_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeLLMAdapter(
        _variant_interpretation_response(
            model="stage5-1-degraded-model",
            phenotype_conclusion="phenotype evidence unavailable",
        )
    )

    result, log_text = _execute_complete_workflow(
        tmp_path,
        monkeypatch,
        adapter=adapter,
        degraded=True,
    )

    assert result["status"] == "partial"
    assert result["workflow_state"] == "completed"
    assert all(
        item["status"] == "success"
        for item in result["variant_interpretation_results"]
    )
    for evidence, draft in zip(
        result["evidence_objects"],
        result["draft_variant_reports"],
        strict=True,
    ):
        annotation = evidence["annotations"]
        capabilities = evidence["capability_results"]
        assert annotation["vep"]["provider"] == "VariantValidator"
        assert annotation["vep"]["fallback_used"] is True
        assert annotation["vep"]["primary_failure"] == "timeout"
        assert annotation["genebe"]["status"] == "unavailable"
        assert "automated_acmg_criteria" not in annotation["genebe"]
        assert capabilities["clinvar_evidence"]["status"] == "no_match"
        clinvar_lineage = next(
            record
            for record in evidence["provenance"]["lineage"]
            if record["evidence_path"] == "pathogenicity.clinvar"
        )
        assert clinvar_lineage["status"] == "not_found"
        assert clinvar_lineage["evidence_present"] is False
        population = capabilities["population_frequency"]
        assert population["status"] == "success"
        assert population["provider"] == "ucsc_gnomad"
        assert population["provider_role"] == "fallback"
        assert population["fallback_for"] == "gnomad"
        assert population["primary_failure"] == "forbidden"
        providers = draft["machine_original_report"]["provenance"]["providers"]
        assert any("VariantValidator" in provider for provider in providers)
        assert any("UCSC gnomAD" in provider for provider in providers)
        assert draft["machine_original_report"]["limitations"]
    prompts = "\n".join(
        message.content
        for request in adapter.requests
        for message in request.messages
    )
    assert "Operational unavailability is not biological absence" in prompts
    assert '"status":"unavailable"' in prompts
    assert '"status":"no_match"' in prompts
    final_providers = result["final_clinical_report"]["method_data_sources"][
        "providers"
    ]
    assert any("VariantValidator" in provider for provider in final_providers)
    assert any("UCSC gnomAD" in provider for provider in final_providers)
    _assert_complete_traceability(result)
    _assert_safe_boundaries(result, log_text, adapter)


def test_scenario_c_schema_invalid_variant_is_isolated_through_final_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _variant_interpretation_response(model="stage5-1-partial-model")
    invalid = _unsupported_interpretation_response()
    adapter = SequenceLLMAdapter([valid, invalid, invalid, valid])

    result, log_text = _execute_complete_workflow(
        tmp_path,
        monkeypatch,
        adapter=adapter,
    )

    assert result["status"] == "partial"
    assert result["workflow_state"] == "completed"
    interpretations = result["variant_interpretation_results"]
    assert [item["status"] for item in interpretations] == [
        "success",
        "failed",
        "success",
    ]
    assert interpretations[1]["error_type"] == "output_schema_failure"
    assert interpretations[1]["interpretation"] is None
    assert len(adapter.requests) == 4
    assert len(result["evidence_objects"]) == 3
    assert len(result["draft_variant_reports"]) == 3
    failed_draft = result["draft_variant_reports"][1]
    assert failed_draft["machine_original_report"]["variant_interpretation"][
        "status"
    ] == "failed"
    final_report = result["final_clinical_report"]
    final_statuses = [
        item["interpretation_status"] for item in final_report["main_findings"]
    ]
    assert final_statuses == [
        "success",
        "failed",
        "success",
    ]
    assert final_report["main_findings"][1]["interpretation_narrative"] is None
    presentation = build_partial_analysis_presentation(result)
    assert presentation["headline"] == "Analysis completed partially"
    assert presentation["successful_variant_count"] == 2
    assert presentation["failed_variant_count"] == 1
    assert presentation["rows"][1]["Failure category"] == (
        "Automated interpretation"
    )
    assert _UNSUPPORTED_SENTINEL not in log_text
    assert _UNSUPPORTED_SENTINEL not in json.dumps(result, ensure_ascii=False)
    assert _UNSUPPORTED_SENTINEL not in json.dumps(presentation, ensure_ascii=False)
    _assert_complete_traceability(result)
    _assert_safe_boundaries(result, log_text, adapter)
