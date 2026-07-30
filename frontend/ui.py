"""Presentation-only Streamlit interface for the analysis pipeline."""

from pathlib import Path

import streamlit as st

from backend.phenotype import (
    HPODataError,
    PhenotypeError,
    search_hpo_terms,
    update_hpo_data,
)
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
VCF_INPUT_MODE = "VCF upload"
MANUAL_INPUT_MODE = "Manual variant"
SELECTED_HPO_KEY = "selected_hpo_terms"
HPO_RESULTS_KEY = "hpo_search_results"
PREPARED_INPUT_KEY = "prepared_analysis_input"


def _initialize_session_state() -> None:
    """Initialize frontend-only state in one place."""

    st.session_state.setdefault(SELECTED_HPO_KEY, [])
    st.session_state.setdefault(HPO_RESULTS_KEY, [])
    st.session_state.setdefault(PREPARED_INPUT_KEY, None)


def _clear_prepared_input() -> None:
    """Discard a prepared summary when its source inputs change."""

    st.session_state[PREPARED_INPUT_KEY] = None


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


def _render_hpo_update_control() -> None:
    """Expose the existing coordinated HPO refresh operation."""

    if not st.button(
        "Update HPO data",
        key="update_hpo_data",
        icon=":material/refresh:",
        help=(
            "Download and validate the latest ontology, gene "
            "associations, and disease annotations."
        ),
        width="stretch",
    ):
        return

    try:
        with st.spinner("Updating HPO datasets..."):
            result = update_hpo_data()
    except (HPODataError, PhenotypeError, OSError, RuntimeError) as exc:
        st.error(f"HPO update failed: {exc}")
        return

    st.session_state[HPO_RESULTS_KEY] = []
    _clear_prepared_input()
    st.success(
        "HPO data updated to "
        f"{result['current_version']} "
        f"({result['active_term_count']:,} active terms)."
    )


def _render_hpo_picker() -> None:
    """Render local HPO search and selected-phenotype controls."""

    with st.container(border=True):
        st.subheader("Phenotypes")
        st.caption(
            "Search the locally installed Human Phenotype Ontology "
            "by term, synonym, or HPO ID."
        )

        with st.form("hpo_search_form", border=False):
            query = st.text_input(
                "Search HPO terms",
                key="hpo_search_query",
                max_chars=200,
                placeholder="e.g. seizure or HP:0001250",
            )
            search_submitted = st.form_submit_button(
                "Search",
                icon=":material/search:",
            )

        if search_submitted:
            try:
                st.session_state[HPO_RESULTS_KEY] = search_hpo_terms(
                    query,
                    limit=10,
                )
            except (PhenotypeError, HPODataError) as exc:
                st.session_state[HPO_RESULTS_KEY] = []
                st.error(str(exc))

        suggestions = st.session_state[HPO_RESULTS_KEY]
        if search_submitted and not suggestions:
            st.info("No matching HPO terms were found.")

        if suggestions:
            choice = st.selectbox(
                "Search results",
                suggestions,
                index=None,
                key="hpo_search_choice",
                format_func=lambda term: (
                    f"{term['id']} — {term['name']}"
                ),
                placeholder="Choose a phenotype",
            )
            if st.button(
                "Add phenotype",
                key="add_hpo_term",
                icon=":material/add:",
                disabled=choice is None,
            ):
                selected = st.session_state[SELECTED_HPO_KEY]
                if choice is not None and not any(
                    term["id"] == choice["id"] for term in selected
                ):
                    selected.append(
                        {"id": choice["id"], "name": choice["name"]}
                    )
                    _clear_prepared_input()
                elif choice is not None:
                    st.info(f"{choice['id']} is already selected.")

        selected_terms = st.session_state[SELECTED_HPO_KEY]
        st.markdown("**Selected phenotypes**")
        if not selected_terms:
            st.caption("No phenotypes selected. This input is optional.")
            return

        for term in selected_terms:
            with st.container(
                horizontal=True,
                vertical_alignment="center",
                gap="small",
            ):
                st.write(f"**{term['id']}** — {term['name']}")
                if st.button(
                    "Remove",
                    key=f"remove_{term['id']}",
                    icon=":material/delete:",
                    help=f"Remove {term['id']}",
                ):
                    st.session_state[SELECTED_HPO_KEY] = [
                        selected
                        for selected in selected_terms
                        if selected["id"] != term["id"]
                    ]
                    _clear_prepared_input()
                    st.rerun()


