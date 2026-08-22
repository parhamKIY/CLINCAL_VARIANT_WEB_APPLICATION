"""Central user-safe error mapping for backend and frontend boundaries."""

from __future__ import annotations

from typing import Literal, TypedDict

from backend.annotation import AnnotationError
from backend.llm import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
    LLMValidationError,
)
from backend.logging_config import get_logger
from backend.report import (
    ClinicalInterpretationError,
    ClinicalReportError,
    EvidenceObjectError,
)
from backend.vcf_processing import VCFProcessingError


LOGGER = get_logger("error_handling")
UIErrorContext = Literal[
    "hpo_update",
    "phenotype_search",
    "phenotype_extraction",
    "phenotype_acceptance",
    "manual_input",
    "excel_upload",
    "excel_selection",
    "analysis_start",
    "analysis_worker",
]


class PipelineError(RuntimeError):
    """Base error for the complete analysis pipeline."""


class PipelineInputError(PipelineError):
    """Raised when an analysis request violates its public contract."""


class PipelineResultError(PipelineError):
    """Raised when a frontend-safe pipeline result is malformed."""


class PublicError(TypedDict):
    """Bounded error fields permitted to cross the presentation boundary."""

    code: str
    message: str
    recoverable: bool


def map_pipeline_exception(
    error: Exception,
    *,
    default_code: str,
    default_message: str,
    default_recoverable: bool,
    stage: str,
) -> PublicError:
    """Map an internal exception to a stable user-safe pipeline error."""

    if isinstance(error, PipelineInputError):
        public_error: PublicError = {
            "code": "invalid_input",
            "message": (
                "The analysis input is incomplete or invalid. Review the "
                "selected input and phenotype terms, then try again."
            ),
            "recoverable": False,
        }
    elif isinstance(error, VCFProcessingError):
        public_error = {
            "code": "vcf_processing_failed",
            "message": (
                "The VCF could not be read or validated. Check the file "
                "format and try again."
            ),
            "recoverable": False,
        }
    elif isinstance(error, AnnotationError):
        public_error = {
            "code": "annotation_failed",
            "message": (
                "Variant annotation could not be completed. Verify the "
                "input and try again."
            ),
            "recoverable": True,
        }
    elif isinstance(error, EvidenceObjectError):
        public_error = {
            "code": "evidence_object_failed",
            "message": (
                "Evidence construction could not be completed for this "
                "variant."
            ),
            "recoverable": False,
        }
    elif isinstance(error, LLMAuthenticationError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "The LLM provider rejected the configured credentials "
                "or permissions."
            ),
            "recoverable": True,
        }
    elif isinstance(error, LLMQuotaError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "The LLM provider has insufficient quota or credit. Add "
                "provider credit or select another model."
            ),
            "recoverable": True,
        }
    elif isinstance(error, LLMRateLimitError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "The LLM provider rate limit was reached. Try again "
                "later."
            ),
            "recoverable": True,
        }
    elif isinstance(error, LLMTimeoutError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "Automated interpretation could not be completed. "
                "Technical details were recorded. Try again later."
            ),
            "recoverable": True,
        }
    elif isinstance(error, LLMRequestError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "Automated interpretation could not be completed. "
                "Technical details were recorded. Try again later."
            ),
            "recoverable": True,
        }
    elif isinstance(error, LLMConfigurationError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "The LLM provider is not configured correctly. Review "
                "the application settings."
            ),
            "recoverable": True,
        }
    elif isinstance(
        error,
        (
            LLMResponseError,
            LLMValidationError,
            ClinicalInterpretationError,
        ),
    ):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "Automated interpretation returned an unusable response. "
                "No interpretation was accepted. Technical details were "
                "recorded."
            ),
            "recoverable": True,
        }
    elif isinstance(error, LLMError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "Automated interpretation could not be completed. "
                "Technical details were recorded."
            ),
            "recoverable": True,
        }
    elif isinstance(error, ClinicalReportError):
        public_error = {
            "code": "report_generation_failed",
            "message": (
                "Report generation could not be completed. The available "
                "analysis results remain available for review."
            ),
            "recoverable": True,
        }
    else:
        public_error = {
            "code": default_code,
            "message": default_message,
            "recoverable": default_recoverable,
        }

    LOGGER.warning(
        "event=user_safe_error stage=%s code=%s recoverable=%s "
        "error_type=%s",
        stage,
        public_error["code"],
        public_error["recoverable"],
        type(error).__name__,
    )
    return public_error


def safe_ui_error_message(
    error: Exception,
    *,
    context: UIErrorContext,
) -> str:
    """Return a fixed UI message without exposing exception text."""

    if (
        context == "phenotype_extraction"
        and isinstance(error, LLMQuotaError)
    ):
        message = (
            "The LLM provider has insufficient quota or credit. Add "
            "provider credit or select another model. Manual HPO "
            "selection remains available."
        )
    else:
        messages = {
            "hpo_update": (
                "HPO data could not be updated. The previously installed "
                "data remains available."
            ),
            "phenotype_search": (
                "Phenotype search could not be completed. Verify the local "
                "HPO data and try again."
            ),
            "phenotype_extraction": (
                "Phenotype candidates could not be extracted safely. "
                "Manual HPO selection remains available."
            ),
            "phenotype_acceptance": (
                "The edited candidates could not be accepted. Correct or "
                "remove invalid HPO identifiers and try again."
            ),
            "manual_input": (
                "The manual variant input is incomplete or invalid. Review "
                "the highlighted rows and try again."
            ),
            "excel_upload": (
                "The Excel workbook could not be read safely. Check the "
                "file and try again."
            ),
            "excel_selection": (
                "The selected Excel rows could not be accepted. Select 1–10 "
                "valid source rows and try again."
            ),
            "analysis_start": (
                "The analysis could not be started. Technical details were "
                "recorded; try again."
            ),
            "analysis_worker": (
                "The analysis stopped unexpectedly. Technical details were "
                "recorded; retained results remain unchanged."
            ),
        }
        message = messages[context]
    LOGGER.warning(
        "event=user_safe_ui_error context=%s error_type=%s",
        context,
        type(error).__name__,
    )
    return message


__all__ = [
    "PipelineError",
    "PipelineInputError",
    "PipelineResultError",
    "PublicError",
    "UIErrorContext",
    "map_pipeline_exception",
    "safe_ui_error_message",
]
