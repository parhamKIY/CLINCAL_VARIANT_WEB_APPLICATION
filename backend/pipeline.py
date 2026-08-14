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
from backend.call_quality import (
    CallQualityError,
    build_call_quality_records,
    validate_call_quality_record,
)
from backend.conditional_enrichment import enrich_conditionally
from backend.database import (
    DatabaseError,
    load_pipeline_state,
    save_complete_analysis,
    save_pipeline_state,
)
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
from backend.evidence_readiness import (
    EvidenceReadinessError,
    build_evidence_readiness_audit,
    validate_evidence_readiness_audit,
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
from backend.final_clinical_report import (
    FinalClinicalReportError,
    compose_final_clinical_report,
    validate_final_clinical_report,
)
from backend.llm import LLMClient
from backend.llm_routing import (
    RoutingProgressCallback,
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
    validate_evidence_object,
)
from backend.report_lifecycle import (
    ReportLifecycleError,
    build_variant_report_records,
    validate_variant_report_record,
)
from backend.variant_interpretation import (
    VariantInterpretationError,
    interpret_variants,
    retry_variant_interpretation,
    validate_variant_interpretation_result,
)
from backend.variant_integrity import (
    VariantIntegrityError,
    build_variant_integrity_records,
    cardinality_counts,
    index_input_variants,
    validate_variant_integrity_record,
)
from backend.variant_report import (
    DraftVariantReportError,
    build_draft_variant_report,
    build_draft_variant_reports,
    validate_draft_variant_report,
)
from backend.vcf_processing import (
    VCFProcessingError,
    parse_manual_variants,
    process_vcf,
)
from config import MAX_VARIANTS_PER_ANALYSIS, settings
PIPELINE_SCHEMA_VERSION = "3.2"
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
PersistedInputType = Literal["vcf", "vcf_gz", "excel", "manual"]
PipelineWorkflowState = Literal[
    "analysis_running",
    "awaiting_final_review",
    "finalization_running",
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
    "analysis_running",
    "awaiting_final_review",
    "finalization_running",
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
ANNOTATION_API_ORDER = PIPELINE_API_ORDER[:6]
ANNOTATION_PROGRESS_START = 35
ANNOTATION_PROGRESS_END = 49
PHENOTYPE_API_PROGRESS = {
    "phen2gene": (52, 54),
    "mydisease": (56, 59),
}


class AnalysisInput(TypedDict):
    """Validated mutually exclusive filtered VCF-table input."""

    input_mode: PipelineInputMode
    vcf_path: str | None
    manual_variants: list[dict[str, object]] | None
    phenotypes: list[str]


class AnalysisContext(TypedDict):
    """Stage 57 analysis-level state required for durable recovery."""

    input_type: PersistedInputType | None
    accepted_hpo_terms: list[str]
    phenotype_extraction_model: str | None
    variant_interpretation_model: str | None
    phenotype_extraction_provenance: dict[str, object] | None


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
    analysis_context: AnalysisContext
    variant_count: int
    variants: list[dict[str, object]]
    variant_integrity_records: list[dict[str, object]]
    annotations: list[dict[str, object]]
    phenotype_results: list[dict[str, object]]
    evidence_objects: list[dict[str, object]]
    evidence_readiness: list[dict[str, object]]
    variant_interpretation_results: list[dict[str, object]]
    draft_variant_reports: list[dict[str, object]]
    variant_report_records: list[dict[str, object]]
    evidence_review_reports: list[dict[str, object]]
    reviewed_evidence_packages: list[dict[str, object]]
    llm_routing_results: list[dict[str, object]]
    final_interpretation_report: dict[str, object] | None
    final_clinical_report: dict[str, object] | None
    report_path: str | None
    analysis_id: str | None
    warnings: list[str]
    errors: list[PipelineIssue]


PipelineProgressCallback = Callable[[PipelineResult], None]


ANALYSIS_INPUT_FIELDS = frozenset(AnalysisInput.__required_keys__)
ANALYSIS_CONTEXT_FIELDS = frozenset(AnalysisContext.__required_keys__)
PHENOTYPE_EXTRACTION_PROVENANCE_FIELDS = frozenset(
    {
        "schema_version",
        "task",
        "prompt_version",
        "model",
        "candidate_hpo_ids",
    }
)
PIPELINE_STAGE_FIELDS = frozenset(
    PipelineStageRecord.__required_keys__
)
PIPELINE_API_FIELDS = frozenset(PipelineAPIRecord.__required_keys__)
PIPELINE_ISSUE_FIELDS = frozenset(PipelineIssue.__required_keys__)
PIPELINE_RESULT_FIELDS = frozenset(PipelineResult.__required_keys__)


def _annotation_api_progress_percent(
    source: str,
    status: AnnotationProgressStatus,
) -> int:
    """Map each normal annotation API update into the 35-49% range."""

    try:
        source_index = ANNOTATION_API_ORDER.index(source)
    except ValueError:
        return ANNOTATION_PROGRESS_START
    event_index = source_index * 2 + (1 if status == "running" else 2)
    event_count = len(ANNOTATION_API_ORDER) * 2
    progress_span = ANNOTATION_PROGRESS_END - ANNOTATION_PROGRESS_START
    return ANNOTATION_PROGRESS_START + (
        event_index * progress_span + event_count // 2
    ) // event_count


def _notify_api_progress(
    result: PipelineResult,
    source: str,
    status: PipelineAPIStatus,
    callback: PipelineProgressCallback | None,
) -> None:
    """Advance and publish the progress assigned to one API update."""

    if source in ANNOTATION_API_ORDER:
        progress = _annotation_api_progress_percent(
            source,
            cast(AnnotationProgressStatus, status),
        )
    else:
        progress_points = PHENOTYPE_API_PROGRESS.get(source)
        progress = (
            progress_points[0 if status == "running" else 1]
            if progress_points is not None
            else result["progress_percent"]
        )
    result["progress_percent"] = max(
        result["progress_percent"],
        progress,
    )
    _notify_progress(result, callback)


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
        "workflow_state": "analysis_running",
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
        "analysis_context": {
            "input_type": None,
            "accepted_hpo_terms": [],
            "phenotype_extraction_model": None,
            "variant_interpretation_model": None,
            "phenotype_extraction_provenance": None,
        },
        "variant_count": 0,
        "variants": [],
        "variant_integrity_records": [],
        "annotations": [],
        "phenotype_results": [],
        "evidence_objects": [],
        "evidence_readiness": [],
        "variant_interpretation_results": [],
        "draft_variant_reports": [],
        "variant_report_records": [],
        "evidence_review_reports": [],
        "reviewed_evidence_packages": [],
        "llm_routing_results": [],
        "final_interpretation_report": None,
        "final_clinical_report": None,
        "report_path": None,
        "analysis_id": None,
        "warnings": [],
        "errors": [],
    }
    return validate_pipeline_result(result)


def validate_analysis_context(value: object) -> AnalysisContext:
    """Validate persisted task models, accepted HPOs, and provenance."""

    if not isinstance(value, dict) or set(value) != ANALYSIS_CONTEXT_FIELDS:
        raise PipelineResultError(
            "pipeline.analysis_context has invalid fields."
        )
    input_type = value["input_type"]
    if input_type not in {None, "vcf", "vcf_gz", "excel", "manual"}:
        raise PipelineResultError(
            "pipeline.analysis_context.input_type is unsupported."
        )
    accepted = value["accepted_hpo_terms"]
    if (
        not isinstance(accepted, list)
        or len(accepted) > MAX_PIPELINE_PHENOTYPES
        or len(accepted) != len(set(accepted))
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"HP:[0-9]{7}", item) is None
            for item in accepted
        )
    ):
        raise PipelineResultError(
            "pipeline.analysis_context.accepted_hpo_terms is invalid."
        )
    for field in (
        "phenotype_extraction_model",
        "variant_interpretation_model",
    ):
        model = value[field]
        if model is not None:
            normalized = _required_text(
                model,
                f"pipeline.analysis_context.{field}",
                PipelineResultError,
            )
            if normalized != model or len(normalized) > 500:
                raise PipelineResultError(
                    f"pipeline.analysis_context.{field} is invalid."
                )
    provenance = value["phenotype_extraction_provenance"]
    if provenance is not None:
        if (
            not isinstance(provenance, dict)
            or set(provenance)
            != PHENOTYPE_EXTRACTION_PROVENANCE_FIELDS
        ):
            raise PipelineResultError(
                "pipeline.analysis_context phenotype provenance is invalid."
            )
        for field in ("schema_version", "task", "prompt_version", "model"):
            normalized = _required_text(
                provenance[field],
                f"pipeline.analysis_context.phenotype_extraction_provenance.{field}",
                PipelineResultError,
            )
            if normalized != provenance[field] or len(normalized) > 500:
                raise PipelineResultError(
                    "pipeline.analysis_context phenotype provenance is invalid."
                )
        candidates = provenance["candidate_hpo_ids"]
        if (
            not isinstance(candidates, list)
            or len(candidates) > 25
            or len(candidates) != len(set(candidates))
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"HP:[0-9]{7}", item) is None
                for item in candidates
            )
        ):
            raise PipelineResultError(
                "pipeline.analysis_context phenotype provenance is invalid."
            )
        if value["phenotype_extraction_model"] is None:
            raise PipelineResultError(
                "pipeline.analysis_context phenotype provenance requires "
                "its selected model."
            )
    return cast(AnalysisContext, deepcopy(value))


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
    analysis_context = validate_analysis_context(
        value["analysis_context"]
    )
    variant_count = value["variant_count"]
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or not 0 <= variant_count <= MAX_VARIANTS_PER_ANALYSIS
    ):
        raise PipelineResultError(
            "pipeline.variant_count must be an integer from 0 to "
            f"{MAX_VARIANTS_PER_ANALYSIS}."
        )
    for field in (
        "variants",
        "variant_integrity_records",
        "annotations",
        "phenotype_results",
        "evidence_objects",
        "evidence_readiness",
        "variant_interpretation_results",
        "draft_variant_reports",
        "variant_report_records",
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
                    "variant_integrity_records",
                    "analysis_context",
                    "annotations",
                    "phenotype_results",
                    "evidence_objects",
                    "evidence_readiness",
                    "variant_interpretation_results",
                    "draft_variant_reports",
                    "variant_report_records",
                    "evidence_review_reports",
                    "reviewed_evidence_packages",
                    "llm_routing_results",
                    "final_interpretation_report",
                    "final_clinical_report",
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
    for index, variant in enumerate(value["variants"]):
        if not isinstance(variant, dict):
            raise PipelineResultError("pipeline.variants must contain dictionaries.")
        call_quality = variant.get("call_quality")
        if call_quality is None:
            continue
        try:
            validated_call_quality = validate_call_quality_record(call_quality)
        except CallQualityError as exc:
            raise PipelineResultError("pipeline variant call quality is invalid.") from exc
        if variant.get("input_index") != index or call_quality != validated_call_quality:
            raise PipelineResultError(
                "pipeline variant call quality does not match input order."
            )
    integrity_records = value["variant_integrity_records"]
    if integrity_records:
        for field in (
            "annotations",
            "phenotype_results",
            "evidence_objects",
        ):
            collection = value[field]
            if collection and len(collection) != variant_count:
                raise PipelineResultError(
                    f"pipeline.{field} must preserve accepted input "
                    "cardinality."
                )
        if len(integrity_records) != variant_count:
            raise PipelineResultError(
                "pipeline.variant_integrity_records must match accepted "
                "input cardinality."
            )
        try:
            validated_integrity_records = [
                validate_variant_integrity_record(record)
                for record in integrity_records
            ]
        except VariantIntegrityError as exc:
            raise PipelineResultError(
                "pipeline.variant_integrity_records is invalid."
            ) from exc
        if [
            record["input_index"] for record in validated_integrity_records
        ] != list(range(variant_count)):
            raise PipelineResultError(
                "pipeline.variant_integrity_records must preserve input order."
            )
    if analysis_context["accepted_hpo_terms"] and value[
        "phenotype_results"
    ]:
        accepted_hpo_terms = analysis_context["accepted_hpo_terms"]
        for phenotype_result in value["phenotype_results"]:
            phenotype_context = phenotype_result.get("phenotype_context")
            if isinstance(phenotype_context, dict):
                observed = phenotype_context.get("patient_hpo_terms")
                if (
                    isinstance(observed, list)
                    and observed
                    and observed != accepted_hpo_terms
                ):
                    raise PipelineResultError(
                        "pipeline.analysis_context accepted HPO terms do "
                        "not match phenotype results."
                    )
    review_reports = value["evidence_review_reports"]
    readiness_audits = value["evidence_readiness"]
    if readiness_audits:
        if len(readiness_audits) != len(value["evidence_objects"]):
            raise PipelineResultError(
                "pipeline.evidence_readiness must match the Evidence "
                "Object count."
            )
        try:
            validated_readiness = [
                validate_evidence_readiness_audit(audit)
                for audit in readiness_audits
            ]
        except EvidenceReadinessError as exc:
            raise PipelineResultError(
                "pipeline.evidence_readiness is invalid."
            ) from exc
        if [
            audit["variant_index"] for audit in validated_readiness
        ] != list(range(len(validated_readiness))):
            raise PipelineResultError(
                "pipeline.evidence_readiness must preserve variant order."
            )
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
                or validate_evidence_object(report["original_machine_report"])
                != validate_evidence_object(value["evidence_objects"][index])
            ):
                raise PipelineResultError(
                    "pipeline.evidence_review_reports must preserve "
                    "Evidence Object order and originals."
                )
    interpretation_results = value["variant_interpretation_results"]
    if interpretation_results:
        if len(readiness_audits) != len(interpretation_results):
            raise PipelineResultError(
                "Every interpretation requires an evidence-readiness audit."
            )
        if any(
            audit.get("readiness_after_rescue")
            not in {"READY", "READY_WITH_LIMITATIONS"}
            for audit in readiness_audits
        ):
            raise PipelineResultError(
                "Interpretation cannot precede the final readiness decision."
            )
        if len(interpretation_results) != len(value["evidence_objects"]):
            raise PipelineResultError(
                "pipeline.variant_interpretation_results must match the "
                "Evidence Object count."
            )
        previous_index = -1
        for item in interpretation_results:
            index = item.get("variant_index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index <= previous_index
                or index >= len(value["evidence_objects"])
            ):
                raise PipelineResultError(
                    "pipeline.variant_interpretation_results must preserve "
                    "ascending variant order without duplicates."
                )
            try:
                validate_variant_interpretation_result(
                    item,
                    evidence=value["evidence_objects"][index],
                )
            except VariantInterpretationError as exc:
                raise PipelineResultError(
                    "pipeline.variant_interpretation_results is invalid."
                ) from exc
            previous_index = index
    draft_variant_reports = value["draft_variant_reports"]
    if draft_variant_reports:
        if len(draft_variant_reports) != len(value["evidence_objects"]):
            raise PipelineResultError(
                "pipeline.draft_variant_reports must match the Evidence "
                "Object count."
            )
        for expected_index, report in enumerate(draft_variant_reports):
            if report.get("variant_index") != expected_index:
                raise PipelineResultError(
                    "pipeline.draft_variant_reports must preserve variant "
                    "order."
                )
            try:
                validate_draft_variant_report(
                    report,
                    evidence=value["evidence_objects"][expected_index],
                    interpretation=interpretation_results[expected_index],
                )
            except DraftVariantReportError as exc:
                raise PipelineResultError(
                    "pipeline.draft_variant_reports is invalid."
                ) from exc
    variant_report_records = value["variant_report_records"]
    if variant_report_records:
        if len(variant_report_records) != len(draft_variant_reports):
            raise PipelineResultError(
                "pipeline.variant_report_records must match the Draft "
                "Variant Report count."
            )
        try:
            validated_lifecycle_records = [
                validate_variant_report_record(record)
                for record in variant_report_records
            ]
        except ReportLifecycleError as exc:
            raise PipelineResultError(
                "pipeline.variant_report_records is invalid."
            ) from exc
        if [
            record["variant_index"] for record in validated_lifecycle_records
        ] != list(range(len(validated_lifecycle_records))):
            raise PipelineResultError(
                "pipeline.variant_report_records must preserve original "
                "input order."
            )
    if integrity_records:
        try:
            expected_integrity_records = build_variant_integrity_records(
                cast(list[Mapping[str, object]], value["variants"]),
                cast(list[Mapping[str, object]], value["variants"]),
                assembly=(
                    cast(str, value["evidence_objects"][0]["assembly"])
                    if value["evidence_objects"]
                    else settings.GENOME_ASSEMBLY
                ),
                evidence_objects=cast(
                    list[Mapping[str, object]], value["evidence_objects"]
                ),
                draft_reports=cast(
                    list[Mapping[str, object]], draft_variant_reports
                ),
                review_records=cast(
                    list[Mapping[str, object]], variant_report_records
                ),
            )
        except VariantIntegrityError as exc:
            raise PipelineResultError(
                "pipeline variant cardinality or allele identity is invalid."
            ) from exc
        if integrity_records != expected_integrity_records:
            raise PipelineResultError(
                "pipeline.variant_integrity_records do not match current "
                "variant objects."
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
                or validate_evidence_object(package["original_machine_report"])
                != validate_evidence_object(value["evidence_objects"][index])
            ):
                raise PipelineResultError(
                    "pipeline.reviewed_evidence_packages must reference "
                    "an unmodified Evidence Object."
                )
            previous_index = index
    if variant_report_records:
        raw_final = value["final_clinical_report"]
        finalized_at = (
            raw_final.get("generated_at")
            if value["workflow_state"] == "completed"
            and isinstance(raw_final, dict)
            else None
        )
        try:
            expected_lifecycle_records = build_variant_report_records(
                cast(list[Mapping[str, object]], draft_variant_reports),
                analysis_id=cast(str | None, value["analysis_id"]),
                confirmed_packages=cast(
                    list[Mapping[str, object]], validated_packages
                ),
                finalized_at=cast(str | None, finalized_at),
            )
        except ReportLifecycleError as exc:
            raise PipelineResultError(
                "pipeline.variant_report_records could not be verified."
            ) from exc
        if variant_report_records != expected_lifecycle_records:
            raise PipelineResultError(
                "pipeline.variant_report_records do not match the current "
                "review lifecycle."
            )
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
    if workflow_state == "awaiting_final_review" and (
        not review_reports
        or len(interpretation_results) != variant_count
        or len(draft_variant_reports) != variant_count
    ):
        raise PipelineResultError(
            "pipeline.awaiting_final_review requires evidence, "
            "interpretation and one Draft Variant Report per variant."
        )
    if (
        workflow_state == "completed"
        and final_report is None
        and (
            len(interpretation_results) != variant_count
            or len(draft_variant_reports) != variant_count
            or len(validated_packages) != variant_count
        )
    ):
        raise PipelineResultError(
            "pipeline.completed requires finalized reviewed variants."
        )
    if final_report is not None and workflow_state != "completed":
        raise PipelineResultError(
            "pipeline Output B requires the completed workflow state."
        )
    final_clinical_report = value["final_clinical_report"]
    if final_clinical_report is not None:
        try:
            validated_clinical_report = validate_final_clinical_report(
                final_clinical_report
            )
            expected_clinical_report = compose_final_clinical_report(
                value,
                timestamp=validated_clinical_report["generated_at"],
            )
        except FinalClinicalReportError as exc:
            raise PipelineResultError(
                "pipeline.final_clinical_report is invalid."
            ) from exc
        if validated_clinical_report != expected_clinical_report:
            raise PipelineResultError(
                "pipeline.final_clinical_report does not match the "
                "reviewer-approved state."
            )
        if workflow_state != "completed":
            raise PipelineResultError(
                "pipeline.final_clinical_report requires the completed "
                "workflow state."
            )
    active_final_review = bool(
        interpretation_results or draft_variant_reports
    )
    if workflow_state == "completed" and active_final_review and (
        len(interpretation_results) != variant_count
        or len(draft_variant_reports) != variant_count
        or len(validated_packages) != variant_count
        or final_clinical_report is None
    ):
        raise PipelineResultError(
            "pipeline.completed final review requires a Final Clinical "
            "Report composed from every confirmed reviewer decision."
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
            "variant_integrity_records",
            "annotations",
            "phenotype_results",
            "evidence_objects",
            "variant_interpretation_results",
            "draft_variant_reports",
            "variant_report_records",
            "evidence_review_reports",
            "reviewed_evidence_packages",
            "llm_routing_results",
            "final_interpretation_report",
            "final_clinical_report",
        )
    )
    result["status"] = (
        "partial"
        if recoverable and has_retained_output
        else "error"
    )
    result["workflow_state"] = "failed"
    result["current_stage"] = stage
    if result["variants"]:
        _sync_variant_integrity_records(result)
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
    _sync_variant_report_records(result)
    try:
        save_pipeline_state(result, database_path=database_path)
    except DatabaseError:
        _append_warning(
            result,
            "The analysis was saved, but its resumable review state could "
            "not be saved.",
        )
        if result["status"] == "success":
            result["status"] = "partial"


