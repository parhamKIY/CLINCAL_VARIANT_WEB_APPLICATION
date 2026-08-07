"""Contracts for the complete clinical variant analysis pipeline."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import uuid4

import requests

from backend.annotation import (
    AnnotationError,
    AnnotationProgressStatus,
    annotate_variants,
)
from backend.conditional_enrichment import enrich_conditionally
from backend.database import DatabaseError, save_complete_analysis
from backend.error_handling import (
    PipelineError,
    PipelineInputError,
    PipelineResultError,
    map_pipeline_exception,
)
from backend.evidence_confirmation import (
    EvidenceConfirmationError,
    confirm_evidence_review,
    validate_reviewed_evidence_package,
)
from backend.evidence_review import (
    EvidenceReviewError,
    build_evidence_review_reports,
    validate_evidence_review_report,
)
from backend.final_interpretation_report import (
    FinalInterpretationReportError,
    build_final_interpretation_report,
    validate_final_interpretation_report,
)
from backend.llm import LLMClient
from backend.llm_routing import (
    Stage35RoutingError,
    route_reviewed_evidence_packages,
    validate_llm_routing_result,
)
from backend.logging_config import (
    bind_analysis_run_id,
    get_logger,
    reset_analysis_run_id,
)
from backend.mydisease import MyDiseaseError, enrich_with_mydisease
from backend.phenotype import (
    HPODataError,
    Phen2GeneError,
    PhenotypeError,
    enrich_with_phen2gene,
    match_phenotypes,
)
from backend.privacy import (
    ClinicalDataPrivacyError,
    minimize_variant,
    validate_no_prohibited_fields,
)
from backend.report import (
    EvidenceObjectError,
    build_evidence_objects,
)
from backend.vcf_processing import (
    VCFProcessingError,
    parse_manual_variants,
    process_vcf,
)
PIPELINE_SCHEMA_VERSION = "2.3"
MAX_PIPELINE_PHENOTYPES = 50
MAX_PIPELINE_WARNINGS = 100
MAX_PIPELINE_ERRORS = 100
ANALYSIS_ID_PATTERN = re.compile(r"analysis-[0-9a-f]{32}")
LOGGER = get_logger("pipeline")

PipelineStatus = Literal[
    "pending",
    "running",
    "partial",
    "success",
    "error",
]
PipelineStageStatus = Literal[
    "pending",
    "running",
    "success",
    "warning",
    "error",
    "skipped",
]
PipelineAPIStatus = Literal[
    "pending",
    "running",
    "success",
    "no_association",
    "warning",
    "error",
    "skipped",
]
PipelineInputMode = Literal["vcf", "manual"]
PipelineWorkflowState = Literal[
    "phase_a_running",
    "awaiting_confirmation",
    "phase_b_running",
    "completed",
    "failed",
]

PIPELINE_STATUS_VALUES = {
    "pending",
    "running",
    "partial",
    "success",
    "error",
}
PIPELINE_STAGE_STATUS_VALUES = {
    "pending",
    "running",
    "success",
    "warning",
    "error",
    "skipped",
}
PIPELINE_API_STATUS_VALUES = PIPELINE_STAGE_STATUS_VALUES | {
    "no_association"
}
PIPELINE_WORKFLOW_STATE_VALUES = {
    "phase_a_running",
    "awaiting_confirmation",
    "phase_b_running",
    "completed",
    "failed",
}
PIPELINE_STAGE_ORDER = (
    "input",
    "vcf_processing",
    "annotation",
    "phenotype",
    "evidence",
    "llm",
    "report",
)
PIPELINE_API_ORDER = (
    "vep",
    "genebe",
    "myvariant",
    "clinvar",
    "clingen",
    "cspec",
    "phen2gene",
    "mydisease",
    "llm",
)


class AnalysisInput(TypedDict):
    """Validated mutually exclusive filtered VCF-table input."""

    input_mode: PipelineInputMode
    vcf_path: str | None
    manual_variants: list[dict[str, object]] | None
    phenotypes: list[str]


class PipelineStageRecord(TypedDict):
    """Progress state for one stable pipeline stage."""

    stage: str
    status: PipelineStageStatus
    progress_percent: int
    message: str | None


class PipelineAPIRecord(TypedDict):
    """Live status for one external API used by the analysis."""

    source: str
    status: PipelineAPIStatus
    message: str | None


class PipelineIssue(TypedDict):
    """Frontend-safe error without an exception or stack trace."""

    stage: str
    code: str
    message: str
    recoverable: bool


class PipelineResult(TypedDict):
    """Versioned JSON-safe output retained for the frontend."""

    schema_version: str
    workflow_state: PipelineWorkflowState
    status: PipelineStatus
    current_stage: str
    progress_percent: int
    stages: list[PipelineStageRecord]
    api_statuses: list[PipelineAPIRecord]
    variant_count: int
    variants: list[dict[str, object]]
    annotations: list[dict[str, object]]
    phenotype_results: list[dict[str, object]]
    evidence_objects: list[dict[str, object]]
    evidence_review_reports: list[dict[str, object]]
    reviewed_evidence_packages: list[dict[str, object]]
    llm_routing_results: list[dict[str, object]]
    final_interpretation_report: dict[str, object] | None
    report_path: str | None
    analysis_id: str | None
    warnings: list[str]
    errors: list[PipelineIssue]


PipelineProgressCallback = Callable[[PipelineResult], None]


ANALYSIS_INPUT_FIELDS = frozenset(AnalysisInput.__required_keys__)
PIPELINE_STAGE_FIELDS = frozenset(
    PipelineStageRecord.__required_keys__
)
PIPELINE_API_FIELDS = frozenset(PipelineAPIRecord.__required_keys__)
PIPELINE_ISSUE_FIELDS = frozenset(PipelineIssue.__required_keys__)
PIPELINE_RESULT_FIELDS = frozenset(PipelineResult.__required_keys__)


def _exact_fields(
    value: dict[object, object],
    expected: frozenset[str],
    path: str,
    error_type: type[PipelineError],
) -> None:
    """Reject omitted and unsupported contract fields."""

    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise error_type(
            f"{path} is missing required fields: "
            f"{', '.join(sorted(missing))}."
        )
    if extra:
        raise error_type(
            f"{path} contains unsupported fields: "
            f"{', '.join(sorted(str(field) for field in extra))}."
        )


def _required_text(
    value: object,
    path: str,
    error_type: type[PipelineError],
) -> str:
    """Return a non-empty visible contract string."""

    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{path} must be a non-empty string.")
    if any(
        ord(character) < 32
        for character in value
        if character not in {"\t"}
    ):
        raise error_type(
            f"{path} cannot contain control characters."
        )
    return value.strip()


def validate_analysis_input(
    *,
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variants: Sequence[Mapping[str, object]] | None = None,
) -> AnalysisInput:
    """Validate one analysis request without accessing the filesystem."""

    normalized_vcf_path: str | None = None
    if vcf_path is not None:
        if not isinstance(vcf_path, (str, Path)):
            raise PipelineInputError(
                "vcf_path must be a string, Path, or None."
            )
        normalized_vcf_path = _required_text(
            str(vcf_path),
            "vcf_path",
            PipelineInputError,
        )

    normalized_manual_variants: list[dict[str, object]] | None = None
    if manual_variants is not None:
        try:
            normalized_manual_variants = [
                dict(variant)
                for variant in parse_manual_variants(manual_variants)
            ]
        except VCFProcessingError as exc:
            raise PipelineInputError(str(exc)) from exc

    source_count = sum(
        value is not None
        for value in (
            normalized_vcf_path,
            normalized_manual_variants,
        )
    )
    if source_count != 1:
        raise PipelineInputError(
            "Exactly one of vcf_path or manual_variants must be provided."
        )

    if not isinstance(phenotypes, (list, tuple)):
        raise PipelineInputError(
            "phenotypes must be a list or tuple of strings."
        )
    if len(phenotypes) > MAX_PIPELINE_PHENOTYPES:
        raise PipelineInputError(
            "phenotypes exceeds the maximum of "
            f"{MAX_PIPELINE_PHENOTYPES}."
        )
    normalized_phenotypes = [
        _required_text(
            phenotype,
            f"phenotypes[{index}]",
            PipelineInputError,
        )
        for index, phenotype in enumerate(phenotypes)
    ]
    if len(set(normalized_phenotypes)) != len(
        normalized_phenotypes
    ):
        raise PipelineInputError(
            "phenotypes must not contain duplicate values."
        )

    return {
        "input_mode": (
            "vcf"
            if normalized_vcf_path is not None
            else "manual"
        ),
        "vcf_path": normalized_vcf_path,
        "manual_variants": normalized_manual_variants,
        "phenotypes": normalized_phenotypes,
    }


def create_pipeline_result() -> PipelineResult:
    """Create an empty deterministic result before execution begins."""

    result: PipelineResult = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "workflow_state": "phase_a_running",
        "status": "pending",
        "current_stage": "input",
        "progress_percent": 0,
        "stages": [
            {
                "stage": stage,
                "status": "pending",
                "progress_percent": 0,
                "message": None,
            }
            for stage in PIPELINE_STAGE_ORDER
        ],
        "api_statuses": [
            {
                "source": source,
                "status": "pending",
                "message": "Waiting for the required pipeline stage.",
            }
            for source in PIPELINE_API_ORDER
        ],
        "variant_count": 0,
        "variants": [],
        "annotations": [],
        "phenotype_results": [],
        "evidence_objects": [],
        "evidence_review_reports": [],
        "reviewed_evidence_packages": [],
        "llm_routing_results": [],
        "final_interpretation_report": None,
        "report_path": None,
        "analysis_id": None,
        "warnings": [],
        "errors": [],
    }
    return validate_pipeline_result(result)


def _validate_stage_records(value: object) -> None:
    """Validate stable stage order and progress payloads."""

    if not isinstance(value, list):
        raise PipelineResultError(
            "pipeline.stages must be a list."
        )
    if len(value) != len(PIPELINE_STAGE_ORDER):
        raise PipelineResultError(
            "pipeline.stages must contain every pipeline stage."
        )

    actual_order: list[str] = []
    for index, record in enumerate(value):
        path = f"pipeline.stages[{index}]"
        if not isinstance(record, dict):
            raise PipelineResultError(
                f"{path} must be a dictionary."
            )
        _exact_fields(
            record,
            PIPELINE_STAGE_FIELDS,
            path,
            PipelineResultError,
        )
        stage = _required_text(
            record["stage"],
            f"{path}.stage",
            PipelineResultError,
        )
        actual_order.append(stage)
        if record["status"] not in PIPELINE_STAGE_STATUS_VALUES:
            raise PipelineResultError(
                f"{path}.status is unsupported."
            )
        progress = record["progress_percent"]
        if (
            isinstance(progress, bool)
            or not isinstance(progress, int)
            or not 0 <= progress <= 100
        ):
            raise PipelineResultError(
                f"{path}.progress_percent must be an integer from "
                "0 to 100."
            )
        if record["message"] is not None:
            _required_text(
                record["message"],
                f"{path}.message",
                PipelineResultError,
            )

    if tuple(actual_order) != PIPELINE_STAGE_ORDER:
        raise PipelineResultError(
            "pipeline.stages must use the required stage order."
        )


def _validate_api_records(value: object) -> None:
    """Validate the fixed external API status contract."""

    if not isinstance(value, list):
        raise PipelineResultError(
            "pipeline.api_statuses must be a list."
        )
    if len(value) != len(PIPELINE_API_ORDER):
        raise PipelineResultError(
            "pipeline.api_statuses must contain every external API."
        )

    actual_order: list[str] = []
    for index, record in enumerate(value):
        path = f"pipeline.api_statuses[{index}]"
        if not isinstance(record, dict):
            raise PipelineResultError(
                f"{path} must be a dictionary."
            )
        _exact_fields(
            record,
            PIPELINE_API_FIELDS,
            path,
            PipelineResultError,
        )
        source = _required_text(
            record["source"],
            f"{path}.source",
            PipelineResultError,
        )
        actual_order.append(source)
        if record["status"] not in PIPELINE_API_STATUS_VALUES:
            raise PipelineResultError(
                f"{path}.status is unsupported."
            )
        if record["message"] is not None:
            _required_text(
                record["message"],
                f"{path}.message",
                PipelineResultError,
            )

    if tuple(actual_order) != PIPELINE_API_ORDER:
        raise PipelineResultError(
            "pipeline.api_statuses must use the required API order."
        )


def _validate_pipeline_issues(value: object) -> None:
    """Validate bounded public errors without exception objects."""

    if not isinstance(value, list):
        raise PipelineResultError(
            "pipeline.errors must be a list."
        )
    if len(value) > MAX_PIPELINE_ERRORS:
        raise PipelineResultError(
            "pipeline.errors exceeds the maximum of "
            f"{MAX_PIPELINE_ERRORS}."
        )
    for index, issue in enumerate(value):
        path = f"pipeline.errors[{index}]"
        if not isinstance(issue, dict):
            raise PipelineResultError(
                f"{path} must be a dictionary."
            )
        _exact_fields(
            issue,
            PIPELINE_ISSUE_FIELDS,
            path,
            PipelineResultError,
        )
        stage = _required_text(
            issue["stage"],
            f"{path}.stage",
            PipelineResultError,
        )
        if stage not in PIPELINE_STAGE_ORDER:
            raise PipelineResultError(
                f"{path}.stage is unsupported."
            )
        _required_text(
            issue["code"],
            f"{path}.code",
            PipelineResultError,
        )
        _required_text(
            issue["message"],
            f"{path}.message",
            PipelineResultError,
        )
        if not isinstance(issue["recoverable"], bool):
            raise PipelineResultError(
                f"{path}.recoverable must be a boolean."
            )


def validate_pipeline_result(value: object) -> PipelineResult:
    """Validate the versioned frontend-safe pipeline result."""

    if not isinstance(value, dict):
        raise PipelineResultError(
            "Pipeline result must be a dictionary."
        )
    _exact_fields(
        value,
        PIPELINE_RESULT_FIELDS,
        "pipeline",
        PipelineResultError,
    )
    if value["schema_version"] != PIPELINE_SCHEMA_VERSION:
        raise PipelineResultError(
            "pipeline.schema_version must be "
            f"{PIPELINE_SCHEMA_VERSION}."
        )
    if value["workflow_state"] not in PIPELINE_WORKFLOW_STATE_VALUES:
        raise PipelineResultError(
            "pipeline.workflow_state is unsupported."
        )
    if value["status"] not in PIPELINE_STATUS_VALUES:
        raise PipelineResultError(
            "pipeline.status is unsupported."
        )
    if value["current_stage"] not in {
        *PIPELINE_STAGE_ORDER,
        "completed",
    }:
        raise PipelineResultError(
            "pipeline.current_stage is unsupported."
        )
    progress = value["progress_percent"]
    if (
        isinstance(progress, bool)
        or not isinstance(progress, int)
        or not 0 <= progress <= 100
    ):
        raise PipelineResultError(
            "pipeline.progress_percent must be an integer from 0 to 100."
        )

    _validate_stage_records(value["stages"])
    _validate_api_records(value["api_statuses"])
    variant_count = value["variant_count"]
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or variant_count < 0
    ):
        raise PipelineResultError(
            "pipeline.variant_count must be a non-negative integer."
        )
    for field in (
        "variants",
        "annotations",
        "phenotype_results",
        "evidence_objects",
        "evidence_review_reports",
        "llm_routing_results",
    ):
        collection = value[field]
        if (
            not isinstance(collection, list)
            or any(not isinstance(item, dict) for item in collection)
        ):
            raise PipelineResultError(
                f"pipeline.{field} must be a list of dictionaries."
            )
    try:
        validate_no_prohibited_fields(
            {
                field: value[field]
                for field in (
                    "variants",
                    "annotations",
                    "phenotype_results",
                    "evidence_objects",
                    "llm_routing_results",
                    "final_interpretation_report",
                )
            },
            context="Pipeline result",
        )
    except ClinicalDataPrivacyError as exc:
        raise PipelineResultError(
            "Pipeline result contains prohibited clinical data."
        ) from exc
    if variant_count != len(value["variants"]):
        raise PipelineResultError(
            "pipeline.variant_count must match the complete variant list."
        )
    review_reports = value["evidence_review_reports"]
    if review_reports:
        if len(review_reports) != len(value["evidence_objects"]):
            raise PipelineResultError(
                "pipeline.evidence_review_reports must match the "
                "Evidence Object count."
            )
        try:
            validated_reports = [
                validate_evidence_review_report(report)
                for report in review_reports
            ]
        except EvidenceReviewError as exc:
            raise PipelineResultError(
                "pipeline.evidence_review_reports is invalid."
            ) from exc
        for index, report in enumerate(validated_reports):
            if (
                report["variant_index"] != index
                or report["original_machine_report"]
                != value["evidence_objects"][index]
            ):
                raise PipelineResultError(
                    "pipeline.evidence_review_reports must preserve "
                    "Evidence Object order and originals."
                )
    packages = value["reviewed_evidence_packages"]
    if (
        not isinstance(packages, list)
        or any(not isinstance(item, dict) for item in packages)
    ):
        raise PipelineResultError(
            "pipeline.reviewed_evidence_packages must be a list of "
            "dictionaries."
        )
    validated_packages: list[dict[str, object]] = []
    if packages:
        try:
            validated_packages = [
                validate_reviewed_evidence_package(package)
                for package in packages
            ]
        except EvidenceConfirmationError as exc:
            raise PipelineResultError(
                "pipeline.reviewed_evidence_packages is invalid."
            ) from exc
        previous_index = -1
        for package in validated_packages:
            index = package["variant_index"]
            if index <= previous_index:
                raise PipelineResultError(
                    "pipeline.reviewed_evidence_packages must preserve "
                    "ascending variant order without duplicates."
                )
            if (
                index >= len(value["evidence_objects"])
                or package["original_machine_report"]
                != value["evidence_objects"][index]
            ):
                raise PipelineResultError(
                    "pipeline.reviewed_evidence_packages must reference "
                    "an unmodified Evidence Object."
                )
            previous_index = index
    routing_results = value["llm_routing_results"]
    if (
        not isinstance(routing_results, list)
        or any(not isinstance(item, dict) for item in routing_results)
    ):
        raise PipelineResultError(
            "pipeline.llm_routing_results must be a list of dictionaries."
        )
    if routing_results:
        packages_by_id = {
            package["package_id"]: package
            for package in validated_packages
        }
        previous_index = -1
        for item in routing_results:
            package = packages_by_id.get(item.get("package_id"))
            if package is None:
                raise PipelineResultError(
                    "pipeline.llm_routing_results must reference a "
                    "confirmed evidence package."
                )
            try:
                validated_result = validate_llm_routing_result(
                    item,
                    package=package,
                )
            except Stage35RoutingError as exc:
                raise PipelineResultError(
                    "pipeline.llm_routing_results is invalid."
                ) from exc
            index = validated_result["variant_index"]
            if index <= previous_index:
                raise PipelineResultError(
                    "pipeline.llm_routing_results must preserve ascending "
                    "variant order without duplicates."
                )
            previous_index = index
    final_report = value["final_interpretation_report"]
    if final_report is not None:
        try:
            validated_report = validate_final_interpretation_report(
                final_report
            )
            expected_report = build_final_interpretation_report(
                variant_count,
                routing_results,
            )
        except FinalInterpretationReportError as exc:
            raise PipelineResultError(
                "pipeline.final_interpretation_report is invalid."
            ) from exc
        if validated_report != expected_report:
            raise PipelineResultError(
                "pipeline.final_interpretation_report does not match "
                "the Stage 35 results."
            )
    workflow_state = value["workflow_state"]
    if workflow_state == "awaiting_confirmation" and not review_reports:
        raise PipelineResultError(
            "pipeline.awaiting_confirmation requires editable reports."
        )
    if workflow_state == "completed" and final_report is None:
        raise PipelineResultError(
            "pipeline.completed requires Output B."
        )
    if final_report is not None and workflow_state != "completed":
        raise PipelineResultError(
            "pipeline Output B requires the completed workflow state."
        )
    if value["report_path"] is not None:
        _required_text(
            value["report_path"],
            "pipeline.report_path",
            PipelineResultError,
        )
    if value["analysis_id"] is not None and (
        not isinstance(value["analysis_id"], str)
        or ANALYSIS_ID_PATTERN.fullmatch(value["analysis_id"]) is None
    ):
        raise PipelineResultError(
            "pipeline.analysis_id must use the application-generated "
            "format or be null."
        )

    warnings = value["warnings"]
    if not isinstance(warnings, list):
        raise PipelineResultError(
            "pipeline.warnings must be a list."
        )
    normalized_warnings = [
        _required_text(
            warning,
            f"pipeline.warnings[{index}]",
            PipelineResultError,
        )
        for index, warning in enumerate(warnings)
    ]
    if len(normalized_warnings) > MAX_PIPELINE_WARNINGS:
        raise PipelineResultError(
            "pipeline.warnings exceeds the maximum of "
            f"{MAX_PIPELINE_WARNINGS}."
        )
    if len(set(normalized_warnings)) != len(normalized_warnings):
        raise PipelineResultError(
            "pipeline.warnings must not contain duplicates."
        )

    _validate_pipeline_issues(value["errors"])
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PipelineResultError(
            "Pipeline result must be JSON serializable."
        ) from exc
    return cast(PipelineResult, value)


def _notify_progress(
    result: PipelineResult,
    callback: PipelineProgressCallback | None,
) -> None:
    """Send an isolated valid snapshot without affecting execution."""

    if callback is None:
        return
    snapshot = validate_pipeline_result(deepcopy(result))
    try:
        callback(snapshot)
    except Exception:
        return


def _set_stage(
    result: PipelineResult,
    stage: str,
    status: PipelineStageStatus,
    *,
    progress_percent: int,
    message: str | None = None,
) -> None:
    """Update one known stage without changing the result schema."""

    stage_index = PIPELINE_STAGE_ORDER.index(stage)
    result["stages"][stage_index] = {
        "stage": stage,
        "status": status,
        "progress_percent": progress_percent,
        "message": message,
    }
    if status == "running":
        LOGGER.info(
            "event=pipeline_stage_started stage=%s",
            stage,
        )
    elif status in {
        "success",
        "warning",
        "error",
        "skipped",
    }:
        LOGGER.info(
            "event=pipeline_stage_finished stage=%s status=%s",
            stage,
            status,
        )


def _set_api_status(
    result: PipelineResult,
    source: str,
    status: PipelineAPIStatus,
    message: str,
) -> None:
    """Update one known external API status."""

    api_index = PIPELINE_API_ORDER.index(source)
    result["api_statuses"][api_index] = {
        "source": source,
        "status": status,
        "message": " ".join(message.split())[:500],
    }


def _append_warning(
    result: PipelineResult,
    message: str,
) -> None:
    """Append one unique warning without exceeding the public bound."""

    normalized = " ".join(message.split())
    if (
        not normalized
        or normalized in result["warnings"]
        or len(result["warnings"]) >= MAX_PIPELINE_WARNINGS
    ):
        return
    result["warnings"].append(normalized)


def _retain_warnings(
    result: PipelineResult,
    warnings: object,
) -> None:
    """Aggregate bounded unique warning text from one pipeline output."""

    if not isinstance(warnings, list):
        return
    for warning in warnings:
        if isinstance(warning, str):
            _append_warning(result, warning)


def _record_issue(
    result: PipelineResult,
    *,
    stage: str,
    code: str,
    message: str,
    recoverable: bool,
) -> None:
    """Retain one sanitized structured issue for frontend display."""

    normalized = " ".join(message.split())
    if not normalized:
        normalized = "The pipeline stage could not be completed."
    issue: PipelineIssue = {
        "stage": stage,
        "code": code,
        "message": normalized[:500],
        "recoverable": recoverable,
    }
    if (
        issue not in result["errors"]
        and len(result["errors"]) < MAX_PIPELINE_ERRORS
    ):
        result["errors"].append(issue)
    if recoverable:
        _append_warning(result, issue["message"])


def _finish_failed_stage(
    result: PipelineResult,
    *,
    stage: str,
    code: str,
    message: str,
    recoverable: bool,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Finalize a stopped pipeline while preserving completed outputs."""

    normalized_message = " ".join(message.split())[:500]
    if not normalized_message:
        normalized_message = (
            "The pipeline stage could not be completed."
        )
    _record_issue(
        result,
        stage=stage,
        code=code,
        message=normalized_message,
        recoverable=recoverable,
    )
    _set_stage(
        result,
        stage,
        "error",
        progress_percent=100,
        message=normalized_message,
    )
    failed_index = PIPELINE_STAGE_ORDER.index(stage)
    for later_stage in PIPELINE_STAGE_ORDER[failed_index + 1:]:
        record = result["stages"][
            PIPELINE_STAGE_ORDER.index(later_stage)
        ]
        if record["status"] in {"pending", "running"}:
            _set_stage(
                result,
                later_stage,
                "skipped",
                progress_percent=0,
                message=f"Skipped because {stage} did not complete.",
            )

    for api_record in result["api_statuses"]:
        if api_record["status"] == "running":
            _set_api_status(
                result,
                api_record["source"],
                "error",
                f"Stopped because {stage} did not complete.",
            )
        elif api_record["status"] == "pending":
            _set_api_status(
                result,
                api_record["source"],
                "skipped",
                f"Not called because {stage} did not complete.",
            )

    has_retained_output = any(
        result[field]
        for field in (
            "variants",
            "annotations",
            "phenotype_results",
            "evidence_objects",
            "evidence_review_reports",
            "reviewed_evidence_packages",
            "llm_routing_results",
            "final_interpretation_report",
        )
    )
    result["status"] = (
        "partial"
        if recoverable and has_retained_output
        else "error"
    )
    result["workflow_state"] = "failed"
    result["current_stage"] = stage
    validated = validate_pipeline_result(result)
    _notify_progress(validated, progress_callback)
    return validated


