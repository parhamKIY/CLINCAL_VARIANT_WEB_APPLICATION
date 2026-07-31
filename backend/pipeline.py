"""Contracts for the complete clinical variant analysis pipeline."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from copy import deepcopy
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import uuid4

import requests

from backend.annotation import AnnotationError, annotate_variants
from backend.database import DatabaseError, save_complete_analysis
from backend.error_handling import (
    PipelineError,
    PipelineInputError,
    PipelineResultError,
    map_pipeline_exception,
)
from backend.llm import LLMClient, LLMError
from backend.logging_config import (
    bind_analysis_run_id,
    get_logger,
    reset_analysis_run_id,
)
from backend.phenotype import (
    HPODataError,
    PhenotypeError,
    match_phenotypes,
)
from backend.privacy import (
    ClinicalDataPrivacyError,
    minimize_variant,
    validate_no_prohibited_fields,
)
from backend.prioritization import (
    PrioritizationError,
    prioritize_variants,
)
from backend.report import (
    ClinicalInterpretationError,
    ClinicalReportError,
    EvidenceObjectError,
    build_clinical_report,
    build_evidence_objects,
    generate_clinical_interpretation,
    save_clinical_report,
)
from backend.vcf_processing import (
    VCFProcessingError,
    VariantData,
    process_vcf,
)


PIPELINE_SCHEMA_VERSION = "1.1"
MAX_PIPELINE_PHENOTYPES = 50
MAX_PIPELINE_WARNINGS = 100
MAX_PIPELINE_ERRORS = 100
MAX_PIPELINE_RETAINED_VARIANTS = 100
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
PipelineInputMode = Literal["vcf", "manual"]

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
PIPELINE_STAGE_ORDER = (
    "input",
    "vcf_processing",
    "prioritization",
    "annotation",
    "phenotype",
    "evidence",
    "llm",
    "report",
)


class AnalysisInput(TypedDict):
    """Validated mutually exclusive VCF or manual-variant input."""

    input_mode: PipelineInputMode
    vcf_path: str | None
    manual_variant: str | None
    phenotypes: list[str]


class PipelineStageRecord(TypedDict):
    """Progress state for one stable pipeline stage."""

    stage: str
    status: PipelineStageStatus
    progress_percent: int
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
    status: PipelineStatus
    current_stage: str
    progress_percent: int
    stages: list[PipelineStageRecord]
    variant_count: int
    variants_truncated: bool
    variants: list[dict[str, object]]
    candidates: list[dict[str, object]]
    annotations: list[dict[str, object]]
    phenotype_results: list[dict[str, object]]
    evidence_objects: list[dict[str, object]]
    report_path: str | None
    analysis_id: str | None
    warnings: list[str]
    errors: list[PipelineIssue]


PipelineProgressCallback = Callable[[PipelineResult], None]


ANALYSIS_INPUT_FIELDS = frozenset(AnalysisInput.__required_keys__)
PIPELINE_STAGE_FIELDS = frozenset(
    PipelineStageRecord.__required_keys__
)
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
    manual_variant: str | None = None,
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

    normalized_manual_variant: str | None = None
    if manual_variant is not None:
        normalized_manual_variant = _required_text(
            manual_variant,
            "manual_variant",
            PipelineInputError,
        )

    source_count = sum(
        value is not None
        for value in (
            normalized_vcf_path,
            normalized_manual_variant,
        )
    )
    if source_count != 1:
        raise PipelineInputError(
            "Exactly one of vcf_path or manual_variant must be provided."
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
        "manual_variant": normalized_manual_variant,
        "phenotypes": normalized_phenotypes,
    }


def create_pipeline_result() -> PipelineResult:
    """Create an empty deterministic result before execution begins."""

    result: PipelineResult = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
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
        "variant_count": 0,
        "variants_truncated": False,
        "variants": [],
        "candidates": [],
        "annotations": [],
        "phenotype_results": [],
        "evidence_objects": [],
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
    variant_count = value["variant_count"]
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or variant_count < 0
    ):
        raise PipelineResultError(
            "pipeline.variant_count must be a non-negative integer."
        )
    if not isinstance(value["variants_truncated"], bool):
        raise PipelineResultError(
            "pipeline.variants_truncated must be a boolean."
        )
    for field in (
        "variants",
        "candidates",
        "annotations",
        "phenotype_results",
        "evidence_objects",
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
                    "candidates",
                    "annotations",
                    "phenotype_results",
                    "evidence_objects",
                )
            },
            context="Pipeline result",
        )
    except ClinicalDataPrivacyError as exc:
        raise PipelineResultError(
            "Pipeline result contains prohibited clinical data."
        ) from exc
    if len(value["variants"]) > MAX_PIPELINE_RETAINED_VARIANTS:
        raise PipelineResultError(
            "pipeline.variants exceeds the retained-variant maximum of "
            f"{MAX_PIPELINE_RETAINED_VARIANTS}."
        )
    if variant_count < len(value["variants"]):
        raise PipelineResultError(
            "pipeline.variant_count cannot be smaller than the retained "
            "variant list."
        )
    if value["variants_truncated"] != (
        variant_count > len(value["variants"])
    ):
        raise PipelineResultError(
            "pipeline.variants_truncated does not match variant_count."
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

    has_retained_output = any(
        result[field]
        for field in (
            "variants",
            "candidates",
            "annotations",
            "phenotype_results",
            "evidence_objects",
        )
    )
    result["status"] = (
        "partial"
        if recoverable and has_retained_output
        else "error"
    )
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
            candidates=result["candidates"],
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


def _process_and_prioritize(
    request: AnalysisInput,
    result: PipelineResult,
    *,
    top_n: int | None,
    seed: int | None,
    sample_name: str | None,
    max_variants: int | None,
    progress_callback: PipelineProgressCallback | None = None,
) -> None:
    """Stream standardized variants into bounded candidate selection."""

    result["status"] = "running"
    result["current_stage"] = "vcf_processing"
    result["progress_percent"] = 10
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
        message="Processing standardized variants.",
    )
    _notify_progress(result, progress_callback)

    variant_stream = process_vcf(
        vcf_path=request["vcf_path"],
        manual_variant=request["manual_variant"],
        sample_name=sample_name,
        max_variants=max_variants,
    )
    retained_variants: list[dict[str, object]] = []
    variant_count = 0

    def track_variants() -> Iterator[VariantData]:
        nonlocal variant_count
        for variant in variant_stream:
            variant_count += 1
            if (
                len(retained_variants)
                < MAX_PIPELINE_RETAINED_VARIANTS
            ):
                retained_variants.append(minimize_variant(variant))
            yield variant

    result["current_stage"] = "prioritization"
    result["progress_percent"] = 20
    _set_stage(
        result,
        "prioritization",
        "running",
        progress_percent=0,
        message="Selecting bounded MVP candidates.",
    )
    _notify_progress(result, progress_callback)
    candidates = prioritize_variants(
        track_variants(),
        top_n=top_n,
        seed=seed,
    )
    if variant_count == 0:
        raise PipelineError(
            "Variant processing produced no variants."
        )

    result["variant_count"] = variant_count
    result["variants"] = retained_variants
    result["variants_truncated"] = (
        variant_count > len(retained_variants)
    )
    result["candidates"] = [
        minimize_variant(candidate)
        for candidate in candidates
    ]
    if result["variants_truncated"]:
        result["warnings"].append(
            "The pipeline retained only the first "
            f"{MAX_PIPELINE_RETAINED_VARIANTS} of {variant_count} "
            "parsed variants for display; candidate selection still "
            "evaluated the complete processed stream."
        )
    LOGGER.info(
        "event=variant_selection_finished variant_count=%d "
        "retained_variant_count=%d variants_truncated=%s "
        "candidate_count=%d",
        variant_count,
        len(retained_variants),
        result["variants_truncated"],
        len(candidates),
    )

    _set_stage(
        result,
        "vcf_processing",
        "success",
        progress_percent=100,
        message=f"Processed {variant_count} variants.",
    )
    _set_stage(
        result,
        "prioritization",
        "success",
        progress_percent=100,
        message=f"Selected {len(candidates)} candidates.",
    )
    result["current_stage"] = "annotation"
    result["progress_percent"] = 30
    _notify_progress(result, progress_callback)


def _retain_annotation_warnings(
    result: PipelineResult,
    annotations: list[dict[str, object]],
) -> None:
    """Copy unique annotation warnings into the bounded pipeline result."""

    for annotation in annotations:
        warnings = annotation.get("warnings", [])
        if not isinstance(warnings, list):
            continue
        for warning in warnings:
            if (
                not isinstance(warning, str)
                or not warning.strip()
            ):
                continue
            _append_warning(result, warning)


def _annotate_and_match(
    request: AnalysisInput,
    result: PipelineResult,
    *,
    annotation_batch_size: int | None,
    annotation_max_retries: int | None,
    annotation_session: requests.Session | None,
    ontology_path: str | Path | None,
    associations_path: str | Path | None,
    progress_callback: PipelineProgressCallback | None = None,
) -> None:
    """Enrich selected candidates and attach optional HPO scores."""

    result["current_stage"] = "annotation"
    result["progress_percent"] = 35
    _set_stage(
        result,
        "annotation",
        "running",
        progress_percent=0,
        message="Annotating selected candidates.",
    )
    _notify_progress(result, progress_callback)
    annotations = annotate_variants(
        result["candidates"],
        batch_size=annotation_batch_size,
        max_retries=annotation_max_retries,
        session=annotation_session,
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
            f"Annotated {len(result['annotations'])} candidates"
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
                "candidates continued without phenotype scores."
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
            _set_stage(
                result,
                "phenotype",
                "warning",
                progress_percent=100,
                message=message,
            )
        else:
            public_phenotype_results = [
                dict(candidate)
                for candidate in phenotype_results
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
                "success",
                progress_percent=100,
                message=(
                    "Attached phenotype scores to "
                    f"{len(result['phenotype_results'])} candidates."
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
    progress_callback: PipelineProgressCallback | None = None,
) -> None:
    """Build all Evidence Objects and report the leading candidate."""

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
    evidence_objects = build_evidence_objects(
        result["phenotype_results"]
    )
    if not evidence_objects:
        raise PipelineError(
            "Evidence Object construction produced no candidates."
        )
    result["evidence_objects"] = [
        dict(evidence)
        for evidence in evidence_objects
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
        message=f"Built {len(evidence_objects)} Evidence Objects.",
    )

    leading_evidence = evidence_objects[0]
    result["current_stage"] = "llm"
    result["progress_percent"] = 75
    _set_stage(
        result,
        "llm",
        "running",
        progress_percent=0,
        message="Interpreting the leading candidate.",
    )
    _notify_progress(result, progress_callback)
    interpretation = generate_clinical_interpretation(
        leading_evidence,
        client=llm_client,
        model=llm_model,
    )
    _set_stage(
        result,
        "llm",
        "success",
        progress_percent=100,
        message=(
            "Generated an evidence-bound interpretation for the "
            "leading candidate."
        ),
    )

    result["current_stage"] = "report"
    result["progress_percent"] = 90
    _set_stage(
        result,
        "report",
        "running",
        progress_percent=0,
        message="Building and saving the clinical report.",
    )
    _notify_progress(result, progress_callback)
    report = build_clinical_report(
        leading_evidence,
        interpretation,
    )
    report_path = save_clinical_report(
        report,
        report_dir=report_dir,
    )
    result["report_path"] = str(report_path)
    report_id = report_path.stem.rsplit("-", 1)[-1]
    LOGGER.info(
        "event=report_saved report_directory=%s report_id=%s",
        report_path.parent,
        report_id,
    )
    _set_stage(
        result,
        "report",
        "success",
        progress_percent=100,
        message="Saved the leading-candidate clinical report.",
    )
    result["status"] = (
        "partial"
        if result["warnings"] or result["errors"]
        else "success"
    )
    result["current_stage"] = "completed"
    result["progress_percent"] = 100
    _notify_progress(result, progress_callback)


def run_variant_selection(
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variant: str | None = None,
    *,
    top_n: int | None = None,
    seed: int | None = None,
    sample_name: str | None = None,
    max_variants: int | None = None,
) -> PipelineResult:
    """Run Stage 10 through processing and MVP prioritization."""

    request = validate_analysis_input(
        vcf_path=vcf_path,
        manual_variant=manual_variant,
        phenotypes=phenotypes,
    )
    result = create_pipeline_result()
    _process_and_prioritize(
        request,
        result,
        top_n=top_n,
        seed=seed,
        sample_name=sample_name,
        max_variants=max_variants,
    )
    return validate_pipeline_result(result)


def run_annotation_and_phenotype(
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variant: str | None = None,
    *,
    top_n: int | None = None,
    seed: int | None = None,
    sample_name: str | None = None,
    max_variants: int | None = None,
    annotation_batch_size: int | None = None,
    annotation_max_retries: int | None = None,
    annotation_session: requests.Session | None = None,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
) -> PipelineResult:
    """Run Stage 10 through annotation and optional HPO matching."""

    request = validate_analysis_input(
        vcf_path=vcf_path,
        manual_variant=manual_variant,
        phenotypes=phenotypes,
    )
    result = create_pipeline_result()
    _process_and_prioritize(
        request,
        result,
        top_n=top_n,
        seed=seed,
        sample_name=sample_name,
        max_variants=max_variants,
    )
    _annotate_and_match(
        request,
        result,
        annotation_batch_size=annotation_batch_size,
        annotation_max_retries=annotation_max_retries,
        annotation_session=annotation_session,
        ontology_path=ontology_path,
        associations_path=associations_path,
    )
    return validate_pipeline_result(result)


def _run_analysis_unpersisted(
    vcf_path: str | Path | None,
    phenotypes: list[str] | tuple[str, ...],
    manual_variant: str | None = None,
    *,
    top_n: int | None = None,
    seed: int | None = None,
    sample_name: str | None = None,
    max_variants: int | None = None,
    annotation_batch_size: int | None = None,
    annotation_max_retries: int | None = None,
    annotation_session: requests.Session | None = None,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
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
            manual_variant=manual_variant,
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
        _process_and_prioritize(
            request,
            result,
            top_n=top_n,
            seed=seed,
            sample_name=sample_name,
            max_variants=max_variants,
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
    except PrioritizationError as exc:
        return _finish_exception(
            result,
            stage="prioritization",
            error=exc,
            default_code="prioritization_failed",
            default_message="Candidate prioritization could not be completed.",
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
                "Candidate enrichment stopped because of an unexpected "
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
            progress_callback=progress_callback,
        )
    except EvidenceObjectError as exc:
        return _finish_exception(
            result,
            stage="evidence",
            error=exc,
            default_code="evidence_object_failed",
            default_message="Evidence construction could not be completed.",
            default_recoverable=False,
            progress_callback=progress_callback,
        )
    except (LLMError, ClinicalInterpretationError) as exc:
        return _finish_exception(
            result,
            stage="llm",
            error=exc,
            default_code="llm_interpretation_failed",
            default_message=(
                "Clinical interpretation could not be completed."
            ),
            default_recoverable=True,
            progress_callback=progress_callback,
        )
    except ClinicalReportError as exc:
        return _finish_exception(
            result,
            stage="report",
            error=exc,
            default_code="report_generation_failed",
            default_message="The clinical report could not be generated.",
            default_recoverable=True,
            progress_callback=progress_callback,
        )
    except PipelineError as exc:
        return _finish_exception(
            result,
            stage="evidence",
            error=exc,
            default_code="evidence_object_failed",
            default_message="Evidence construction produced no candidates.",
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
    manual_variant: str | None = None,
    *,
    top_n: int | None = None,
    seed: int | None = None,
    sample_name: str | None = None,
    max_variants: int | None = None,
    annotation_batch_size: int | None = None,
    annotation_max_retries: int | None = None,
    annotation_session: requests.Session | None = None,
    ontology_path: str | Path | None = None,
    associations_path: str | Path | None = None,
    llm_client: LLMClient | None = None,
    llm_model: str | None = None,
    report_dir: str | Path | None = None,
    database_path: str | Path | None = None,
    persist_analysis: bool = True,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Run the complete pipeline and optionally persist terminal output."""

    if vcf_path is not None and manual_variant is None:
        input_mode = "vcf"
    elif manual_variant is not None and vcf_path is None:
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
            manual_variant=manual_variant,
            top_n=top_n,
            seed=seed,
            sample_name=sample_name,
            max_variants=max_variants,
            annotation_batch_size=annotation_batch_size,
            annotation_max_retries=annotation_max_retries,
            annotation_session=annotation_session,
            ontology_path=ontology_path,
            associations_path=associations_path,
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
                manual_variant=manual_variant,
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


__all__ = [
    "AnalysisInput",
    "MAX_PIPELINE_ERRORS",
    "MAX_PIPELINE_PHENOTYPES",
    "MAX_PIPELINE_RETAINED_VARIANTS",
    "MAX_PIPELINE_WARNINGS",
    "PIPELINE_SCHEMA_VERSION",
    "PIPELINE_STAGE_ORDER",
    "PipelineError",
    "PipelineInputError",
    "PipelineIssue",
    "PipelineProgressCallback",
    "PipelineResult",
    "PipelineResultError",
    "PipelineStageRecord",
    "PipelineStageStatus",
    "PipelineStatus",
    "create_pipeline_result",
    "run_analysis",
    "run_annotation_and_phenotype",
    "run_variant_selection",
    "validate_analysis_input",
    "validate_pipeline_result",
]
