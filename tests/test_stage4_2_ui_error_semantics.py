"""Stage 4.2 user-facing error and partial-result semantics."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

import pyarrow.ipc as pyarrow_ipc
from streamlit.testing.v1 import AppTest

from backend.error_handling import (
    PipelineInputError,
    map_pipeline_exception,
    safe_ui_error_message,
)
from backend.llm import LLMRequestError
from backend.logging_config import (
    APP_LOGGER_NAME,
    configure_logging,
    shutdown_logging,
)
from backend.report import EvidenceObjectError
from frontend.results import (
    build_evidence_source_presentations,
    build_partial_analysis_presentation,
)
from frontend.ui import _result_status


def _partial_results_app() -> None:
    from frontend.results import render_analysis_results

    render_analysis_results(  # type: ignore[arg-type]
        {
            "status": "partial",
            "current_stage": "evidence",
            "variant_count": 2,
            "variants": [
                {"chrom": "1", "pos": 100, "ref": "A", "alt": "G"},
                {"chrom": "2", "pos": 200, "ref": "C", "alt": "T"},
            ],
            "annotations": [],
            "phenotype_results": [],
            "evidence_objects": [],
            "evidence_construction_outcomes": [
                {"variant_index": 0, "status": "success"},
                {
                    "variant_index": 1,
                    "status": "failed",
                    "failure_code": "serialized_size_exceeded",
                },
            ],
            "variant_interpretation_results": [],
            "input_preprocessing_results": [],
            "warnings": [],
        }
    )


def test_backend_errors_map_to_workflow_safe_messages() -> None:
    input_error = map_pipeline_exception(
        PipelineInputError(
            "vcf_path and manual_variants violated an internal contract"
        ),
        stage="input",
        default_code="invalid_input",
        default_message="The analysis input is invalid.",
        default_recoverable=False,
    )
    evidence_error = map_pipeline_exception(
        EvidenceObjectError("serialized_size_exceeded"),
        stage="evidence",
        default_code="evidence_object_failed",
        default_message="Evidence construction failed.",
        default_recoverable=False,
    )
    llm_error = map_pipeline_exception(
        LLMRequestError("HTTP 400 with private provider detail"),
        stage="llm",
        default_code="llm_interpretation_failed",
        default_message="Interpretation failed.",
        default_recoverable=True,
    )

    assert input_error["message"] == (
        "The analysis input is incomplete or invalid. Review the selected "
        "input and phenotype terms, then try again."
    )
    assert evidence_error["message"] == (
        "Evidence construction could not be completed for this variant."
    )
    assert llm_error["message"] == (
        "Automated interpretation could not be completed. Technical "
        "details were recorded. Try again later."
    )
    rendered = " ".join(
        (
            input_error["message"],
            evidence_error["message"],
            llm_error["message"],
        )
    )
    assert "vcf_path" not in rendered
    assert "serialized_size_exceeded" not in rendered
    assert "HTTP 400" not in rendered


def test_safe_mapper_retains_diagnostics_only_in_logs(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "stage4-2.log"
    configure_logging(level="INFO", log_path=log_path, force=True)
    try:
        public_error = map_pipeline_exception(
            LLMRequestError("HTTP 400 private response body"),
            stage="llm",
            default_code="llm_interpretation_failed",
            default_message="Interpretation failed.",
            default_recoverable=True,
        )
        for handler in logging.getLogger(APP_LOGGER_NAME).handlers:
            handler.flush()
        contents = log_path.read_text(encoding="utf-8")
    finally:
        shutdown_logging()

    assert public_error["code"] == "llm_interpretation_failed"
    assert "event=user_safe_error stage=llm" in contents
    assert "error_type=LLMRequestError" in contents
    assert "HTTP 400" not in contents
    assert "private response body" not in contents


def test_partial_analysis_identifies_successful_and_failed_variants() -> None:
    result = {
        "status": "partial",
        "current_stage": "evidence",
        "variants": [
            {"chrom": "1", "pos": 100, "ref": "A", "alt": "G"},
            {"chrom": "2", "pos": 200, "ref": "C", "alt": "T"},
        ],
        "evidence_construction_outcomes": [
            {
                "variant_index": 0,
                "status": "success",
                "failure_code": None,
            },
            {
                "variant_index": 1,
                "status": "failed",
                "failure_code": "serialized_size_exceeded",
            },
        ],
        "variant_interpretation_results": [],
    }

    presentation = build_partial_analysis_presentation(result)

    assert presentation["headline"] == "Analysis completed partially"
    assert presentation["successful_variant_count"] == 1
    assert presentation["failed_variant_count"] == 1
    assert presentation["rows"][0]["Workflow result"] == "Evidence retained"
    assert presentation["rows"][1] == {
        "Variant": "2:200:C:T",
        "Workflow result": "Failed",
        "Failure category": "Evidence construction",
        "Explanation": (
            "Evidence construction could not be completed for this variant."
        ),
        "Next action": (
            "Review the variant input and retained technical record before "
            "retrying."
        ),
    }
    assert "serialized_size_exceeded" not in str(presentation)


def test_partial_analysis_render_shows_counts_and_safe_failure_row() -> None:
    app = AppTest.from_function(_partial_results_app).run(timeout=10)

    assert not app.exception
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["Successful variants"] == "1"
    assert metrics["Failed variants"] == "1"
    assert any(
        item.value == "Analysis completed partially"
        for item in app.subheader
    )
    partial_table = next(
        pyarrow_ipc.open_stream(
            BytesIO(dataframe.proto.arrow_data.data)
        ).read_all()
        for dataframe in app.dataframe
        if "Failure category"
        in pyarrow_ipc.open_stream(
            BytesIO(dataframe.proto.arrow_data.data)
        ).read_all().column_names
    )
    rows = partial_table.to_pylist()
    assert rows[1]["Variant"] == "2:200:C:T"
    assert rows[1]["Failure category"] == "Evidence construction"
    assert "serialized_size_exceeded" not in str(rows)


def test_partial_interpretation_failure_retains_evidence_and_next_action() -> None:
    presentation = build_partial_analysis_presentation(
        {
            "status": "partial",
            "current_stage": "completed",
            "variants": [
                {"chrom": "3", "pos": 300, "ref": "G", "alt": "A"}
            ],
            "evidence_construction_outcomes": [
                {"variant_index": 0, "status": "success"}
            ],
            "variant_interpretation_results": [
                {
                    "variant_index": 0,
                    "status": "failed",
                    "error_type": "request_timeout",
                }
            ],
        }
    )

    assert presentation["successful_variant_count"] == 0
    assert presentation["failed_variant_count"] == 1
    assert presentation["rows"][0]["Failure category"] == (
        "Automated interpretation"
    )
    assert "collected evidence was retained" in presentation["rows"][0][
        "Explanation"
    ]
    assert "Retry automated interpretation later" in presentation["rows"][0][
        "Next action"
    ]
    assert "request_timeout" not in str(presentation)


def test_provider_no_match_unavailable_and_fallback_remain_distinct() -> None:
    direct = build_evidence_source_presentations(
        {
            "source_statuses": {
                "vep": "success",
                "myvariant": "success",
                "clinvar": "not_found",
                "clingen": "unavailable",
            },
            "capability_results": {},
        }
    )
    by_source = {item["Source"]: item for item in direct}

    assert by_source["ClinVar"]["State"] == (
        "Primary lookup: no exact match"
    )
    assert "No exact ClinVar allele record" in by_source["ClinVar"][
        "Explanation"
    ]
    assert by_source["ClinGen/GenCC"]["State"] == "Provider unavailable"

    rescued = build_evidence_source_presentations(
        {
            "source_statuses": {
                "vep": "success",
                "myvariant": "success",
                "clinvar": "unavailable",
                "clingen": "success",
            },
            "capability_results": {
                "clinvar_evidence": {
                    "capability": "clinvar_evidence",
                    "status": "success",
                    "provider": "myvariant",
                    "provider_role": "fallback",
                    "fallback_used": True,
                    "primary_failure": "timeout",
                }
            },
        }
    )
    rescued_clinvar = next(
        item for item in rescued if item["Source"] == "ClinVar"
    )
    assert rescued_clinvar["State"] == "Evidence rescue completed"
    assert "MyVariant.info" in rescued_clinvar["Explanation"]
    assert "primary ClinVar lookup was unavailable" in rescued_clinvar[
        "Recovery"
    ]


def test_partial_status_and_ui_boundary_use_explicit_safe_wording() -> None:
    label, state, expanded = _result_status(
        {"status": "partial", "current_stage": "completed"}  # type: ignore[arg-type]
    )
    upload_message = safe_ui_error_message(
        RuntimeError("C:/private/workbook.xlsx parse failure"),
        context="excel_upload",  # type: ignore[arg-type]
    )

    assert (label, state, expanded) == (
        "Analysis completed partially",
        "complete",
        True,
    )
    assert upload_message == (
        "The Excel workbook could not be read safely. Check the file and "
        "try again."
    )
    assert "private" not in upload_message.casefold()