def _sync_variant_report_records(
    result: PipelineResult,
    *,
    finalized_at: str | None = None,
) -> None:
    """Rebuild the primary records from current audited review state."""

    if not result["draft_variant_reports"]:
        result["variant_report_records"] = []
        return
    if (
        finalized_at is None
        and result["workflow_state"] == "completed"
        and result["final_clinical_report"] is not None
    ):
        finalized_at = cast(
            str,
            result["final_clinical_report"]["generated_at"],
        )
    try:
        records = build_variant_report_records(
            cast(
                list[Mapping[str, object]],
                result["draft_variant_reports"],
            ),
            analysis_id=result["analysis_id"],
            confirmed_packages=cast(
                list[Mapping[str, object]],
                result["reviewed_evidence_packages"],
            ),
            finalized_at=finalized_at,
        )
    except ReportLifecycleError as exc:
        raise PipelineError(
            "Per-variant report lifecycle records could not be synchronized."
        ) from exc
    result["variant_report_records"] = [dict(item) for item in records]


def _sync_variant_integrity_records(result: PipelineResult) -> None:
    """Rebuild the Stage 87 count/order/allele lineage gate."""

    if not result["variants"]:
        result["variant_integrity_records"] = []
        return
    try:
        records = build_variant_integrity_records(
            cast(list[Mapping[str, object]], result["variants"]),
            cast(list[Mapping[str, object]], result["variants"]),
            assembly=(
                cast(str, result["evidence_objects"][0]["assembly"])
                if result["evidence_objects"]
                else settings.GENOME_ASSEMBLY
            ),
            evidence_objects=cast(
                list[Mapping[str, object]], result["evidence_objects"]
            ),
            draft_reports=cast(
                list[Mapping[str, object]], result["draft_variant_reports"]
            ),
            review_records=cast(
                list[Mapping[str, object]], result["variant_report_records"]
            ),
        )
    except VariantIntegrityError as exc:
        raise PipelineError(
            "Variant cardinality or allele identity changed unexpectedly."
        ) from exc
    result["variant_integrity_records"] = [dict(record) for record in records]