def _is_supported_vcf_filename(filename: str) -> bool:
    """Return whether an uploaded filename is VCF-shaped."""

    lowered_name = filename.casefold()
    return lowered_name.endswith(".vcf") or lowered_name.endswith(
        ".vcf.gz"
    )


def _prepare_input(
    input_mode: str,
    uploaded_vcf: object | None,
    manual_variant: str,
) -> None:
    """Validate UI presence rules and store a safe input summary."""

    selected_phenotypes = [
        dict(term) for term in st.session_state[SELECTED_HPO_KEY]
    ]
    if input_mode == VCF_INPUT_MODE:
        if uploaded_vcf is None:
            st.error("Upload a .vcf or .vcf.gz file before analysis.")
            _clear_prepared_input()
            return
        filename = str(getattr(uploaded_vcf, "name", ""))
        if not _is_supported_vcf_filename(filename):
            st.error("The uploaded file must end in .vcf or .vcf.gz.")
            _clear_prepared_input()
            return
        source_summary = filename
    else:
        source_summary = manual_variant.strip()
        if not source_summary:
            st.error(
                "Enter a variant in CHROM:POS:REF:ALT format "
                "before analysis."
            )
            _clear_prepared_input()
            return

    st.session_state[PREPARED_INPUT_KEY] = {
        "input_mode": input_mode,
        "source": source_summary,
        "phenotypes": selected_phenotypes,
    }


def _render_variant_input() -> None:
    """Render source selection and the batched analysis submission form."""

    with st.container(border=True):
        st.subheader("Variant input")
        input_mode = st.segmented_control(
            "Input source",
            (VCF_INPUT_MODE, MANUAL_INPUT_MODE),
            default=VCF_INPUT_MODE,
            key="variant_input_mode",
            on_change=_clear_prepared_input,
            width="stretch",
        )

        with st.form("analysis_input_form", border=False):
            uploaded_vcf = None
            manual_variant = ""
            if input_mode == VCF_INPUT_MODE:
                uploaded_vcf = st.file_uploader(
                    "VCF file",
                    type=("vcf", "gz"),
                    key="vcf_upload",
                    help="Accepted formats: .vcf and .vcf.gz.",
                )
            else:
                manual_variant = st.text_input(
                    "Variant",
                    key="manual_variant",
                    placeholder="1:941284:G:A",
                    help="Use CHROM:POS:REF:ALT format.",
                )

            submitted = st.form_submit_button(
                "Analyze variant",
                type="primary",
                icon=":material/biotech:",
                width="stretch",
            )

        if submitted:
            _prepare_input(
                input_mode or VCF_INPUT_MODE,
                uploaded_vcf,
                manual_variant,
            )

        prepared_input = st.session_state[PREPARED_INPUT_KEY]
        if prepared_input is not None:
            phenotype_count = len(prepared_input["phenotypes"])
            st.success(
                "Input package prepared: "
                f"{prepared_input['source']} with "
                f"{phenotype_count} phenotype"
                f"{'' if phenotype_count == 1 else 's'}. "
                "Pipeline execution will be connected in Stage 11 step 3."
            )


def render_app() -> None:
    """Render the Stage 11 application shell and input controls."""

    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=":material/genetics:",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _initialize_session_state()
    _load_styles()

    st.title(PAGE_TITLE)
    st.caption(PAGE_SUBTITLE)
    st.warning(DECISION_SUPPORT_NOTICE)

    with st.sidebar:
        st.subheader("Application")
        st.write(settings.APP_NAME)
        st.caption(f"Genome assembly: {settings.GENOME_ASSEMBLY}")
        st.caption("Pipeline: ready")
        st.divider()
        _render_hpo_update_control()

    st.subheader("Analysis workflow")
    _render_workflow_overview()
    st.divider()
    _render_variant_input()
    _render_hpo_picker()
