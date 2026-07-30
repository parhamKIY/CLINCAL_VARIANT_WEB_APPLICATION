"""Contracts for the complete clinical variant analysis pipeline."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, TypedDict, cast

from backend.prioritization import prioritize_variants
from backend.vcf_processing import VariantData, process_vcf


PIPELINE_SCHEMA_VERSION = "1.0"
MAX_PIPELINE_PHENOTYPES = 50
MAX_PIPELINE_WARNINGS = 100
MAX_PIPELINE_ERRORS = 100
MAX_PIPELINE_RETAINED_VARIANTS = 100

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


class PipelineError(RuntimeError):
    """Base error for the complete analysis pipeline."""


class PipelineInputError(PipelineError):
    """Raised when an analysis request violates its public contract."""


class PipelineResultError(PipelineError):
    """Raised when a frontend-safe pipeline result is malformed."""


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
    warnings: list[str]
    errors: list[PipelineIssue]


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


def _process_and_prioritize(
    request: AnalysisInput,
    result: PipelineResult,
    *,
    top_n: int | None,
    seed: int | None,
    sample_name: str | None,
    max_variants: int | None,
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
                retained_variants.append(dict(variant))
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
        dict(candidate)
        for candidate in candidates
    ]
    if result["variants_truncated"]:
        result["warnings"].append(
            "The pipeline retained only the first "
            f"{MAX_PIPELINE_RETAINED_VARIANTS} of {variant_count} "
            "parsed variants for display; candidate selection still "
            "evaluated the complete processed stream."
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
    "PipelineResult",
    "PipelineResultError",
    "PipelineStageRecord",
    "PipelineStageStatus",
    "PipelineStatus",
    "create_pipeline_result",
    "run_variant_selection",
    "validate_analysis_input",
    "validate_pipeline_result",
]
