"""Presentation-only Streamlit interface for the analysis pipeline."""

from pathlib import Path
from typing import TypedDict, cast

import streamlit as st

from backend.error_handling import safe_ui_error_message
from backend.pipeline import PipelineResult
from backend.phenotype import (
    HPODataError,
    PhenotypeError,
    search_hpo_terms,
    update_hpo_data,
)
from config import settings
from frontend.execution import (
    FrontendExecutionError,
    UploadedVCF,
    execute_analysis,
)
from frontend.results import render_analysis_results
from frontend.report_viewer import render_report_viewer


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
PIPELINE_RESULT_KEY = "pipeline_result"
LLM_MODEL_KEY = "selected_llm_model"
LLM_MODEL_CATALOG = (
    "gpt-5.4-mini",
    "gpt-5.4",
    "gemini-3.1-pro-preview",
    "claude-sonnet-4-6",
    "gemini-3.1-flash-lite",
    "gpt-5.4-nano",
)
LLM_MODEL_ADVANTAGES = {
    "gpt-5.4-mini": (
        "Recommended: best balance for conclusions, report quality, "
        "speed, and cost"
    ),
    "gpt-5.4": (
        "Best for difficult cases and deeper conclusions; higher cost"
    ),
    "gemini-3.1-pro-preview": (
        "High-quality comparison model for complex interpretation"
    ),
    "claude-sonnet-4-6": (
        "Strong professional report writing; higher cost"
    ),
    "gemini-3.1-flash-lite": (
        "Cheap and fast for draft reports"
    ),
    "gpt-5.4-nano": (
        "Lowest-cost option for basic testing; less detailed conclusions"
    ),
}
PIPELINE_STAGE_LABELS = {
    "input": "Input validation",
    "vcf_processing": "VCF processing",
    "prioritization": "Candidate prioritization",
    "annotation": "Variant annotation",
    "phenotype": "Phenotype matching",
    "evidence": "Evidence construction",
    "llm": "Clinical interpretation",
    "report": "Report generation",
}
PIPELINE_STATUS_ICONS = {
    "pending": ":material/schedule:",
    "running": ":material/sync:",
    "success": ":material/check_circle:",
    "warning": ":material/warning:",
    "error": ":material/error:",
    "skipped": ":material/skip_next:",
}


class AnalysisSubmission(TypedDict):
    """One validated frontend request ready for pipeline execution."""

    uploaded_vcf: UploadedVCF | None
    manual_variant: str | None
    phenotypes: list[str]
    llm_model: str


def _llm_model_options() -> tuple[str, ...]:
    """Return the configured default followed by approved UI models."""

    return tuple(
        dict.fromkeys((settings.LLM_MODEL, *LLM_MODEL_CATALOG))
    )


def _format_llm_model_option(model: str) -> str:
    """Add a concise use-case advantage to one model identifier."""

    advantage = LLM_MODEL_ADVANTAGES.get(
        model,
        "Custom model configured in .env",
    )
    return f"{model} — {advantage}"


def _initialize_session_state() -> None:
    """Initialize frontend-only state in one place."""

    st.session_state.setdefault(SELECTED_HPO_KEY, [])
    st.session_state.setdefault(HPO_RESULTS_KEY, [])
    st.session_state.setdefault(PIPELINE_RESULT_KEY, None)
    model_options = _llm_model_options()
    if st.session_state.get(LLM_MODEL_KEY) not in model_options:
        st.session_state[LLM_MODEL_KEY] = settings.LLM_MODEL


def _clear_analysis_result() -> None:
    """Discard an earlier result when analysis inputs change."""

    st.session_state[PIPELINE_RESULT_KEY] = None
    st.session_state.pop("selected_evidence_object", None)


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
        st.error(safe_ui_error_message(exc, context="hpo_update"))
        return

    st.session_state[HPO_RESULTS_KEY] = []
    _clear_analysis_result()
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
                st.error(
                    safe_ui_error_message(
                        exc,
                        context="phenotype_search",
                    )
                )

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
                    _clear_analysis_result()
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
                    _clear_analysis_result()
                    st.rerun()