def _process_filtered_variants(
    request: AnalysisInput,
    result: PipelineResult,
    *,
    call_quality_acknowledgements: Mapping[int, object] | None = None,
    call_quality_overrides: Mapping[int, object] | None = None,
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

    try:
        indexed_variants = index_input_variants(raw_variants)
    except VariantIntegrityError as exc:
        raise PipelineError(
            "Accepted input variant order could not be established."
        ) from exc
    try:
        call_quality_records = build_call_quality_records(
            indexed_variants,
            acknowledgements=call_quality_acknowledgements,
            overrides=call_quality_overrides,
            require_ready=True,
        )
    except CallQualityError as exc:
        result["variants"] = [
            minimize_variant(
                {
                    **variant,
                    "call_quality": record,
                }
            )
            for variant, record in zip(
                indexed_variants,
                build_call_quality_records(indexed_variants),
            )
        ]
        result["variant_count"] = len(result["variants"])
        raise PipelineError(
            "Call quality must be acknowledged or overridden before analysis."
        ) from exc
    result["variants"] = [
        minimize_variant({**variant, "call_quality": record})
        for variant, record in zip(indexed_variants, call_quality_records)
    ]
    result["variant_count"] = len(result["variants"])
    try:
        result["variant_integrity_records"] = [
            dict(record)
            for record in build_variant_integrity_records(
                indexed_variants,
                cast(list[Mapping[str, object]], result["variants"]),
                assembly=settings.GENOME_ASSEMBLY,
            )
        ]
    except VariantIntegrityError as exc:
        raise PipelineError(
            "Parser and normalized variant identity did not match."
        ) from exc
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
        _notify_api_progress(
            result,
            source,
            cast(PipelineAPIStatus, status),
            progress_callback,
        )

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
        _notify_api_progress(
            result,
            "phen2gene",
            "skipped",
            progress_callback,
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
            _notify_api_progress(
                result,
                "phen2gene",
                "skipped",
                progress_callback,
            )
        else:
            _set_api_status(
                result,
                "phen2gene",
                "running",
                "Submitting the analysis HPO set once to Phen2Gene.",
            )
            _notify_api_progress(
                result,
                "phen2gene",
                "running",
                progress_callback,
            )
            try:
                phen2gene_result = enrich_with_phen2gene(
                    phenotype_results,
                    request["phenotypes"],
                    ontology_path=ontology_path,
                    associations_path=associations_path,
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
                phen2gene_api_status: PipelineAPIStatus = "error"
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
                if phen2gene_result["fallback_used"]:
                    api_status = "warning"
                    phenotype_status = "warning"
                    _record_issue(
                        result,
                        stage="phenotype",
                        code="phen2gene_fallback_used",
                        message=(
                            "Phen2Gene was unavailable; Local HPO-Gene "
                            "fallback provided direct-overlap context."
                        ),
                        recoverable=True,
                    )
                elif phen2gene_availability == "available":
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
                phen2gene_api_status = api_status
                phenotype_message = (
                    (
                        "Attached local HPO-Gene fallback context with "
                        "explicit degraded-mode provenance to "
                    )
                    if phen2gene_result["fallback_used"]
                    else "Attached local and Phen2Gene phenotype evidence to "
                ) + f"{len(phenotype_results)} variants."

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
            _notify_api_progress(
                result,
                "phen2gene",
                phen2gene_api_status,
                progress_callback,
            )

    _set_api_status(
        result,
        "mydisease",
        "running",
        "Retrieving bounded gene-disease-phenotype context.",
    )
    _notify_api_progress(
        result,
        "mydisease",
        "running",
        progress_callback,
    )
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
        _notify_api_progress(
            result,
            "mydisease",
            "error",
            progress_callback,
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
            if mydisease_result.get("local_degraded_count", 0):
                _record_issue(
                    result,
                    stage="phenotype",
                    code="mydisease_local_degraded",
                    message=(
                        "MyDisease.info was operationally unavailable; "
                        "bounded local patient HPO context was retained "
                        "without claiming a gene-disease association."
                    ),
                    recoverable=True,
                )
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
        _notify_api_progress(
            result,
            "mydisease",
            mydisease_api_status,
            progress_callback,
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
    """Build evidence, pre-review interpretations, and review drafts."""

    _ = report_dir
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
    preliminary_readiness = [
        build_evidence_readiness_audit(
            evidence,
            variant_index=index,
        )
        for index, evidence in enumerate(preliminary_evidence)
    ]

    def notify_enrichment_progress(
        step: int,
        total: int,
        provider: str,
        provider_status: str,
    ) -> None:
        completed = step if provider_status != "running" else step - 1
        result["progress_percent"] = 65 + int((completed / total) * 9)
        _set_stage(
            result,
            "evidence",
            "running",
            progress_percent=int((completed / total) * 100),
            message=(
                f"Variant enrichment {step} of {total}: "
                f"{provider} {provider_status}."
            ),
        )
        _notify_progress(result, progress_callback)

    conditional_result = enrich_conditionally(
        result["phenotype_results"],
        preliminary_evidence,
        population_session=population_session,
        literature_session=literature_session,
        readiness_audits=preliminary_readiness,
        progress_callback=notify_enrichment_progress,
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
    readiness_audits = [
        build_evidence_readiness_audit(
            evidence,
            variant_index=index,
            before_rescue=preliminary_readiness[index],
        )
        for index, evidence in enumerate(evidence_objects)
    ]
    if any(
        audit["readiness_after_rescue"]
        not in {"READY", "READY_WITH_LIMITATIONS"}
        for audit in readiness_audits
    ):
        raise PipelineError(
            "Evidence readiness did not establish minimum allele identity."
        )
    result["evidence_readiness"] = [
        dict(audit) for audit in readiness_audits
    ]
    _sync_variant_integrity_records(result)
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
        "running",
        progress_percent=0,
        message="Interpreting every variant before final review.",
    )
    _set_api_status(
        result,
        "llm",
        "running",
        "Variant interpretation started before final review.",
    )
    _notify_progress(result, progress_callback)

    def notify_interpretation_progress(
        index: int,
        total: int,
        interpretation_status: str,
    ) -> None:
        completed = (
            index
            if interpretation_status != "running"
            else index - 1
        )
        result["progress_percent"] = 75 + int((completed / total) * 13)
        message = (
            f"Variant interpretation {index} of {total} "
            f"{('started' if interpretation_status == 'running' else 'finished')}."
        )
        _set_stage(
            result,
            "llm",
            "running",
            progress_percent=int((completed / total) * 100),
            message=message,
        )
        _set_api_status(result, "llm", "running", message)
        _notify_progress(result, progress_callback)

    interpretation_results = interpret_variants(
        evidence_objects,
        model=llm_model,
        client=llm_client,
        readiness_audits=readiness_audits,
        progress_callback=notify_interpretation_progress,
    )
    result["variant_interpretation_results"] = [
        dict(item)
        for item in interpretation_results
    ]
    if (
        result["analysis_context"]["variant_interpretation_model"] is None
        and interpretation_results
    ):
        result["analysis_context"]["variant_interpretation_model"] = (
            interpretation_results[0]["configured_model"]
        )
    failed_interpretations = sum(
        item["status"] == "failed"
        for item in interpretation_results
    )
    model_warnings = sum(
        len(item["warnings"])
        for item in interpretation_results
        if item["status"] == "success"
    )
    for item in interpretation_results:
        _retain_warnings(result, item["warnings"])
    if failed_interpretations:
        llm_status: PipelineStageStatus = "warning"
        llm_message = (
            f"Interpretation completed with {failed_interpretations} "
            "explicit variant failure(s); collected evidence was preserved."
        )
        _append_warning(result, llm_message)
    elif model_warnings:
        llm_status = "warning"
        llm_message = (
            f"Interpreted {len(interpretation_results)} variant(s) with "
            f"{model_warnings} model warning(s)."
        )
    else:
        llm_status = "success"
        llm_message = (
            f"Interpreted {len(interpretation_results)} variant(s) before "
            "final review."
        )
    _set_stage(
        result,
        "llm",
        llm_status,
        progress_percent=100,
        message=llm_message,
    )
    _set_api_status(result, "llm", llm_status, llm_message)
    LOGGER.info(
        "event=variant_interpretations_generated result_count=%d "
        "failure_count=%d",
        len(interpretation_results),
        failed_interpretations,
    )

    result["current_stage"] = "report"
    result["progress_percent"] = 90
    _set_stage(
        result,
        "report",
        "running",
        progress_percent=0,
        message="Preparing evidence-and-interpretation review drafts.",
    )
    _notify_progress(result, progress_callback)
    draft_variant_reports = build_draft_variant_reports(
        list(evidence_objects),
        list(interpretation_results),
    )
    result["draft_variant_reports"] = [
        dict(report)
        for report in draft_variant_reports
    ]
    _sync_variant_report_records(result)
    _sync_variant_integrity_records(result)
    integrity_counts = cardinality_counts(
        result["variant_integrity_records"]
    )
    review_reports = build_evidence_review_reports(evidence_objects)
    result["evidence_review_reports"] = [
        dict(report)
        for report in review_reports
    ]
    LOGGER.info(
        "event=draft_variant_reports_prepared report_count=%d",
        len(draft_variant_reports),
    )
    LOGGER.info(
        "event=variant_integrity_gate_passed variant_count=%d",
        integrity_counts["pipeline"],
    )
    _set_stage(
        result,
        "report",
        "success",
        progress_percent=100,
        message=(
            f"Prepared {len(draft_variant_reports)} coherent Draft "
            "Variant Report(s)."
        ),
    )
    result["status"] = (
        "partial"
        if result["warnings"] or result["errors"]
        else "success"
    )
    result["workflow_state"] = "awaiting_final_review"
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
    call_quality_acknowledgements: Mapping[int, object] | None = None,
    call_quality_overrides: Mapping[int, object] | None = None,
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
    input_type: PersistedInputType | None = None,
    phenotype_extraction_model: str | None = None,
    phenotype_extraction_provenance: Mapping[str, object] | None = None,
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

    resolved_input_type: PersistedInputType = cast(
        PersistedInputType,
        input_type or request["input_mode"],
    )
    context: dict[str, object] = {
        "input_type": resolved_input_type,
        "accepted_hpo_terms": list(request["phenotypes"]),
        "phenotype_extraction_model": phenotype_extraction_model,
        "variant_interpretation_model": llm_model,
        "phenotype_extraction_provenance": (
            dict(phenotype_extraction_provenance)
            if phenotype_extraction_provenance is not None
            else None
        ),
    }
    try:
        result["analysis_context"] = validate_analysis_context(context)
    except PipelineResultError as exc:
        return _finish_exception(
            result,
            stage="input",
            error=PipelineInputError(str(exc)),
            default_code="invalid_input",
            default_message="The analysis context is invalid.",
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
            call_quality_acknowledgements=call_quality_acknowledgements,
            call_quality_overrides=call_quality_overrides,
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
    except VariantInterpretationError as exc:
        return _finish_exception(
            result,
            stage="llm",
            error=exc,
            default_code="variant_interpretation_failed",
            default_message=(
                "Variant interpretation could not be completed safely."
            ),
            default_recoverable=True,
            progress_callback=progress_callback,
        )
    except DraftVariantReportError as exc:
        return _finish_exception(
            result,
            stage="report",
            error=exc,
            default_code="draft_variant_report_failed",
            default_message=(
                "Draft Variant Reports could not be composed safely."
            ),
            default_recoverable=False,
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
    call_quality_acknowledgements: Mapping[int, object] | None = None,
    call_quality_overrides: Mapping[int, object] | None = None,
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
    input_type: PersistedInputType | None = None,
    phenotype_extraction_model: str | None = None,
    phenotype_extraction_provenance: Mapping[str, object] | None = None,
    report_dir: str | Path | None = None,
    database_path: str | Path | None = None,
    persist_analysis: bool = True,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Run analysis through interpretation, then pause for final review."""

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
            call_quality_acknowledgements=call_quality_acknowledgements,
            call_quality_overrides=call_quality_overrides,
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
            input_type=input_type,
            phenotype_extraction_model=phenotype_extraction_model,
            phenotype_extraction_provenance=(
                phenotype_extraction_provenance
            ),
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
            or validate_evidence_object(package["original_machine_report"])
            != validate_evidence_object(working["evidence_objects"][index])
        ):
            raise PipelineError(
                "Confirmed evidence does not match this analysis."
            )
        if index < len(working["variants"]):
            source_quality = working["variants"][index].get("call_quality")
            if source_quality is not None:
                try:
                    quality = validate_call_quality_record(source_quality)
                except CallQualityError as exc:
                    raise PipelineError(
                        "Call-quality confirmation state is invalid."
                    ) from exc
                if (
                    quality["status"] == "not_evaluated"
                    and quality["acknowledged_at"] is None
                ) or (
                    quality["status"] == "failed" and quality["override"] is None
                ):
                    raise PipelineError(
                        "Call quality requires acknowledgement or an override before confirmation."
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
        working["final_clinical_report"] = None
        working["workflow_state"] = (
            "awaiting_final_review"
            if working["variant_interpretation_results"]
            else "awaiting_confirmation"
        )
        _sync_variant_report_records(working)
    return validate_pipeline_result(working)


def finalize_reviewed_analysis(
    result: PipelineResult,
    reports: Sequence[Mapping[str, object]] | None = None,
    *,
    timestamp: str | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Finalize complete review state without making another LLM call."""

    working = validate_pipeline_result(deepcopy(result))
    if working["workflow_state"] != "awaiting_final_review":
        raise PipelineError(
            "Finalization requires an analysis awaiting final review."
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
            "Finalization requires confirmed review state for every variant."
        )
    if len(working["variant_interpretation_results"]) != working[
        "variant_count"
    ]:
        raise PipelineError(
            "Finalization requires one pre-review interpretation result "
            "for every variant."
        )

    working["workflow_state"] = "finalization_running"
    working["status"] = "running"
    working["current_stage"] = "report"
    working["progress_percent"] = 95
    _set_stage(
        working,
        "report",
        "running",
        progress_percent=95,
        message="Validating the complete final review state.",
    )
    _notify_progress(working, progress_callback)

    working["llm_routing_results"] = []
    working["final_interpretation_report"] = None
    working["final_clinical_report"] = None
    failed = sum(
        item["status"] == "failed"
        for item in working["variant_interpretation_results"]
    )
    selected_count = sum(
        bool(record["report_data"]["review_state"]["include_in_final_report"])
        for record in working["variant_report_records"]
    )
    message = (
        "Final review confirmed with "
        f"{failed} explicit interpretation failure(s)."
        if failed
        else "Final review confirmed without an additional LLM call."
    )
    message += (
        f" Final Report selection: {selected_count} of "
        f"{working['variant_count']} in original input order."
    )
    _set_stage(
        working,
        "report",
        "warning" if failed else "success",
        progress_percent=100,
        message=message,
    )
    if failed:
        _append_warning(working, message)
    working["status"] = (
        "partial"
        if failed or working["warnings"] or working["errors"]
        else "success"
    )
    working["workflow_state"] = "completed"
    working["current_stage"] = "completed"
    working["progress_percent"] = 100
    try:
        working["final_clinical_report"] = dict(
            compose_final_clinical_report(
                working,
                timestamp=timestamp,
            )
        )
    except FinalClinicalReportError as exc:
        raise PipelineError(
            "Final Clinical Report could not be composed from the "
            "reviewer-approved state."
        ) from exc
    _sync_variant_report_records(
        working,
        finalized_at=cast(
            str,
            working["final_clinical_report"]["generated_at"],
        ),
    )
    validated = validate_pipeline_result(working)
    _notify_progress(validated, progress_callback)
    return validated


def update_draft_variant_report(
    result: PipelineResult,
    report: Mapping[str, object],
) -> PipelineResult:
    """Persist one report decision and invalidate confirmation when required."""

    working = validate_pipeline_result(deepcopy(result))
    if not isinstance(report, Mapping):
        raise PipelineError("Draft Variant Report must be a mapping.")
    index = report.get("variant_index")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= working["variant_count"]
    ):
        raise PipelineError("Draft Variant Report index is invalid.")
    try:
        validated_report = validate_draft_variant_report(
            dict(report),
            evidence=working["evidence_objects"][index],
            interpretation=working["variant_interpretation_results"][
                index
            ],
        )
    except DraftVariantReportError as exc:
        raise PipelineError(str(exc)) from exc
    previous_report = working["draft_variant_reports"][index]
    previous_edits = previous_report["edit_history"]
    previous_selections = previous_report["selection_history"]
    if (
        validated_report["edit_history"][: len(previous_edits)]
        != previous_edits
        or validated_report["selection_history"][: len(previous_selections)]
        != previous_selections
    ):
        raise PipelineError(
            "Draft Variant Report history must remain append-only."
        )
    selection_changed = (
        previous_report["include_in_final_report"]
        != validated_report["include_in_final_report"]
        or previous_selections != validated_report["selection_history"]
    )
    reviewed_content_changed = (
        previous_report["reviewed_report"]
        != validated_report["reviewed_report"]
        or previous_report["edit_history"]
        != validated_report["edit_history"]
    )
    confirmation_invalidated = selection_changed or (
        validated_report["include_in_final_report"]
        and reviewed_content_changed
    )
    working["draft_variant_reports"][index] = dict(validated_report)
    if confirmation_invalidated:
        working["reviewed_evidence_packages"] = [
            package
            for package in working["reviewed_evidence_packages"]
            if package["variant_index"] != index
        ]
        working["llm_routing_results"] = [
            item
            for item in working["llm_routing_results"]
            if item["variant_index"] != index
        ]
        working["final_interpretation_report"] = None
        working["final_clinical_report"] = None
        working["workflow_state"] = "awaiting_final_review"
        working["current_stage"] = "completed"
        working["progress_percent"] = 100
    _sync_variant_report_records(working)
    LOGGER.info(
        "event=draft_variant_report_updated variant_index=%d "
        "edit_count=%d selection_count=%d confirmation_invalidated=%s",
        index,
        len(validated_report["edit_history"]),
        len(validated_report["selection_history"]),
        str(confirmation_invalidated).lower(),
    )
    return validate_pipeline_result(working)


def retry_failed_variant_interpretation(
    result: PipelineResult,
    *,
    variant_index: int,
    model: str | None = None,
    client: LLMClient | None = None,
    fallback_model: str | None = None,
    fallback_client: LLMClient | None = None,
    max_retries: int | None = None,
    timestamp: str | None = None,
) -> PipelineResult:
    """Retry one failed draft interpretation without rerunning providers."""

    working = validate_pipeline_result(deepcopy(result))
    if (
        isinstance(variant_index, bool)
        or not isinstance(variant_index, int)
        or variant_index < 0
        or variant_index >= working["variant_count"]
    ):
        raise PipelineError("Interpretation retry variant index is invalid.")
    prior = working["variant_interpretation_results"][variant_index]
    if prior["status"] != "failed":
        raise PipelineError("Only failed interpretations can be retried.")
    report = working["draft_variant_reports"][variant_index]
    if (
        report["edit_history"]
        or report["selection_history"]
        or report["review_status"] != "draft"
        or any(
            package["variant_index"] == variant_index
            for package in working["reviewed_evidence_packages"]
        )
    ):
        raise PipelineError(
            "Interpretation retry cannot overwrite existing reviewer decisions."
        )
    try:
        retried = retry_variant_interpretation(
            working["evidence_objects"][variant_index],
            prior,
            model=model,
            client=client,
            fallback_model=fallback_model,
            fallback_client=fallback_client,
            max_retries=max_retries,
            timestamp=timestamp,
            readiness_audit=working["evidence_readiness"][variant_index],
        )
        rebuilt = build_draft_variant_report(
            working["evidence_objects"][variant_index],
            retried,
            variant_index=variant_index,
        )
    except (VariantInterpretationError, DraftVariantReportError) as exc:
        raise PipelineError("Interpretation retry could not be completed.") from exc

    working["variant_interpretation_results"][variant_index] = dict(retried)
    working["draft_variant_reports"][variant_index] = dict(rebuilt)
    working["reviewed_evidence_packages"] = [
        package
        for package in working["reviewed_evidence_packages"]
        if package["variant_index"] != variant_index
    ]
    working["llm_routing_results"] = []
    working["final_interpretation_report"] = None
    working["final_clinical_report"] = None
    working["warnings"] = [
        warning
        for warning in working["warnings"]
        if not (
            (
                warning.startswith("Interpretation completed with ")
                and warning.endswith(
                    "explicit variant failure(s); collected evidence was preserved."
                )
            )
            or (
                warning.startswith("Interpretation retry completed with ")
                and warning.endswith(
                    "failed variant interpretation(s); collected evidence was preserved."
                )
            )
        )
    ]
    failed_count = sum(
        item["status"] == "failed"
        for item in working["variant_interpretation_results"]
    )
    if failed_count:
        message = (
            f"Interpretation retry completed with {failed_count} failed "
            "variant interpretation(s); collected evidence was preserved."
        )
        _append_warning(working, message)
        stage_status: PipelineStageStatus = "warning"
    else:
        message = (
            "Interpretation retry succeeded using the persisted Evidence Object; "
            "upstream providers were not rerun."
        )
        stage_status = "success"
    _set_stage(
        working,
        "llm",
        stage_status,
        progress_percent=100,
        message=message,
    )
    _set_api_status(working, "llm", stage_status, message)
    working["workflow_state"] = "awaiting_final_review"
    working["current_stage"] = "completed"
    working["progress_percent"] = 100
    working["status"] = (
        "partial"
        if failed_count or working["warnings"] or working["errors"]
        else "success"
    )
    _sync_variant_report_records(working)
    if working["variant_integrity_records"]:
        _sync_variant_integrity_records(working)
    return validate_pipeline_result(working)


def get_selected_draft_variant_reports(
    result: PipelineResult,
) -> list[dict[str, object]]:
    """Project included reports in immutable original variant order."""

    working = validate_pipeline_result(deepcopy(result))
    selected_indexes = [
        record["variant_index"]
        for record in working["variant_report_records"]
        if record["report_data"]["review_state"]["include_in_final_report"]
    ]
    return [
        dict(working["draft_variant_reports"][index])
        for index in selected_indexes
    ]


def generate_confirmed_interpretations(
    result: PipelineResult,
    *,
    light_client: LLMClient | None = None,
    strong_client: LLMClient | None = None,
    light_model: str | None = None,
    strong_model: str | None = None,
    timestamp: str | None = None,
    progress_callback: RoutingProgressCallback | None = None,
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
            progress_callback=progress_callback,
        )
    except (EvidenceConfirmationError, Stage35RoutingError) as exc:
        raise PipelineError("Stage 35 routing could not start.") from exc
    working["llm_routing_results"] = [
        dict(item) for item in routing_results
    ]
    working["final_interpretation_report"] = None
    working["final_clinical_report"] = None
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


def retry_failed_interpretations(
    result: PipelineResult,
    *,
    light_client: LLMClient | None = None,
    strong_client: LLMClient | None = None,
    light_model: str | None = None,
    strong_model: str | None = None,
    timestamp: str | None = None,
) -> PipelineResult:
    """Retry only failed LLM results while preserving confirmed evidence."""

    working = validate_pipeline_result(deepcopy(result))
    if working["workflow_state"] != "completed":
        raise PipelineError("LLM retry requires a completed Output B.")
    failed_indexes = {
        item["variant_index"]
        for item in working["llm_routing_results"]
        if item["status"] == "failed"
    }
    if not failed_indexes:
        raise PipelineError("Output B has no failed interpretations to retry.")
    packages = [
        package
        for package in working["reviewed_evidence_packages"]
        if package["variant_index"] in failed_indexes
    ]
    if {package["variant_index"] for package in packages} != failed_indexes:
        raise PipelineError(
            "Failed interpretations do not match the confirmed evidence."
        )

    try:
        retried = route_reviewed_evidence_packages(
            packages,
            light_client=light_client,
            strong_client=strong_client,
            light_model=light_model,
            strong_model=strong_model,
            timestamp=timestamp,
        )
    except (EvidenceConfirmationError, Stage35RoutingError) as exc:
        raise PipelineError("Failed interpretations could not be retried.") from exc

    merged = {
        item["variant_index"]: dict(item)
        for item in working["llm_routing_results"]
    }
    for item in retried:
        merged[item["variant_index"]] = dict(item)
        _retain_warnings(working, item["warnings"])
    working["llm_routing_results"] = [
        merged[index] for index in sorted(merged)
    ]
    remaining = sum(
        item["status"] == "failed"
        for item in working["llm_routing_results"]
    )
    message = (
        f"LLM retry completed with {remaining} failed interpretation(s); "
        "confirmed evidence was preserved."
        if remaining
        else "LLM retry recovered all failed interpretations."
    )
    _set_stage(
        working,
        "llm",
        "warning" if remaining else "success",
        progress_percent=100,
        message=message,
    )
    _set_api_status(
        working,
        "llm",
        "warning" if remaining else "success",
        message,
    )
    if remaining:
        _append_warning(working, message)
    working["final_interpretation_report"] = None
    working["final_clinical_report"] = None
    working["workflow_state"] = "phase_b_running"
    return generate_final_interpretation_report(working)


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

    def notify_routing_progress(
        index: int,
        total: int,
        route: str,
        route_status: str,
    ) -> None:
        completed = index if route_status != "running" else index - 1
        overall_progress = 50 + int((completed / total) * 34)
        role = "strong" if route == "llm_2" else "low-cost"
        message = (
            f"Variant {index} of {total}: {role} LLM request "
            f"{('started' if route_status == 'running' else 'finished')}."
        )
        working["progress_percent"] = overall_progress
        _set_stage(
            working,
            "llm",
            "running",
            progress_percent=int((completed / total) * 100),
            message=message,
        )
        _set_api_status(working, "llm", "running", message)
        _notify_progress(working, progress_callback)

    working = generate_confirmed_interpretations(
        working,
        light_client=light_client,
        strong_client=strong_client,
        light_model=light_model,
        strong_model=strong_model,
        timestamp=timestamp,
        progress_callback=notify_routing_progress,
    )
    working["progress_percent"] = 85
    _notify_progress(working, progress_callback)
    working = generate_final_interpretation_report(working)
    _notify_progress(working, progress_callback)
    return working


def resume_saved_analysis(
    analysis_id: str,
    reports: Sequence[Mapping[str, object]] | None = None,
    *,
    database_path: str | Path | None = None,
    light_client: LLMClient | None = None,
    strong_client: LLMClient | None = None,
    light_model: str | None = None,
    strong_model: str | None = None,
    timestamp: str | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Load one Draft, run Phase B, and persist the Confirmed result."""

    saved = load_pipeline_state(analysis_id, database_path=database_path)
    completed = resume_confirmed_analysis(
        saved,
        reports,
        light_client=light_client,
        strong_client=strong_client,
        light_model=light_model,
        strong_model=strong_model,
        timestamp=timestamp,
        progress_callback=progress_callback,
    )
    save_pipeline_state(completed, database_path=database_path)
    return completed


__all__ = [
    "AnalysisContext",
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
    "PersistedInputType",
    "confirm_reviewed_evidence",
    "finalize_reviewed_analysis",
    "generate_confirmed_interpretations",
    "generate_final_interpretation_report",
    "get_selected_draft_variant_reports",
    "resume_confirmed_analysis",
    "resume_saved_analysis",
    "retry_failed_variant_interpretation",
    "create_pipeline_result",
    "run_analysis",
    "update_draft_variant_report",
    "run_annotation_and_phenotype",
    "run_variant_processing",
    "validate_analysis_input",
    "validate_analysis_context",
    "validate_pipeline_result",
]
