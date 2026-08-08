"""Central user-safe error mapping for backend and frontend boundaries."""

from __future__ import annotations

from typing import Literal, TypedDict

from backend.annotation import AnnotationError
from backend.llm import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
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
            "message": str(error),
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
                "The available evidence could not be converted into the "
                "required clinical format."
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
            "message": "The LLM request timed out.",
            "recoverable": True,
        }
    elif isinstance(error, LLMRequestError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "The LLM provider is temporarily unavailable. Try again "
                "later."
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
                "The LLM response could not be safely validated. Try "
                "again."
            ),
            "recoverable": True,
        }
    elif isinstance(error, LLMError):
        public_error = {
            "code": "llm_interpretation_failed",
            "message": (
                "Clinical interpretation could not be completed. Try "
                "again later."
            ),
            "recoverable": True,
        }
    elif isinstance(error, ClinicalReportError):
        public_error = {
            "code": "report_generation_failed",
            "message": (
                "The clinical report could not be generated or saved."
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
            "Phenotype candidates could not be extracted safely. Manual "
            "HPO selection remains available."
        ),
        "phenotype_acceptance": (
            "The edited candidates could not be accepted. Correct or "
            "remove invalid HPO identifiers and try again."
        ),
    }
    LOGGER.warning(
        "event=user_safe_ui_error context=%s error_type=%s",
        context,
        type(error).__name__,
    )
    return messages[context]


__all__ = [
    "PipelineError",
    "PipelineInputError",
    "PipelineResultError",
    "PublicError",
    "UIErrorContext",
    "map_pipeline_exception",
    "safe_ui_error_message",
]