def _render_llm_model_selector() -> str:
    """Render the per-analysis LLM model selection."""

    with st.container(border=True):
        st.subheader("Interpretation model")
        st.caption(
            "Choose the AvalAI chat model used for the clinical "
            "interpretation and final report. The selection applies "
            "to new analyses."
        )
        selected_model = st.selectbox(
            "LLM model",
            _llm_model_options(),
            key=LLM_MODEL_KEY,
            format_func=_format_llm_model_option,
            help=(
                "More capable models may produce stronger summaries but "
                "usually cost more. Every result still requires review "
                "by a qualified healthcare professional."
            ),
            on_change=_clear_analysis_result,
        )
        st.caption(
            "Selected advantage: "
            f"{LLM_MODEL_ADVANTAGES.get(selected_model, 'Configured default')}."
        )
    return selected_model


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
    llm_model: str,
) -> AnalysisSubmission | None:
    """Validate frontend presence rules and build one submission."""

    phenotype_ids = [
        term["id"] for term in st.session_state[SELECTED_HPO_KEY]
    ]
    if input_mode == VCF_INPUT_MODE:
        if uploaded_vcf is None:
            st.error("Upload a .vcf or .vcf.gz file before analysis.")
            _clear_analysis_result()
            return None
        filename = str(getattr(uploaded_vcf, "name", ""))
        if not _is_supported_vcf_filename(filename):
            st.error("The uploaded file must end in .vcf or .vcf.gz.")
            _clear_analysis_result()
            return None
        _clear_analysis_result()
        return {
            "uploaded_vcf": cast(UploadedVCF, uploaded_vcf),
            "manual_variant": None,
            "phenotypes": phenotype_ids,
            "llm_model": llm_model,
        }

    normalized_variant = manual_variant.strip()
    if not normalized_variant:
        st.error(
            "Enter a variant in CHROM:POS:REF:ALT format before analysis."
        )
        _clear_analysis_result()
        return None

    _clear_analysis_result()
    return {
        "uploaded_vcf": None,
        "manual_variant": normalized_variant,
        "phenotypes": phenotype_ids,
        "llm_model": llm_model,
    }


