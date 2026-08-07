"""Streamlit viewer and download for Stage 36 Output B."""

from __future__ import annotations

from typing import cast

import streamlit as st

from backend.final_interpretation_report import (
    FAILURE_LABELS,
    FINAL_INTERPRETATION_REPORT_FILENAME,
    FinalInterpretationReportError,
    render_final_interpretation_report_text,
    validate_final_interpretation_report,
)
from backend.pipeline import PipelineResult


def render_final_interpretation_output(result: PipelineResult) -> None:
    """Render Output B only when Stage 36 has produced it."""

    report = result.get("final_interpretation_report")
    if report is None:
        return
    try:
        validated = validate_final_interpretation_report(report)
        text = render_final_interpretation_report_text(validated)
    except FinalInterpretationReportError as exc:
        st.error(f"Output B is unavailable: {exc}")
        return

    st.subheader("Output B — Final interpretation only")
    st.download_button(
        "Download Output B",
        data=text.encode("utf-8"),
        file_name=FINAL_INTERPRETATION_REPORT_FILENAME,
        mime="text/plain",
        key="download_final_interpretation_report",
        icon=":material/download:",
        on_click="ignore",
    )
    for entry in validated["entries"]:
        with st.container(border=True):
            st.markdown(f"#### Variant {entry['variant_index'] + 1}")
            if entry["status"] == "success":
                st.text(cast(str, entry["final_interpretation"]))
            else:
                st.error(
                    FAILURE_LABELS[cast(str, entry["failure_status"])]
                )


__all__ = ["render_final_interpretation_output"]
