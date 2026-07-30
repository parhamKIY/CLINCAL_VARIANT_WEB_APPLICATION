"""Presentation-only Streamlit interface for the analysis pipeline."""

from pathlib import Path

import streamlit as st

from config import settings


PAGE_TITLE = "Clinical Variant Interpretation"
PAGE_SUBTITLE = (
    "Evidence-centered variant annotation and phenotype correlation"
)
DECISION_SUPPORT_NOTICE = (
    "This application provides clinical decision support only. "
    "Its output is not a diagnosis or treatment recommendation and "
    "must be reviewed by a qualified healthcare professional."
)
STYLES_PATH = Path(__file__).with_name("styles.css")


def _load_styles() -> None:
    """Load local presentation styles without adding analysis logic."""

    try:
        css = STYLES_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    if css.strip():
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def _render_workflow_overview() -> None:
    """Show the backend stages the frontend will orchestrate."""

    stages = (
        "Input",
        "Prioritization",
        "Annotation",
        "Phenotype",
        "Evidence",
        "Report",
    )
    for row_start in range(0, len(stages), 3):
        columns = st.columns(3)
        for index, (column, stage) in enumerate(
            zip(
                columns,
                stages[row_start:row_start + 3],
                strict=True,
            ),
            start=row_start + 1,
        ):
            with column:
                st.markdown(
                    (
                        '<div class="workflow-step">'
                        f'<span class="workflow-number">{index}</span>'
                        f"<strong>{stage}</strong>"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )


def render_app() -> None:
    """Render the Stage 11 application shell."""

    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon="🧬",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _load_styles()

    st.title(PAGE_TITLE)
    st.caption(PAGE_SUBTITLE)
    st.warning(DECISION_SUPPORT_NOTICE)

    with st.sidebar:
        st.subheader("Application")
        st.write(settings.APP_NAME)
        st.caption(f"Genome assembly: {settings.GENOME_ASSEMBLY}")
        st.caption("Pipeline: ready")

    st.subheader("Analysis workflow")
    _render_workflow_overview()
    st.info(
        "Frontend foundation is ready. Variant and phenotype inputs "
        "will be connected in the next Stage 11 step."
    )