def _finish_exception(
    result: PipelineResult,
    *,
    stage: str,
    error: Exception,
    default_code: str,
    default_message: str,
    default_recoverable: bool,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Finalize a stage using only centrally mapped public error fields."""

    public_error = map_pipeline_exception(
        error,
        stage=stage,
        default_code=default_code,
        default_message=default_message,
        default_recoverable=default_recoverable,
    )
    return _finish_failed_stage(
        result,
        stage=stage,
        code=public_error["code"],
        message=public_error["message"],
        recoverable=public_error["recoverable"],
        progress_callback=progress_callback,
    )


def _persist_terminal_result(
    request: AnalysisInput,
    result: PipelineResult,
    *,
    database_path: str | Path | None,
    report_dir: str | Path | None,
) -> None:
    """Persist terminal output without hiding usable clinical results."""

    source_filename: str | None = None
    if request["input_mode"] == "vcf" and request["vcf_path"] is not None:
        source_filename = Path(request["vcf_path"]).name
    try:
        record = save_complete_analysis(
            status=result["status"],
            source_filename=source_filename,
            warnings=result["warnings"],
            candidates=result["variants"],
            evidence_objects=result["evidence_objects"],
            report_path=result["report_path"],
            database_path=database_path,
            report_dir=report_dir,
        )
    except DatabaseError:
        _append_warning(
            result,
            "The analysis completed but could not be saved to the "
            "local analysis database.",
        )
        if result["status"] == "success":
            result["status"] = "partial"
        result["analysis_id"] = None
        return
    result["analysis_id"] = record["analysis_id"]


def _process_filtered_variants(
    request: AnalysisInput,
    result: PipelineResult,
    *,
    progress_callback: PipelineProgressCallback | None = None,
) -> None:
    """Load every variant from the pre-filtered input table."""

    result["status"] = "running"
    result["current_stage"] = "vcf_processing"
    result["progress_percent"] = 15
    _set_stage(
        result,
        "input",
        "success",
        progress_percent=100,
        message=f"Validated {request['input_mode']} input.",
    )
    _set_stage(
        result,
        "vcf_processing",
        "running",
        progress_percent=0,
        message="Reading the filtered variant table.",
    )
    _notify_progress(result, progress_callback)

    if request["input_mode"] == "vcf":
        raw_variants = list(
            process_vcf(vcf_path=request["vcf_path"])
        )
    else:
        raw_variants = [
            dict(variant)
            for variant in request["manual_variants"] or ()
        ]
    if not raw_variants:
        raise PipelineError(
            "Variant processing produced no variants."
        )

    result["variants"] = [
        minimize_variant(variant)
        for variant in raw_variants
    ]
    result["variant_count"] = len(result["variants"])
    LOGGER.info(
        "event=filtered_variants_loaded variant_count=%d",
        result["variant_count"],
    )

    _set_stage(
        result,
        "vcf_processing",
        "success",
        progress_percent=100,
        message=(
            f"Loaded all {result['variant_count']} filtered variants."
        ),
    )
    result["current_stage"] = "annotation"
    result["progress_percent"] = 25
    _notify_progress(result, progress_callback)


def _retain_annotation_warnings(
    result: PipelineResult,
    annotations: list[dict[str, object]],
) -> None:
    """Copy unique annotation warnings into the bounded pipeline result."""

    for annotation in annotations:
        _retain_warnings(result, annotation.get("warnings", []))


def _annotate_and_match(
    request: AnalysisInput,
    result: PipelineResult,
    *,
    annotation_batch_size: int | None,
    annotation_max_retries: int | None,
    annotation_session: requests.Session | None,
    ontology_path: str | Path | None,
    associations_path: str | Path | None,
    phen2gene_max_retries: int | None,
    phen2gene_session: requests.Session | None,
    phen2gene_use_cache: bool,
    mydisease_session: requests.Session | None,
    progress_callback: PipelineProgressCallback | None = None,
) -> None:
    """Enrich filtered variants and attach optional HPO scores."""

    result["current_stage"] = "annotation"
    result["progress_percent"] = 35
    _set_stage(
        result,
        "annotation",
        "running",
        progress_percent=0,
        message="Annotating all filtered variants.",
    )
    _notify_progress(result, progress_callback)

    def update_annotation_progress(
        source: str,
        status: AnnotationProgressStatus,
        message: str,
    ) -> None:
        _set_api_status(result, source, status, message)
        _notify_progress(result, progress_callback)

    annotations = annotate_variants(
        result["variants"],
        batch_size=annotation_batch_size,
        max_retries=annotation_max_retries,
        session=annotation_session,
        progress_callback=update_annotation_progress,
    )
    public_annotations = [
        dict(annotation)
        for annotation in annotations
    ]
    try:
        validate_no_prohibited_fields(
            public_annotations,
            context="Annotation output",
        )
    except ClinicalDataPrivacyError as exc:
        raise AnnotationError(
            "Annotation output violated the clinical-data privacy "
            "contract."
        ) from exc
    result["annotations"] = public_annotations
    _retain_annotation_warnings(result, result["annotations"])
    annotation_status: PipelineStageStatus = (
        "warning"
        if any(
            annotation.get("warnings")
            for annotation in result["annotations"]
        )
        else "success"
    )
    _set_stage(
        result,
        "annotation",
        annotation_status,
        progress_percent=100,
        message=(
            f"Annotated {len(result['annotations'])} variants"
            + (
                " with source warnings."
                if annotation_status == "warning"
                else "."
            )
        ),
    )

    result["current_stage"] = "phenotype"
    result["progress_percent"] = 50
    if not request["phenotypes"]:
        result["phenotype_results"] = [
            dict(annotation)
            for annotation in result["annotations"]
        ]
        _set_api_status(
            result,
            "phen2gene",
            "skipped",
            "Not called because no HPO phenotypes were supplied.",
        )
        _set_stage(
            result,
            "phenotype",
            "skipped",
            progress_percent=100,
            message="No HPO phenotypes were supplied.",
        )
    else:
        _set_stage(
            result,
            "phenotype",
            "running",
            progress_percent=0,
            message="Matching annotated genes to HPO phenotypes.",
        )
        _notify_progress(result, progress_callback)
        try:
            phenotype_results = match_phenotypes(
                result["annotations"],
                request["phenotypes"],
                ontology_path=ontology_path,
                associations_path=associations_path,
            )
        except (PhenotypeError, HPODataError):
            message = (
                "Phenotype matching was unavailable; annotated "
                "variants continued without phenotype scores."
            )
            result["phenotype_results"] = [
                dict(annotation)
                for annotation in result["annotations"]
            ]
            _record_issue(
                result,
                stage="phenotype",
                code="phenotype_matching_unavailable",
                message=message,
                recoverable=True,
            )
            _set_api_status(
                result,
                "phen2gene",
                "skipped",
                "Not called because local HPO validation was unavailable.",
            )
            _set_stage(
                result,
                "phenotype",
                "warning",
                progress_percent=100,
                message=message,
            )
        else:
            _set_api_status(
                result,
                "phen2gene",
                "running",
                "Submitting the analysis HPO set once to Phen2Gene.",
            )
            _notify_progress(result, progress_callback)
            try:
                phen2gene_result = enrich_with_phen2gene(
                    phenotype_results,
                    request["phenotypes"],
                    ontology_path=ontology_path,
                    max_retries=phen2gene_max_retries,
                    session=phen2gene_session,
                    use_cache=phen2gene_use_cache,
                )
            except (
                Phen2GeneError,
                PhenotypeError,
                HPODataError,
            ):
                message = (
                    "Phen2Gene evidence could not be produced; local "
                    "HPO matching results were retained."
                )
                _set_api_status(
                    result,
                    "phen2gene",
                    "error",
                    message,
                )
                _record_issue(
                    result,
                    stage="phenotype",
                    code="phen2gene_unavailable",
                    message=message,
                    recoverable=True,
                )
                phenotype_status: PipelineStageStatus = "warning"
                phenotype_message = (
                    "Attached local phenotype scores to "
                    f"{len(phenotype_results)} variants; Phen2Gene "
                    "was unavailable."
                )
            else:
                phenotype_results = phen2gene_result["variants"]
                phen2gene_availability = phen2gene_result[
                    "availability"
                ]
                if phen2gene_availability == "available":
                    api_status: PipelineAPIStatus = "success"
                    phenotype_status = "success"
                elif (
                    phen2gene_availability == "unavailable"
                    and phen2gene_result["request_attempts"] == 0
                ):
                    api_status = "skipped"
                    phenotype_status = "warning"
                elif phen2gene_availability == "unavailable":
                    api_status = "error"
                    phenotype_status = "warning"
                    _record_issue(
                        result,
                        stage="phenotype",
                        code="phen2gene_unavailable",
                        message=(
                            "Phen2Gene was unavailable; local HPO "
                            "matching and all annotation evidence were "
                            "retained."
                        ),
                        recoverable=True,
                    )
                else:
                    api_status = "warning"
                    phenotype_status = "warning"
                _set_api_status(
                    result,
                    "phen2gene",
                    api_status,
                    phen2gene_result["message"],
                )
                phenotype_message = (
                    "Attached local and Phen2Gene phenotype evidence "
                    f"to {len(phenotype_results)} variants."
                )

            public_phenotype_results = [
                dict(variant)
                for variant in phenotype_results
            ]
            try:
                validate_no_prohibited_fields(
                    public_phenotype_results,
                    context="Phenotype output",
                )
            except ClinicalDataPrivacyError as exc:
                raise AnnotationError(
                    "Phenotype output violated the clinical-data privacy "
                    "contract."
                ) from exc
            result["phenotype_results"] = public_phenotype_results
            _set_stage(
                result,
                "phenotype",
                phenotype_status,
                progress_percent=100,
                message=phenotype_message,
            )

    _set_api_status(
        result,
        "mydisease",
        "running",
        "Retrieving bounded gene-disease-phenotype context.",
    )
    _notify_progress(result, progress_callback)
    try:
        mydisease_result = enrich_with_mydisease(
            result["phenotype_results"],
            request["phenotypes"],
            session=mydisease_session,
        )
    except MyDiseaseError:
        mydisease_message = (
            "MyDisease.info context could not be produced; annotation, local "
            "HPO, and Phen2Gene evidence were retained."
        )
        _set_api_status(
            result,
            "mydisease",
            "error",
            mydisease_message,
        )
        _record_issue(
            result,
            stage="phenotype",
            code="mydisease_unavailable",
            message=mydisease_message,
            recoverable=True,
        )
        _set_stage(
            result,
            "phenotype",
            "warning",
            progress_percent=100,
            message=mydisease_message,
        )
    else:
        public_mydisease_results = [
            dict(variant)
            for variant in mydisease_result["variants"]
        ]
        try:
            validate_no_prohibited_fields(
                public_mydisease_results,
                context="MyDisease phenotype output",
            )
        except ClinicalDataPrivacyError as exc:
            raise AnnotationError(
                "MyDisease output violated the clinical-data privacy "
                "contract."
            ) from exc
        result["phenotype_results"] = public_mydisease_results
        mydisease_status = mydisease_result["status"]
        if mydisease_status == "available":
            mydisease_api_status: PipelineAPIStatus = "success"
        elif mydisease_status == "no_association":
            mydisease_api_status = "no_association"
        elif mydisease_status == "unsupported":
            mydisease_api_status = "skipped"
        elif mydisease_status == "unavailable":
            mydisease_api_status = "error"
            _record_issue(
                result,
                stage="phenotype",
                code="mydisease_unavailable",
                message=(
                    "MyDisease.info was unavailable; annotation, local "
                    "HPO, and Phen2Gene evidence were retained."
                ),
                recoverable=True,
            )
        elif mydisease_status == "invalid_response":
            mydisease_api_status = "error"
            _record_issue(
                result,
                stage="phenotype",
                code="mydisease_invalid_response",
                message=(
                    "MyDisease.info returned an invalid response; "
                    "annotation, local HPO, and Phen2Gene evidence "
                    "were retained."
                ),
                recoverable=True,
            )
        else:
            mydisease_api_status = "warning"
        _set_api_status(
            result,
            "mydisease",
            mydisease_api_status,
            mydisease_result["message"],
        )

        phenotype_record = next(
            record
            for record in result["stages"]
            if record["stage"] == "phenotype"
        )
        prior_status = phenotype_record["status"]
        if mydisease_api_status in {"error", "warning"}:
            combined_status: PipelineStageStatus = "warning"
        elif prior_status == "skipped":
            combined_status = "success"
        else:
            combined_status = prior_status
        _set_stage(
            result,
            "phenotype",
            combined_status,
            progress_percent=100,
            message=(
                "Phenotype and Phen2Gene evidence were retained. "
                f"{mydisease_result['message']}"
            ),
        )

    result["current_stage"] = "evidence"
    result["progress_percent"] = 60
    _notify_progress(result, progress_callback)


def _build_evidence_and_report(
    result: PipelineResult,
    *,
    llm_client: LLMClient | None,
    llm_model: str | None,
    report_dir: str | Path | None,
    population_session: requests.Session | None = None,
    literature_session: requests.Session | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> None:
    """Build Stage 33 Output A for every filtered variant."""

    _ = (llm_client, llm_model, report_dir)
    result["current_stage"] = "evidence"
    result["progress_percent"] = 65
    _set_stage(
        result,
        "evidence",
        "running",
        progress_percent=0,
        message="Building bounded Evidence Objects.",
    )
    _notify_progress(result, progress_callback)
    preliminary_evidence = build_evidence_objects(
        result["phenotype_results"]
    )
    conditional_result = enrich_conditionally(
        result["phenotype_results"],
        preliminary_evidence,
        population_session=population_session,
        literature_session=literature_session,
    )
    result["phenotype_results"] = conditional_result["variants"]
    for candidate in result["phenotype_results"]:
        conditional = candidate.get("conditional_enrichment")
        if isinstance(conditional, Mapping):
            _retain_warnings(result, conditional.get("warnings"))
    evidence_objects = build_evidence_objects(
        result["phenotype_results"]
    )
    if not evidence_objects:
        raise PipelineError(
            "Evidence Object construction produced no variants."
        )
    result["evidence_objects"] = [
        dict(evidence)
        for evidence in evidence_objects
    ]
    review_reports = build_evidence_review_reports(evidence_objects)
    result["evidence_review_reports"] = [
        dict(report)
        for report in review_reports
    ]
    LOGGER.info(
        "event=evidence_build_finished evidence_object_count=%d",
        len(evidence_objects),
    )
    _set_stage(
        result,
        "evidence",
        "success",
        progress_percent=100,
        message=(
            f"Built {len(evidence_objects)} Evidence Objects; "
            f"conditional enrichment ran for "
            f"{conditional_result['triggered_count']} variants."
        ),
    )

    result["current_stage"] = "llm"
    result["progress_percent"] = 75
    _set_stage(
        result,
        "llm",
        "skipped",
        progress_percent=100,
        message="Final interpretation requires human confirmation.",
    )
    _set_api_status(
        result,
        "llm",
        "skipped",
        "Not called before human review and confirmation.",
    )
    _notify_progress(result, progress_callback)

    result["current_stage"] = "report"
    result["progress_percent"] = 90
    _set_stage(
        result,
        "report",
        "running",
        progress_percent=0,
        message="Preparing editable evidence review reports.",
    )
    _notify_progress(result, progress_callback)
    LOGGER.info(
        "event=evidence_review_reports_prepared report_count=%d",
        len(review_reports),
    )
    _set_stage(
        result,
        "report",
        "success",
        progress_percent=100,
        message=(
            f"Prepared {len(review_reports)} editable evidence review "
            "reports."
        ),
    )
    result["status"] = (
        "partial"
        if result["warnings"] or result["errors"]
        else "success"
    )
    result["workflow_state"] = "awaiting_confirmation"
    result["current_stage"] = "completed"
    result["progress_percent"] = 100
    _notify_progress(result, progress_callback)


def run_variant_processing(
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variants: Sequence[Mapping[str, object]] | None = None,
) -> PipelineResult:
    """Load and validate one filtered VCF table."""

    request = validate_analysis_input(
        vcf_path=vcf_path,
        manual_variants=manual_variants,
        phenotypes=phenotypes,
    )
    result = create_pipeline_result()
    _process_filtered_variants(
        request,
        result,
    )
    return validate_pipeline_result(result)


def run_annotation_and_phenotype(
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variants: Sequence[Mapping[str, object]] | None = None,
    *,
    annotation_batch_size: int | None = None,
    annotation_max_retries: int | None = None,
    annotation_session: requests.Session | None = None,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
    phen2gene_max_retries: int | None = None,
    phen2gene_session: requests.Session | None = None,
    phen2gene_use_cache: bool = True,
    mydisease_session: requests.Session | None = None,
) -> PipelineResult:
    """Run annotation, optional HPO matching, and MyDisease enrichment."""

    request = validate_analysis_input(
        vcf_path=vcf_path,
        manual_variants=manual_variants,
        phenotypes=phenotypes,
    )
    result = create_pipeline_result()
    _process_filtered_variants(
        request,
        result,
    )
    _annotate_and_match(
        request,
        result,
        annotation_batch_size=annotation_batch_size,
        annotation_max_retries=annotation_max_retries,
        annotation_session=annotation_session,
        ontology_path=ontology_path,
        associations_path=associations_path,
        phen2gene_max_retries=phen2gene_max_retries,
        phen2gene_session=phen2gene_session,
        phen2gene_use_cache=phen2gene_use_cache,
        mydisease_session=mydisease_session,
    )
    return validate_pipeline_result(result)


def _run_analysis_unpersisted(
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variants: Sequence[Mapping[str, object]] | None = None,
    *,
    annotation_batch_size: int | None = None,
    annotation_max_retries: int | None = None,
    annotation_session: requests.Session | None = None,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
    phen2gene_max_retries: int | None = None,
    phen2gene_session: requests.Session | None = None,
    phen2gene_use_cache: bool = True,
    mydisease_session: requests.Session | None = None,
    population_session: requests.Session | None = None,
    literature_session: requests.Session | None = None,
    llm_client: LLMClient | None = None,
    llm_model: str | None = None,
    report_dir: str | Path | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Run the clinical pipeline before optional database persistence."""

    result = create_pipeline_result()
    try:
        request = validate_analysis_input(
            vcf_path=vcf_path,
            manual_variants=manual_variants,
            phenotypes=phenotypes,
        )
    except PipelineInputError as exc:
        return _finish_exception(
            result,
            stage="input",
            error=exc,
            default_code="invalid_input",
            default_message="The analysis input is invalid.",
            default_recoverable=False,
            progress_callback=progress_callback,
        )

    result["status"] = "running"
    result["current_stage"] = "input"
    result["progress_percent"] = 5
    _set_stage(
        result,
        "input",
        "running",
        progress_percent=0,
        message="Validating analysis input.",
    )
    _notify_progress(result, progress_callback)

    try:
        _process_filtered_variants(
            request,
            result,
            progress_callback=progress_callback,
        )
    except VCFProcessingError as exc:
        return _finish_exception(
            result,
            stage="vcf_processing",
            error=exc,
            default_code="vcf_processing_failed",
            default_message="Variant processing could not be completed.",
            default_recoverable=False,
            progress_callback=progress_callback,
        )
    except PipelineError as exc:
        return _finish_exception(
            result,
            stage="vcf_processing",
            error=exc,
            default_code="no_variants",
            default_message="Variant processing produced no variants.",
            default_recoverable=False,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        return _finish_exception(
            result,
            stage=result["current_stage"],
            error=exc,
            default_code="unexpected_processing_error",
            default_message=(
                "Variant processing stopped because of an unexpected "
                "internal error."
            ),
            default_recoverable=False,
            progress_callback=progress_callback,
        )

    try:
        _annotate_and_match(
            request,
            result,
            annotation_batch_size=annotation_batch_size,
            annotation_max_retries=annotation_max_retries,
            annotation_session=annotation_session,
            ontology_path=ontology_path,
            associations_path=associations_path,
            phen2gene_max_retries=phen2gene_max_retries,
            phen2gene_session=phen2gene_session,
            phen2gene_use_cache=phen2gene_use_cache,
            mydisease_session=mydisease_session,
            progress_callback=progress_callback,
        )
    except AnnotationError as exc:
        return _finish_exception(
            result,
            stage="annotation",
            error=exc,
            default_code="annotation_failed",
            default_message="Variant annotation could not be completed.",
            default_recoverable=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        return _finish_exception(
            result,
            stage=result["current_stage"],
            error=exc,
            default_code="unexpected_enrichment_error",
            default_message=(
                "Variant enrichment stopped because of an unexpected "
                "internal error."
            ),
            default_recoverable=False,
            progress_callback=progress_callback,
        )

    try:
        _build_evidence_and_report(
            result,
            llm_client=llm_client,
            llm_model=llm_model,
            report_dir=report_dir,
            population_session=population_session,
            literature_session=literature_session,
            progress_callback=progress_callback,
        )
    except (EvidenceObjectError, EvidenceReviewError) as exc:
        return _finish_exception(
            result,
            stage="evidence",
            error=exc,
            default_code="evidence_object_failed",
            default_message="Evidence construction could not be completed.",
            default_recoverable=False,
            progress_callback=progress_callback,
        )
    except PipelineError as exc:
        return _finish_exception(
            result,
            stage="evidence",
            error=exc,
            default_code="evidence_object_failed",
            default_message="Evidence construction produced no variants.",
            default_recoverable=False,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        return _finish_exception(
            result,
            stage=result["current_stage"],
            error=exc,
            default_code="unexpected_pipeline_error",
            default_message=(
                "Analysis stopped because of an unexpected internal "
                "error."
            ),
            default_recoverable=False,
            progress_callback=progress_callback,
        )
    return validate_pipeline_result(result)


def run_analysis(
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variants: Sequence[Mapping[str, object]] | None = None,
    *,
    annotation_batch_size: int | None = None,
    annotation_max_retries: int | None = None,
    annotation_session: requests.Session | None = None,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
    phen2gene_max_retries: int | None = None,
    phen2gene_session: requests.Session | None = None,
    phen2gene_use_cache: bool = True,
    mydisease_session: requests.Session | None = None,
    population_session: requests.Session | None = None,
    literature_session: requests.Session | None = None,
    llm_client: LLMClient | None = None,
    llm_model: str | None = None,
    report_dir: str | Path | None = None,
    database_path: str | Path | None = None,
    persist_analysis: bool = True,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Run Phase A through editable Output A, then pause for confirmation."""

    if vcf_path is not None and manual_variants is None:
        input_mode = "vcf"
    elif manual_variants is not None and vcf_path is None:
        input_mode = "manual"
    else:
        input_mode = "invalid"
    phenotype_count = (
        len(phenotypes)
        if isinstance(phenotypes, (list, tuple))
        else -1
    )
    run_id = f"run-{uuid4().hex}"
    context_token = bind_analysis_run_id(run_id)
    LOGGER.info(
        "event=analysis_started input_mode=%s phenotype_count=%d",
        input_mode,
        phenotype_count,
    )
    try:
        result = _run_analysis_unpersisted(
            vcf_path=vcf_path,
            phenotypes=phenotypes,
            manual_variants=manual_variants,
            annotation_batch_size=annotation_batch_size,
            annotation_max_retries=annotation_max_retries,
            annotation_session=annotation_session,
            ontology_path=ontology_path,
            associations_path=associations_path,
            phen2gene_max_retries=phen2gene_max_retries,
            phen2gene_session=phen2gene_session,
            phen2gene_use_cache=phen2gene_use_cache,
            mydisease_session=mydisease_session,
            population_session=population_session,
            literature_session=literature_session,
            llm_client=llm_client,
            llm_model=llm_model,
            report_dir=report_dir,
            progress_callback=progress_callback,
        )
        if not isinstance(persist_analysis, bool):
            _append_warning(
                result,
                "Analysis persistence was disabled because its setting "
                "was invalid.",
            )
            if result["status"] == "success":
                result["status"] = "partial"
        elif persist_analysis and not (
            result["current_stage"] == "input"
            and any(
                issue["code"] == "invalid_input"
                for issue in result["errors"]
            )
        ):
            request = validate_analysis_input(
                vcf_path=vcf_path,
                manual_variants=manual_variants,
                phenotypes=phenotypes,
            )
            _persist_terminal_result(
                request,
                result,
                database_path=database_path,
                report_dir=report_dir,
            )

        validated = validate_pipeline_result(result)
        LOGGER.info(
            "event=analysis_finished status=%s analysis_id=%s "
            "warning_count=%d error_count=%d",
            validated["status"],
            validated["analysis_id"],
            len(validated["warnings"]),
            len(validated["errors"]),
        )
        return validated
    except Exception as exc:
        LOGGER.error(
            "event=analysis_aborted error_type=%s",
            type(exc).__name__,
        )
        raise
    finally:
        reset_analysis_run_id(context_token)


def confirm_reviewed_evidence(
    result: PipelineResult,
    reports: Sequence[Mapping[str, object]],
    *,
    timestamp: str | None = None,
) -> PipelineResult:
    """Build Stage 34 Reviewed Evidence Packages without calling the LLM.

    Accepts one or more user-confirmed Stage 33 drafts (a full or partial
    subset of ``result["evidence_review_reports"]``), re-runs the
    deterministic post-review conflict audit for each, and stores the
    resulting immutable packages. Confirming a variant again replaces its
    prior package. No stage transition and no LLM call happen here.
    """

    working = validate_pipeline_result(deepcopy(result))
    if not working["evidence_review_reports"]:
        raise PipelineError(
            "No editable evidence review reports are available to "
            "confirm."
        )
    if isinstance(reports, (str, bytes, Mapping)):
        raise PipelineError(
            "reports must be a sequence of evidence review drafts."
        )

    packages_by_index: dict[int, dict[str, object]] = {
        cast(int, package["variant_index"]): dict(package)
        for package in working["reviewed_evidence_packages"]
    }
    updated_indexes: set[int] = set()
    for report in reports:
        try:
            package = confirm_evidence_review(report, timestamp=timestamp)
        except EvidenceConfirmationError as exc:
            raise PipelineError(str(exc)) from exc
        index = package["variant_index"]
        if (
            index >= len(working["evidence_objects"])
            or package["original_machine_report"]
            != working["evidence_objects"][index]
        ):
            raise PipelineError(
                "Confirmed evidence does not match this analysis."
            )
        packages_by_index[index] = dict(package)
        updated_indexes.add(index)
        LOGGER.info(
            "event=reviewed_evidence_package_confirmed variant_index=%d "
            "post_review_status=%s",
            index,
            package["post_review_conflict"]["status"],
        )

    working["reviewed_evidence_packages"] = [
        packages_by_index[index]
        for index in sorted(packages_by_index.keys())
    ]
    working["llm_routing_results"] = [
        item
        for item in working["llm_routing_results"]
        if item["variant_index"] not in updated_indexes
    ]
    if updated_indexes:
        working["final_interpretation_report"] = None
        working["workflow_state"] = "awaiting_confirmation"
    return validate_pipeline_result(working)


def generate_confirmed_interpretations(
    result: PipelineResult,
    *,
    light_client: LLMClient | None = None,
    strong_client: LLMClient | None = None,
    light_model: str | None = None,
    strong_model: str | None = None,
    timestamp: str | None = None,
) -> PipelineResult:
    """Run Stage 35 routing for confirmed packages without building Output B."""

    working = validate_pipeline_result(deepcopy(result))
    packages = working["reviewed_evidence_packages"]
    if not packages:
        raise PipelineError(
            "Final interpretation requires confirmed reviewed evidence."
        )
    prior_degraded = bool(working["warnings"] or working["errors"])
    working["workflow_state"] = "phase_b_running"
    try:
        routing_results = route_reviewed_evidence_packages(
            packages,
            light_client=light_client,
            strong_client=strong_client,
            light_model=light_model,
            strong_model=strong_model,
            timestamp=timestamp,
        )
    except (EvidenceConfirmationError, Stage35RoutingError) as exc:
        raise PipelineError("Stage 35 routing could not start.") from exc
    working["llm_routing_results"] = [
        dict(item) for item in routing_results
    ]
    working["final_interpretation_report"] = None
    failed = sum(item["status"] == "failed" for item in routing_results)
    model_warnings = sum(
        len(item["warnings"])
        for item in routing_results
        if item["status"] == "success"
    )
    for item in routing_results:
        _retain_warnings(working, item["warnings"])
    if failed:
        message = (
            f"Stage 35 completed with {failed} failed interpretation "
            "request(s); confirmed evidence was preserved."
        )
        _set_stage(
            working,
            "llm",
            "warning",
            progress_percent=100,
            message=message,
        )
        _set_api_status(working, "llm", "warning", message)
        _append_warning(working, message)
    elif model_warnings:
        message = (
            f"Stage 35 routed {len(routing_results)} confirmed variant(s) "
            f"with {model_warnings} model warning(s)."
        )
        _set_stage(
            working,
            "llm",
            "warning",
            progress_percent=100,
            message=message,
        )
        _set_api_status(working, "llm", "warning", message)
    else:
        message = (
            f"Stage 35 routed {len(routing_results)} confirmed variant(s)."
        )
        _set_stage(
            working,
            "llm",
            "success",
            progress_percent=100,
            message=message,
        )
        _set_api_status(working, "llm", "success", message)
    working["status"] = (
        "partial"
        if prior_degraded or failed or model_warnings
        else "success"
    )
    working["current_stage"] = "llm"
    working["progress_percent"] = 85
    return validate_pipeline_result(working)


def generate_final_interpretation_report(
    result: PipelineResult,
) -> PipelineResult:
    """Build Stage 36 Output B from Stage 35 results without raw evidence."""

    working = validate_pipeline_result(deepcopy(result))
    if not working["llm_routing_results"]:
        raise PipelineError(
            "Output B requires completed Stage 35 routing results."
        )
    try:
        report = build_final_interpretation_report(
            working["variant_count"],
            working["llm_routing_results"],
        )
    except FinalInterpretationReportError as exc:
        raise PipelineError("Stage 36 Output B could not be built.") from exc
    working["final_interpretation_report"] = dict(report)
    failed = sum(
        entry["status"] == "failed" for entry in report["entries"]
    )
    if failed:
        message = (
            f"Stage 36 produced Output B with {failed} explicit "
            "interpretation failure(s)."
        )
        _set_stage(
            working,
            "report",
            "warning",
            progress_percent=100,
            message=message,
        )
        _append_warning(working, message)
        working["status"] = "partial"
    else:
        message = (
            f"Stage 36 produced {report['variant_count']} final "
            "interpretation(s) in input order."
        )
        _set_stage(
            working,
            "report",
            "success",
            progress_percent=100,
            message=message,
        )
        if working["status"] != "partial":
            working["status"] = "success"
    working["current_stage"] = "report"
    working["progress_percent"] = 100
    working["workflow_state"] = "completed"
    return validate_pipeline_result(working)


def resume_confirmed_analysis(
    result: PipelineResult,
    reports: Sequence[Mapping[str, object]] | None = None,
    *,
    light_client: LLMClient | None = None,
    strong_client: LLMClient | None = None,
    light_model: str | None = None,
    strong_model: str | None = None,
    timestamp: str | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Resume the same paused analysis through Phase B and Output B."""

    working = validate_pipeline_result(deepcopy(result))
    if working["workflow_state"] != "awaiting_confirmation":
        raise PipelineError(
            "Phase B requires an analysis paused after Phase A."
        )
    if reports is not None:
        working = confirm_reviewed_evidence(
            working,
            reports,
            timestamp=timestamp,
        )
    expected_indexes = list(range(working["variant_count"]))
    confirmed_indexes = [
        package["variant_index"]
        for package in working["reviewed_evidence_packages"]
    ]
    if confirmed_indexes != expected_indexes:
        raise PipelineError(
            "Phase B requires confirmed reviewed evidence for every variant."
        )

    working["workflow_state"] = "phase_b_running"
    working["status"] = "running"
    working["current_stage"] = "llm"
    working["progress_percent"] = 50
    _set_stage(
        working,
        "llm",
        "running",
        progress_percent=0,
        message="Routing confirmed variants to the selected LLM layer.",
    )
    _set_stage(
        working,
        "report",
        "pending",
        progress_percent=0,
        message="Waiting for confirmed interpretations.",
    )
    _notify_progress(working, progress_callback)

    working = generate_confirmed_interpretations(
        working,
        light_client=light_client,
        strong_client=strong_client,
        light_model=light_model,
        strong_model=strong_model,
        timestamp=timestamp,
    )
    _notify_progress(working, progress_callback)
    working = generate_final_interpretation_report(working)
    _notify_progress(working, progress_callback)
    return working


__all__ = [
    "AnalysisInput",
    "MAX_PIPELINE_ERRORS",
    "MAX_PIPELINE_PHENOTYPES",
    "MAX_PIPELINE_WARNINGS",
    "PIPELINE_API_ORDER",
    "PIPELINE_SCHEMA_VERSION",
    "PIPELINE_STAGE_ORDER",
    "PIPELINE_WORKFLOW_STATE_VALUES",
    "PipelineAPIRecord",
    "PipelineAPIStatus",
    "PipelineError",
    "PipelineInputError",
    "PipelineIssue",
    "PipelineProgressCallback",
    "PipelineResult",
    "PipelineResultError",
    "PipelineStageRecord",
    "PipelineStageStatus",
    "PipelineStatus",
    "PipelineWorkflowState",
    "confirm_reviewed_evidence",
    "generate_confirmed_interpretations",
    "generate_final_interpretation_report",
    "resume_confirmed_analysis",
    "create_pipeline_result",
    "run_analysis",
    "run_annotation_and_phenotype",
    "run_variant_processing",
    "validate_analysis_input",
    "validate_pipeline_result",
]