def _render_variant_input(
    llm_model: str,
) -> AnalysisSubmission | None:
    """Render source selection and the batched analysis submission form."""

    with st.container(border=True):
        st.subheader("Variant input")
        input_mode = st.segmented_control(
            "Input source",
            (VCF_INPUT_MODE, MANUAL_INPUT_MODE),
            default=VCF_INPUT_MODE,
            key="variant_input_mode",
            on_change=_clear_analysis_result,
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
                    help=(
                        "Accepted formats: .vcf and .vcf.gz. "
                        f"Maximum size: "
                        f"{settings.MAX_UPLOAD_BYTES // 1_000_000} MB."
                    ),
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
            return _prepare_input(
                input_mode or VCF_INPUT_MODE,
                uploaded_vcf,
                manual_variant,
                llm_model,
            )
    return None


def _result_status(
    result: PipelineResult,
) -> tuple[str, str, bool]:
    """Return the final status label, state, and expansion mode."""

    if result["status"] == "success":
        return "Analysis completed", "complete", False
    if (
        result["status"] == "partial"
        and result["current_stage"] == "completed"
    ):
        return "Analysis completed with warnings", "complete", True
    if result["status"] == "partial":
        return "Analysis stopped with partial results", "error", True
    if result["status"] == "error":
        stage_label = PIPELINE_STAGE_LABELS.get(
            result["current_stage"],
            result["current_stage"].replace("_", " ").title(),
        )
        return f"Analysis failed during {stage_label}", "error", True
    return "Analysis in progress", "running", True


def _write_stage_records(target: object, result: PipelineResult) -> None:
    """Write all non-pending stage records to a status container."""

    for record in result["stages"]:
        if record["status"] == "pending":
            continue
        label = PIPELINE_STAGE_LABELS[record["stage"]]
        icon = PIPELINE_STATUS_ICONS[record["status"]]
        message = record["message"] or record["status"].title()
        target.write(f"{icon} **{label}:** {message}")


def _render_pipeline_issues(result: PipelineResult) -> None:
    """Render frontend-safe pipeline issues without tracebacks."""

    for issue in result["errors"]:
        if issue["recoverable"]:
            st.warning(issue["message"])
        else:
            st.error(issue["message"])


def _render_pipeline_status(result: PipelineResult) -> None:
    """Restore the latest execution status on ordinary reruns."""

    st.subheader("Analysis status")
    label, state, expanded = _result_status(result)
    st.progress(
        result["progress_percent"],
        text=f"{result['progress_percent']}% complete",
    )
    status = st.status(
        label,
        state=state,
        expanded=expanded,
    )
    _write_stage_records(status, result)
    _render_pipeline_issues(result)


def _execute_submission(
    submission: AnalysisSubmission,
) -> PipelineResult | None:
    """Run the complete pipeline with live progress feedback."""

    st.subheader("Analysis status")
    status = st.status("Starting analysis", expanded=True)
    progress_bar = status.progress(
        0,
        text="Preparing the analysis request",
    )
    emitted_updates: set[tuple[str, str, str | None]] = set()

    def update_progress(snapshot: PipelineResult) -> None:
        progress = snapshot["progress_percent"]
        current_stage = snapshot["current_stage"]
        progress_bar.progress(
            progress,
            text=f"{progress}% complete",
        )
        if current_stage == "completed":
            return

        record = next(
            stage
            for stage in snapshot["stages"]
            if stage["stage"] == current_stage
        )
        label = PIPELINE_STAGE_LABELS[current_stage]
        status.update(
            label=f"Running {label.casefold()}",
            state="running",
            expanded=True,
        )
        for stage_record in snapshot["stages"]:
            if stage_record["status"] == "pending":
                continue
            event = (
                stage_record["stage"],
                stage_record["status"],
                stage_record["message"],
            )
            if event in emitted_updates:
                continue
            emitted_updates.add(event)
            stage_label = PIPELINE_STAGE_LABELS[
                stage_record["stage"]
            ]
            icon = PIPELINE_STATUS_ICONS[stage_record["status"]]
            message = (
                stage_record["message"]
                or stage_record["status"].title()
            )
            status.write(
                f"{icon} **{stage_label}:** {message}"
            )

    try:
        result = execute_analysis(
            uploaded_vcf=submission["uploaded_vcf"],
            manual_variant=submission["manual_variant"],
            phenotypes=submission["phenotypes"],
            llm_model=submission["llm_model"],
            progress_callback=update_progress,
        )
    except FrontendExecutionError as exc:
        status.update(
            label="Analysis could not start",
            state="error",
            expanded=True,
        )
        status.error(str(exc))
        st.session_state[PIPELINE_RESULT_KEY] = None
        return None
    except Exception:
        status.update(
            label="Analysis could not complete",
            state="error",
            expanded=True,
        )
        status.error(
            "An unexpected internal error stopped the analysis."
        )
        st.session_state[PIPELINE_RESULT_KEY] = None
        return None

    st.session_state[PIPELINE_RESULT_KEY] = result
    progress_bar.progress(
        result["progress_percent"],
        text=f"{result['progress_percent']}% complete",
    )
    label, state, expanded = _result_status(result)
    status.update(label=label, state=state, expanded=expanded)
    _render_pipeline_issues(result)
    return result


def render_app() -> None:
    """Render the Stage 11 application and pipeline execution flow."""

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
    llm_model = _render_llm_model_selector()
    submission = _render_variant_input(llm_model)
    _render_hpo_picker()
    st.divider()

    pipeline_result: PipelineResult | None = None
    if submission is not None:
        pipeline_result = _execute_submission(submission)
    elif st.session_state[PIPELINE_RESULT_KEY] is not None:
        pipeline_result = cast(
            PipelineResult,
            st.session_state[PIPELINE_RESULT_KEY],
        )
        _render_pipeline_status(pipeline_result)

    if pipeline_result is not None:
        st.divider()
        render_analysis_results(pipeline_result)
        st.divider()
        render_report_viewer(pipeline_result)
