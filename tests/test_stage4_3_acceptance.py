"""Stage 4.3 audit-trail, performance, and workflow acceptance."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
import logging
from pathlib import Path
import re

from openpyxl import Workbook
import pytest

from backend.excel_processing import parse_excel_input_records
from backend.gene_identity import resolve_gene_identity
from backend.llm import LLMClient, LLMTimeoutError
from backend.logging_config import (
    APP_LOGGER_NAME,
    configure_logging,
    shutdown_logging,
)
from backend.pipeline import (
    PIPELINE_STAGE_ORDER,
    attach_input_preprocessing_results,
    run_analysis,
    run_annovar_like_input_processing,
)
from backend.variant_integrity import stable_allele_identity
from frontend.results import build_partial_analysis_presentation
from frontend.xlsx_selection import select_excel_input_records
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _mydisease_evidence_stub,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.regression

_PRIVATE_SENTINEL = "patient-name private-llm-timeout-detail"


def _workbook_payload() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Selected"
    worksheet.append(("Chr", "Start", "End", "Ref", "Alt", "Quality", "Filter"))
    worksheet.append(("1", 100, 100, "A", "G", 90.0, "PASS"))
    worksheet.append(("2", 200, 200, "C", "T", 80.0, "PASS"))
    worksheet.append(("3", 300, 300, "G", "A", 70.0, "PASS"))
    payload = BytesIO()
    workbook.save(payload)
    return payload.getvalue()


def _fallback_annotation(variant: dict[str, object]) -> dict[str, object]:
    annotation = EvidenceFactory._pipeline_candidate()
    annotation["variant"] = deepcopy(variant)
    annotation["references"] = []
    annotation["population_frequency"] = None
    annotation["hpo_terms"] = []
    annotation["matched_hpo_terms"] = []
    annotation["phenotype_match_count"] = 0
    annotation["phenotype_score"] = None
    normalized_variant = {
        "assembly": "GRCh38",
        "chrom": variant["chrom"],
        "pos": variant["pos"],
        "ref": variant["ref"],
        "alt": variant["alt"],
    }
    transcript = str(annotation["transcript"])
    hgvs_c = f"{transcript}:c.100C>T"
    annotation["hgvsc"] = hgvs_c
    annotation["hgvsp"] = annotation["protein_change"]
    annotation["sources"] = {
        "vep": {
            "status": "partial",
            "provider": "VariantValidator",
            "provider_role": "fallback",
            "fallback_used": True,
            "fallback_for": "ensembl_vep",
            "primary_provider": "Ensembl VEP",
            "primary_failure": "timeout",
            "fallback_provider": "VariantValidator",
            "fallback_status": "success",
            "fallback_failure": None,
            "source_type": "validation_mapping_fallback",
            "upstream_sources": ["VariantValidator"],
            "normalized_variant": normalized_variant,
            "validated_variant": normalized_variant,
            "validated_genomic_hgvs": None,
            "validated_gene": annotation["gene"],
            "validated_gene_id": annotation["gene_id"],
            "validated_transcript": transcript,
            "validated_transcript_hgvs": hgvs_c,
            "validated_protein_hgvs": annotation["protein_change"],
            "selected_record": hgvs_c,
            "consequence_available": False,
            "validation_warnings": [],
            "most_severe_consequence": None,
            "transcript_consequences": [],
        },
        "genebe": {
            "status": "unavailable",
            "primary_failure": "unavailable",
        },
        "myvariant": {"status": "not_found"},
        "clinvar": {"status": "not_found"},
        "clingen": {
            "status": "unavailable",
            "primary_failure": "unavailable",
            "curations": [],
        },
        "cspec": {"status": "no_match"},
    }
    annotation["warnings"] = [
        "Ensembl VEP was unavailable; VariantValidator retained exact mapping.",
        "GeneBe was unavailable; automated ACMG context was not added.",
    ]
    annotation["gene_identity_resolution"] = resolve_gene_identity(annotation)
    return annotation


def _install_offline_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_annotate(
        variants: object,
        **_: object,
    ) -> list[dict[str, object]]:
        return [
            _fallback_annotation(dict(variant))
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
            "message": "No validated direct gene-disease association was retained.",
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
        enriched = [
            deepcopy(dict(variant))
            for variant in variants  # type: ignore[union-attr]
        ]
        return {
            "variants": enriched,
            "status": "skipped",
            "message": "No variant triggered conditional enrichment.",
            "variant_count": len(enriched),
            "triggered_count": 0,
            "population_status": "skipped",
            "litvar_status": "skipped",
            "europe_pmc_status": "skipped",
            "pubmed_status": "skipped",
        }

    monkeypatch.setattr("backend.pipeline.annotate_variants", fake_annotate)
    monkeypatch.setattr("backend.pipeline.enrich_with_mydisease", fake_mydisease)
    monkeypatch.setattr("backend.pipeline.enrich_conditionally", fake_conditional)


def _serialized_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def test_xlsx_to_partial_draft_reports_is_traceable_and_measurable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise every production stage with one isolated LLM failure."""

    _install_offline_boundaries(monkeypatch)
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
    adapter = SequenceLLMAdapter(
        [
            _variant_interpretation_response(
                model="stage4-3-model",
                phenotype_conclusion="phenotype evidence unavailable",
            ),
            LLMTimeoutError(_PRIVATE_SENTINEL),
            _variant_interpretation_response(
                model="stage4-3-model",
                phenotype_conclusion="phenotype evidence unavailable",
            ),
        ]
    )
    log_path = tmp_path / "stage4_3_pipeline.log"
    configure_logging(level="INFO", log_path=log_path, force=True)
    try:
        result = run_analysis(
            vcf_path=None,
            manual_variants=canonical_variants,
            phenotypes=[],
            llm_client=LLMClient(adapter),
            input_type="excel",
            database_path=tmp_path / "stage4_3.sqlite3",
        )
        result = attach_input_preprocessing_results(
            result,
            prepared["input_preprocessing_results"],
        )
        for handler in logging.getLogger(APP_LOGGER_NAME).handlers:
            handler.flush()
        log_text = log_path.read_text(encoding="utf-8")
    finally:
        shutdown_logging()

    assert result["status"] == "partial"
    assert result["current_stage"] == "completed"
    assert result["variant_count"] == 3
    assert len(result["annotations"]) == 3
    assert len(result["evidence_objects"]) == 3
    assert [item["status"] for item in result["variant_interpretation_results"]] == [
        "success",
        "failed",
        "success",
    ]
    assert len(result["draft_variant_reports"]) == 3
    assert len(result["variant_report_records"]) == 3
    assert isinstance(result["analysis_id"], str)

    for index in range(3):
        preprocessing = result["input_preprocessing_results"][index]
        integrity = result["variant_integrity_records"][index]
        evidence = result["evidence_objects"][index]
        interpretation = result["variant_interpretation_results"][index]
        draft = result["draft_variant_reports"][index]
        lifecycle = result["variant_report_records"][index]
        identity = stable_allele_identity(
            evidence["variant"],
            assembly=evidence["assembly"],
        )

        assert preprocessing["canonical_variant_identity"] == identity
        assert integrity["parser_allele_identity"] == identity
        assert interpretation["variant"] == evidence["variant"]
        assert draft["variant_index"] == index
        assert lifecycle["report_id"] == draft["report_id"]
        assert lifecycle["analysis_id"] == result["analysis_id"]
        assert lifecycle["report_data"]["analysis_id"] == result["analysis_id"]
        assert lifecycle["lifecycle_state"] == "draft"
        assert lifecycle["report_data"]["interpretation"]["model"] == (
            interpretation["configured_model"]
        )
        assert lifecycle["report_data"]["interpretation"]["prompt_version"] == (
            interpretation["prompt_version"]
        )
        vep = evidence["annotations"]["vep"]
        assert vep["provider"] == "VariantValidator"
        assert vep["provider_role"] == "fallback"
        assert vep["primary_provider"] == "Ensembl VEP"
        assert vep["primary_failure"] == "timeout"
        assert any(
            "Fallback:" in provider and "VariantValidator" in provider
            for provider in draft["machine_original_report"]["provenance"]["providers"]
        )
        assert draft["machine_original_report"]["limitations"]

    presentation = build_partial_analysis_presentation(result)
    assert presentation["headline"] == "Analysis completed partially"
    assert presentation["successful_variant_count"] == 2
    assert presentation["failed_variant_count"] == 1
    assert presentation["rows"][1]["Failure category"] == "Automated interpretation"
    assert "Retry automated interpretation" in presentation["rows"][1]["Next action"]

    stage_events = {
        stage: (status, int(duration))
        for stage, status, duration in re.findall(
            r"event=pipeline_stage_finished stage=([a-z_]+) "
            r"status=([a-z_]+) duration_ms=(\d+)",
            log_text,
        )
    }
    assert set(stage_events) == set(PIPELINE_STAGE_ORDER)
    assert all(duration >= 0 for _, duration in stage_events.values())
    assert log_text.count("event=llm_call ") == 3
    assert "event=draft_variant_reports_prepared report_count=3" in log_text
    assert "event=evidence_build_finished evidence_object_count=3" in log_text
    assert "serialization_validation=passed" in log_text
    assert "event=analysis_persistence_started variant_count=3" in log_text
    persistence_match = re.search(
        r"event=analysis_persistence_finished status=assigned "
        r"analysis_id=(analysis-[0-9a-f]{32}) variant_count=3 duration_ms=(\d+)",
        log_text,
    )
    assert persistence_match is not None
    assert persistence_match.group(1) == result["analysis_id"]
    assert _PRIVATE_SENTINEL not in log_text
    assert _PRIVATE_SENTINEL not in json.dumps(result, ensure_ascii=False)
    assert _PRIVATE_SENTINEL not in json.dumps(presentation, ensure_ascii=False)

    evidence_sizes = [_serialized_size(item) for item in result["evidence_objects"]]
    metrics = {
        "stage_duration_ms": {
            stage: stage_events[stage][1] for stage in PIPELINE_STAGE_ORDER
        },
        "variant_count": result["variant_count"],
        "llm_call_count": log_text.count("event=llm_call "),
        "draft_report_count": len(result["draft_variant_reports"]),
        "persistence_duration_ms": int(persistence_match.group(2)),
        "evidence_serialized_bytes_total": sum(evidence_sizes),
        "evidence_serialized_bytes_max": max(evidence_sizes),
    }
    (tmp_path / "stage4_3_metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def test_persistence_failure_has_bounded_timing_without_private_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep terminal persistence failures measurable and secret-free."""

    _install_offline_boundaries(monkeypatch)

    def fail_persistence(*_: object, **__: object) -> None:
        raise RuntimeError(_PRIVATE_SENTINEL)

    monkeypatch.setattr(
        "backend.pipeline._persist_terminal_result",
        fail_persistence,
    )
    log_path = tmp_path / "stage4_3_persistence_failure.log"
    configure_logging(level="INFO", log_path=log_path, force=True)
    try:
        with pytest.raises(RuntimeError, match=_PRIVATE_SENTINEL):
            run_analysis(
                vcf_path=None,
                manual_variants=[
                    {
                        "chrom": "1",
                        "pos": 100,
                        "ref": "A",
                        "alt": "G",
                        "qual": None,
                        "filter": "PASS",
                    }
                ],
                phenotypes=[],
                llm_client=LLMClient(
                    FakeLLMAdapter(
                        _variant_interpretation_response(
                            model="stage4-3-model",
                            phenotype_conclusion=(
                                "phenotype evidence unavailable"
                            ),
                        )
                    )
                ),
                database_path=tmp_path / "not-written.sqlite3",
            )
        for handler in logging.getLogger(APP_LOGGER_NAME).handlers:
            handler.flush()
        log_text = log_path.read_text(encoding="utf-8")
    finally:
        shutdown_logging()

    assert "event=analysis_persistence_started variant_count=1" in log_text
    assert re.search(
        r"event=analysis_persistence_finished status=failed "
        r"analysis_id=None variant_count=1 duration_ms=\d+",
        log_text,
    )
    assert "event=analysis_aborted error_type=RuntimeError" in log_text
    assert _PRIVATE_SENTINEL not in log_text
