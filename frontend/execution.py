"""Safe bridge between Streamlit inputs and the public pipeline."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from backend.pipeline import (
    PipelineProgressCallback,
    PipelineResult,
    run_analysis,
)
from config import settings


class FrontendExecutionError(RuntimeError):
    """Raised when a frontend input cannot be prepared safely."""


class UploadedVCF(Protocol):
    """Minimal uploaded-file contract required by the execution bridge."""

    name: str

    def getvalue(self) -> bytes:
        """Return the uploaded file contents."""


def execute_analysis(
    *,
    uploaded_vcf: UploadedVCF | None,
    manual_variant: str | None,
    phenotypes: list[str],
    progress_callback: PipelineProgressCallback | None = None,
) -> PipelineResult:
    """Execute one manual or temporary-upload analysis request."""

    if uploaded_vcf is not None and manual_variant is not None:
        raise FrontendExecutionError(
            "Choose either a VCF upload or a manual variant."
        )

    if uploaded_vcf is None:
        return run_analysis(
            vcf_path=None,
            manual_variant=manual_variant,
            phenotypes=phenotypes,
            progress_callback=progress_callback,
        )

    lowered_name = uploaded_vcf.name.casefold()
    suffix = ".vcf.gz" if lowered_name.endswith(".vcf.gz") else ".vcf"
    try:
        settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="analysis-",
            dir=settings.UPLOAD_DIR,
        ) as temporary_directory:
            temporary_path = Path(temporary_directory) / f"input{suffix}"
            temporary_path.write_bytes(uploaded_vcf.getvalue())
            return run_analysis(
                vcf_path=temporary_path,
                manual_variant=None,
                phenotypes=phenotypes,
                progress_callback=progress_callback,
            )
    except OSError as exc:
        raise FrontendExecutionError(
            "The uploaded VCF could not be prepared for analysis."
        ) from exc


__all__ = [
    "FrontendExecutionError",
    "UploadedVCF",
    "execute_analysis",
]
