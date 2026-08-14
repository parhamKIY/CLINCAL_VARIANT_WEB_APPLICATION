"""Secure Streamlit viewer for generated clinical report files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from backend.pipeline import PipelineResult
from backend.final_clinical_report import (
    FinalClinicalReportError,
    render_final_clinical_report_markdown,
    validate_final_clinical_report,
)
from backend.final_docx_package import (
    FinalDocxPackageError,
    build_final_docx_package,
)
from backend.report import MAX_CLINICAL_REPORT_TEXT_BYTES
from backend.report_exports import (
    ReportExportError,
    render_report_docx,
    render_report_pdf,
)
from config import settings


class ReportViewerError(RuntimeError):
    """Raised when a report cannot be displayed safely."""


@dataclass(frozen=True, slots=True)
class ReportDocument:
    """Bounded report contents prepared for viewing and download."""

    filename: str
    text: str
    data: bytes


def load_report_document(
    report_path: str | Path,
    *,
    report_dir: str | Path | None = None,
) -> ReportDocument:
    """Load one UTF-8 text report confined to the report directory."""

    allowed_directory = (
        Path(report_dir)
        if report_dir is not None
        else settings.REPORT_DIR
    ).resolve()
    try:
        resolved_path = Path(report_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReportViewerError(
            "The generated report file is no longer available."
        ) from exc

    if (
        not resolved_path.is_relative_to(allowed_directory)
        or not resolved_path.is_file()
        or resolved_path.suffix.casefold() != ".txt"
    ):
        raise ReportViewerError(
            "The generated report path is not an approved text file."
        )

    try:
        report_data = resolved_path.read_bytes()
    except OSError as exc:
        raise ReportViewerError(
            "The generated report could not be read."
        ) from exc

    if not report_data:
        raise ReportViewerError("The generated report is empty.")
    if len(report_data) > MAX_CLINICAL_REPORT_TEXT_BYTES:
        raise ReportViewerError(
            "The generated report exceeds the display size limit."
        )
    try:
        report_text = report_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportViewerError(
            "The generated report is not valid UTF-8 text."
        ) from exc
    report_text = (
        report_text.replace("\r\n", "\n").replace("\r", "\n")
    )

    return ReportDocument(
        filename=resolved_path.name,
        text=report_text,
        data=report_data,
    )


def render_report_viewer(result: PipelineResult) -> None:
    """Render and offer the generated report without exposing its path."""

    st.subheader("Clinical report")
    report_path = result["report_path"]
    if report_path is None:
        st.info(
            "No clinical report was generated for this analysis."
        )
        return

    try:
        report = load_report_document(report_path)
    except ReportViewerError as exc:
        st.error(str(exc))
        return

    with st.container(border=True):
        st.caption(f"Generated report: {report.filename}")
        try:
            pdf_data = render_report_pdf(report.text)
            docx_data = render_report_docx(report.text)
        except ReportExportError:
            pdf_data = None
            docx_data = None
            st.warning(
                "PDF and Word exports are temporarily unavailable. "
                "The text report remains available."
            )

        with st.container(
            horizontal=True,
            horizontal_alignment="left",
            gap="small",
        ):
            st.download_button(
                "Download text",
                data=report.data,
                file_name=report.filename,
                mime="text/plain",
                key="download_clinical_report_text",
                icon=":material/download:",
                on_click="ignore",
            )
            if pdf_data is not None:
                st.download_button(
                    "Download PDF",
                    data=pdf_data,
                    file_name=Path(report.filename).with_suffix(
                        ".pdf"
                    ).name,
                    mime="application/pdf",
                    key="download_clinical_report_pdf",
                    icon=":material/picture_as_pdf:",
                    type="primary",
                    on_click="ignore",
                )
            if docx_data is not None:
                st.download_button(
                    "Download Word",
                    data=docx_data,
                    file_name=Path(report.filename).with_suffix(
                        ".docx"
                    ).name,
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                    key="download_clinical_report_docx",
                    icon=":material/description:",
                    on_click="ignore",
                )
        st.divider()
        st.markdown(report.text)

    with st.expander(
        "Plain-text source",
        icon=":material/text_snippet:",
    ):
        st.code(
            report.text,
            language="text",
            wrap_lines=True,
        )


def render_final_clinical_report_viewer(result: PipelineResult) -> None:
    """Render the approved Stage 56 report with in-memory exports."""

    report_value = result.get("final_clinical_report")
    if report_value is None:
        return
    st.subheader("Final Clinical Report")
    try:
        report = validate_final_clinical_report(report_value)
        report_text = render_final_clinical_report_markdown(report)
    except FinalClinicalReportError as exc:
        st.error(f"The Final Clinical Report is invalid: {exc}")
        return
    filename_stem = str(result.get("analysis_id") or report["report_id"])
    filename_stem = f"{filename_stem}-final-clinical-report"
    with st.container(border=True):
        st.caption(
            "Reviewer-approved content only. The selected editable Word package "
            "is authoritative; export does not regenerate interpretation or make "
            "another LLM call."
        )
        st.caption(
            f"Finalization state: {report['metadata']['finalization_state']}"
        )
        selected_count = report["metadata"]["selected_variant_count"]
        package = None
        if selected_count:
            try:
                package = build_final_docx_package(result)
            except FinalDocxPackageError as exc:
                st.warning(f"The selected Word report package is unavailable: {exc}")
        try:
            pdf_data = render_report_pdf(
                report_text,
                title="Final Clinical Report",
            )
            docx_data = render_report_docx(
                report_text,
                title="Final Clinical Report",
            )
        except ReportExportError:
            pdf_data = None
            docx_data = None
            st.warning(
                "PDF and Word exports are temporarily unavailable. "
                "The text report remains available."
            )
        with st.container(
            horizontal=True,
            horizontal_alignment="left",
            gap="small",
        ):
            if package is not None:
                st.download_button(
                    "Download selected Word report package",
                    data=package["data"],
                    file_name=package["filename"],
                    mime="application/zip",
                    key="download_final_selected_docx_package",
                    icon=":material/folder_zip:",
                    type="primary",
                    on_click="ignore",
                )
            st.download_button(
                "Download final report text",
                data=report_text.encode("utf-8"),
                file_name=f"{filename_stem}.txt",
                mime="text/plain",
                key="download_final_clinical_report_text",
                icon=":material/download:",
                on_click="ignore",
            )
            if pdf_data is not None:
                st.download_button(
                    "Download final report PDF",
                    data=pdf_data,
                    file_name=f"{filename_stem}.pdf",
                    mime="application/pdf",
                    key="download_final_clinical_report_pdf",
                    icon=":material/picture_as_pdf:",
                    on_click="ignore",
                )
            if docx_data is not None:
                st.download_button(
                    "Download final report Word",
                    data=docx_data,
                    file_name=f"{filename_stem}.docx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                    key="download_final_clinical_report_docx",
                    icon=":material/description:",
                    on_click="ignore",
                )
        st.divider()
        st.markdown(report_text)


__all__ = [
    "ReportDocument",
    "ReportViewerError",
    "load_report_document",
    "render_final_clinical_report_viewer",
    "render_report_viewer",
]
