"""Secure Streamlit viewer for generated clinical report files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from backend.pipeline import PipelineResult
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


__all__ = [
    "ReportDocument",
    "ReportViewerError",
    "load_report_document",
    "render_report_viewer",
]
