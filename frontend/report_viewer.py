"""Secure Streamlit viewer for generated clinical report files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from backend.pipeline import PipelineResult
from backend.report import MAX_CLINICAL_REPORT_MARKDOWN_BYTES
from config import settings


class ReportViewerError(RuntimeError):
    """Raised when a report cannot be displayed safely."""


@dataclass(frozen=True, slots=True)
class ReportDocument:
    """Bounded report contents prepared for viewing and download."""

    filename: str
    markdown: str
    data: bytes


def load_report_document(
    report_path: str | Path,
    *,
    report_dir: str | Path | None = None,
) -> ReportDocument:
    """Load one UTF-8 Markdown report confined to the report directory."""

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
        or resolved_path.suffix.casefold() != ".md"
    ):
        raise ReportViewerError(
            "The generated report path is not an approved Markdown file."
        )

    try:
        report_data = resolved_path.read_bytes()
    except OSError as exc:
        raise ReportViewerError(
            "The generated report could not be read."
        ) from exc

    if not report_data:
        raise ReportViewerError("The generated report is empty.")
    if len(report_data) > MAX_CLINICAL_REPORT_MARKDOWN_BYTES:
        raise ReportViewerError(
            "The generated report exceeds the display size limit."
        )
    try:
        markdown = report_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportViewerError(
            "The generated report is not valid UTF-8 text."
        ) from exc
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")

    return ReportDocument(
        filename=resolved_path.name,
        markdown=markdown,
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
        st.download_button(
            "Download Markdown report",
            data=report.data,
            file_name=report.filename,
            mime="text/markdown",
            key="download_clinical_report",
            icon=":material/download:",
            type="primary",
            on_click="ignore",
            width="stretch",
        )
        st.divider()
        st.markdown(report.markdown)

    with st.expander(
        "Markdown source",
        icon=":material/code:",
    ):
        st.code(
            report.markdown,
            language="markdown",
            wrap_lines=True,
        )


__all__ = [
    "ReportDocument",
    "ReportViewerError",
    "load_report_document",
    "render_report_viewer",
]
