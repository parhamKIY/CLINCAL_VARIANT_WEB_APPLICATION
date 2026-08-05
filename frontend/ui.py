"""Presentation-only Streamlit interface for the analysis pipeline."""

from math import isfinite
from numbers import Integral, Real
from pathlib import Path
from typing import TypedDict, cast

import pandas as pd
import streamlit as st

from backend.error_handling import safe_ui_error_message
from backend.pipeline import (
    PipelineProgressCallback,
    PipelineResult,
    create_pipeline_result,
)
from backend.phenotype import (
    HPODataError,
    PhenotypeError,
    search_hpo_terms,
    update_hpo_data,
)
from backend.vcf_processing import (
    MAX_FILTERED_VCF_ROWS,
    STANDARD_PRIMARY_CHROMOSOMES,
    VCFProcessingError,
    get_primary_chromosome_length,
    normalize_primary_chromosome,
)
from config import settings
from frontend.execution import (
    AnalysisJob,
    UploadedVCF,
    execute_analysis,
)
from frontend.evidence_review import (
    clear_evidence_review_state,
    render_evidence_review,
)
from frontend.results import render_analysis_results


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
MANUAL_INPUT_MODE = "Manual table"
MANUAL_VARIANT_COLUMNS = (
    "chrom",
    "pos",
    "ref",
    "alt",
    "qual",
    "filter",
)
SELECTED_HPO_KEY = "selected_hpo_terms"
HPO_RESULTS_KEY = "hpo_search_results"
PIPELINE_RESULT_KEY = "pipeline_result"
ANALYSIS_JOB_KEY = "analysis_job"
ANALYSIS_NOTICE_KEY = "analysis_notice"
ANALYSIS_NOTICE_LEVEL_KEY = "analysis_notice_level"
LLM_MODEL_KEY = "selected_llm_model"
LLM_PINNED_MODELS = (
    "gpt-5.4-mini",
    "gpt-5.4",
    "gemini-3.1-pro-preview",
    "claude-sonnet-4-6",
    "gemini-3.1-flash-lite",
    "gpt-5.4-nano",
)
LLM_RECOMMENDED_MODELS = (
    "gpt-5.5",
    "claude-opus-4-8",
    "deepseek-v4-pro",
    "gemini-3.5-flash",
    "claude-haiku-4-5",
    "gpt-4.1-mini",
    "gpt-5-nano",
    "gemini-2.5-flash-lite",
    "deepseek-v4-flash",
)
LLM_MODEL_CATALOG = LLM_PINNED_MODELS + LLM_RECOMMENDED_MODELS
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
    "gpt-5.5": (
        "Maximum-quality option for the hardest conclusions; extremely "
        "high cost and potentially more billed reasoning tokens"
    ),
    "claude-opus-4-8": (
        "Premium nuanced synthesis and polished reports; very high cost"
    ),
    "deepseek-v4-pro": (
        "Strong analytical synthesis at comparatively low cost"
    ),
    "gemini-3.5-flash": (
        "Latest fast Google option with strong quality; costly for a "
        "Flash model"
    ),
    "claude-haiku-4-5": (
        "Fast, polished report writing at moderate cost"
    ),
    "gpt-4.1-mini": (
        "Reliable structured reports at low-to-moderate cost"
    ),
    "gpt-5-nano": (
        "Very cheap and fast for screening; reduced conclusion depth"
    ),
    "gemini-2.5-flash-lite": (
        "Ultra-low-cost fast drafts; reduced conclusion depth"
    ),
    "deepseek-v4-flash": (
        "Lowest-cost analytical alternative; validate report consistency"
    ),
}
PIPELINE_STAGE_LABELS = {
    "input": "Input validation",
    "vcf_processing": "VCF processing",
    "annotation": "Variant annotation",
    "phenotype": "Phenotype and gene evidence",
    "evidence": "Evidence construction",
    "llm": "Clinical interpretation",
    "report": "Report generation",
}
PIPELINE_STATUS_ICONS = {
    "pending": ":material/schedule:",
    "running": ":material/sync:",
    "success": ":material/check_circle:",
    "no_association": ":material/check_circle:",
    "warning": ":material/warning:",
    "error": ":material/error:",
    "skipped": ":material/skip_next:",
}
API_SOURCE_LABELS = {
    "vep": "Ensembl VEP",
    "genebe": "GeneBe",
    "myvariant": "MyVariant.info",
    "clinvar": "NCBI ClinVar",
    "clingen": "ClinGen/GenCC (UCSC)",
    "cspec": "ClinGen CSpec Registry",
    "phen2gene": "Phen2Gene",
    "mydisease": "MyDisease.info",
    "llm": "LLM API",
}
API_STATUS_LABELS = {
    "pending": "Waiting",
    "running": "In progress",
    "success": "Success",
    "no_association": "No association",
    "warning": "Completed with warnings",
    "error": "Failed",
    "skipped": "Not called",
}


class AnalysisSubmission(TypedDict):
    """One validated frontend request ready for pipeline execution."""

    uploaded_vcf: UploadedVCF | None
    manual_variants: list[dict[str, object]] | None
    phenotypes: list[str]
    llm_model: str


def _llm_model_options() -> tuple[str, ...]:
    """Return pinned, recommended, and configured model options."""

    return tuple(
        dict.fromkeys((*LLM_MODEL_CATALOG, settings.LLM_MODEL))
    )


def _format_llm_model_option(model: str) -> str:
    """Add a concise use-case advantage to one model identifier."""

    advantage = LLM_MODEL_ADVANTAGES.get(
        model,
        "Custom model configured in .env",
    )
    pin_label = "Pinned — " if model in LLM_PINNED_MODELS else ""
    return f"{model} — {pin_label}{advantage}"


def _select_pinned_llm_model(model: str) -> None:
    """Select one pinned model and discard an older analysis result."""

    st.session_state[LLM_MODEL_KEY] = model
    _clear_analysis_result()


def _initialize_session_state() -> None:
    """Initialize frontend-only state in one place."""

    st.session_state.setdefault(SELECTED_HPO_KEY, [])
    st.session_state.setdefault(HPO_RESULTS_KEY, [])
    st.session_state.setdefault(PIPELINE_RESULT_KEY, None)
    st.session_state.setdefault(ANALYSIS_JOB_KEY, None)
    st.session_state.setdefault(ANALYSIS_NOTICE_KEY, None)
    st.session_state.setdefault(ANALYSIS_NOTICE_LEVEL_KEY, "info")
    model_options = _llm_model_options()
    if st.session_state.get(LLM_MODEL_KEY) not in model_options:
        st.session_state[LLM_MODEL_KEY] = settings.LLM_MODEL


def _clear_analysis_result() -> None:
    """Discard an earlier result when analysis inputs change."""

    st.session_state[PIPELINE_RESULT_KEY] = None
    st.session_state.pop("selected_evidence_object", None)
    clear_evidence_review_state()


def _manual_widget_key(field: str, row_index: int) -> str:
    """Return one stable key for a manual-variant row widget."""

    return f"manual_variant_{field}_{row_index}"


def _manual_chromosome_changed(row_index: int) -> None:
    """Clear stale state when a manual chromosome selection is removed."""

    _clear_analysis_result()
    chromosome_key = _manual_widget_key("chrom", row_index)
    if st.session_state.get(chromosome_key) is None:
        st.session_state.pop(
            _manual_widget_key("pos", row_index),
            None,
        )


def _analysis_job() -> AnalysisJob | None:
    """Return the current background job when one exists."""

    job = st.session_state.get(ANALYSIS_JOB_KEY)
    return job if isinstance(job, AnalysisJob) else None


def _cancel_active_analysis() -> None:
    """Request cooperative cancellation from the active worker."""

    job = _analysis_job()
    if job is not None:
        job.request_cancel()
    _clear_analysis_result()


def _load_styles() -> None:
    """Load local presentation styles without adding analysis logic."""

    try:
        css = STYLES_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    if css.strip():
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def _render_appearance_status() -> None:
    """Show where the native per-user light/dark selector lives."""

    st.subheader("Appearance")
    st.badge(
        "Light / Dark",
        icon=":material/contrast:",
        color="blue",
    )
    st.caption(
        "Switch between Light and Dark from the app menu: "
        "⋮ → Settings → Theme."
    )


def _render_workflow_overview() -> None:
    """Show the backend stages the frontend will orchestrate."""

    stages = (
        "Input",
        "Annotation",
        "Phenotype",
        "Evidence",
        "Interpretation",
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
            "The six primary choices are pinned first. Open the menu "
            "and type any part of a model name to search the curated "
            "AvalAI chat-model catalog."
        )
        with st.container(
            key="pinned_llm_models",
            gap="xsmall",
        ):
            st.markdown(
                "**:material/push_pin: Pinned model quick picks**"
            )
            st.caption(
                "Frequently used models are grouped here for fast "
                "access. Hover over a model to see its advantage."
            )
            with st.container(
                horizontal=True,
                gap="xsmall",
            ):
                for model in LLM_PINNED_MODELS:
                    st.button(
                        model,
                        key=f"select_{model}",
                        type=(
                            "primary"
                            if st.session_state[LLM_MODEL_KEY] == model
                            else "secondary"
                        ),
                        help=LLM_MODEL_ADVANTAGES[model],
                        on_click=_select_pinned_llm_model,
                        args=(model,),
                    )
        selected_model = st.selectbox(
            "LLM model",
            _llm_model_options(),
            key=LLM_MODEL_KEY,
            format_func=_format_llm_model_option,
            placeholder="Search or select an AvalAI chat model",
            filter_mode="contains",
            help=(
                "More capable models may produce stronger summaries but "
                "usually cost more. Premium reasoning may also increase "
                "billed token usage. Every result still requires review "
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


def _manual_variant_table() -> pd.DataFrame:
    """Return five blank rows with stable editor column types."""

    return pd.DataFrame(
        {
            "chrom": pd.Series(
                [pd.NA] * MAX_FILTERED_VCF_ROWS,
                dtype="string",
            ),
            "pos": pd.Series(
                [pd.NA] * MAX_FILTERED_VCF_ROWS,
                dtype="Int64",
            ),
            "ref": pd.Series(
                [""] * MAX_FILTERED_VCF_ROWS,
                dtype="string",
            ),
            "alt": pd.Series(
                [""] * MAX_FILTERED_VCF_ROWS,
                dtype="string",
            ),
            "qual": pd.Series(
                [pd.NA] * MAX_FILTERED_VCF_ROWS,
                dtype="Float64",
            ),
            "filter": pd.Series(
                [""] * MAX_FILTERED_VCF_ROWS,
                dtype="string",
            ),
        }
    )


def _is_blank_table_value(value: object) -> bool:
    """Return whether one editor cell is unused."""

    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return bool(pd.isna(value))


def _normalize_manual_table(
    table: object,
) -> list[dict[str, object]]:
    """Convert used editor rows into the backend manual-table contract."""

    if (
        not isinstance(table, pd.DataFrame)
        or tuple(table.columns) != MANUAL_VARIANT_COLUMNS
    ):
        raise ValueError("The manual variant table is invalid.")
    rows: list[dict[str, object]] = []
    for row_index, raw_row in enumerate(
        table.to_dict(orient="records")
    ):
        if all(
            _is_blank_table_value(raw_row[column])
            for column in MANUAL_VARIANT_COLUMNS
        ):
            continue
        required = ("chrom", "pos", "ref", "alt")
        if any(
            _is_blank_table_value(raw_row[column])
            for column in required
        ):
            raise ValueError(
                f"Complete CHROM, POS, REF, and ALT in row "
                f"{row_index + 1}."
            )
        raw_position = raw_row["pos"]
        if (
            isinstance(raw_position, bool)
            or not isinstance(raw_position, Integral)
            or int(raw_position) <= 0
        ):
            raise ValueError(
                f"POS in row {row_index + 1} must be a positive integer."
            )
        try:
            chromosome = normalize_primary_chromosome(
                str(raw_row["chrom"])
            )
            chromosome_length = get_primary_chromosome_length(
                chromosome,
                settings.GENOME_ASSEMBLY,
            )
        except VCFProcessingError as exc:
            raise ValueError(
                f"CHROM in row {row_index + 1} is invalid: {exc}"
            ) from exc
        if int(raw_position) > chromosome_length:
            raise ValueError(
                f"POS in row {row_index + 1} must be between 1 and "
                f"{chromosome_length:,} for chromosome {chromosome} in "
                f"{settings.GENOME_ASSEMBLY}. Analysis will not start "
                "until this value is corrected."
            )
        raw_quality = raw_row["qual"]
        quality: float | None = None
        if not _is_blank_table_value(raw_quality):
            if (
                isinstance(raw_quality, bool)
                or not isinstance(raw_quality, Real)
                or not isfinite(float(raw_quality))
                or float(raw_quality) < 0
            ):
                raise ValueError(
                    f"QUAL in row {row_index + 1} must be non-negative."
                )
            quality = float(raw_quality)
        rows.append(
            {
                "chrom": chromosome,
                "pos": int(raw_position),
                "ref": str(raw_row["ref"]).strip(),
                "alt": str(raw_row["alt"]).strip(),
                "qual": quality,
                "filter": (
                    None
                    if _is_blank_table_value(raw_row["filter"])
                    else str(raw_row["filter"]).strip()
                ),
            }
        )
    if not rows:
        raise ValueError("Enter at least one manual variant row.")
    return rows


def _manual_position_error(
    chromosome: str | None,
    position: object,
    row_index: int,
) -> str | None:
    """Return an assembly-aware error for one visible POS widget."""

    if chromosome is None or position is None:
        return None
    chromosome_length = get_primary_chromosome_length(
        chromosome,
        settings.GENOME_ASSEMBLY,
    )
    if (
        isinstance(position, bool)
        or not isinstance(position, Integral)
        or int(position) < 1
        or int(position) > chromosome_length
    ):
        return (
            f"POS in row {row_index + 1} must be between 1 and "
            f"{chromosome_length:,} for chromosome {chromosome} "
            f"({settings.GENOME_ASSEMBLY}). Correct this value before "
            "analysis."
        )
    return None


def _render_manual_variant_table(
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Render five row-aware native Streamlit variant-input rows."""

    table = _manual_variant_table()
    position_errors: list[str] = []
    for row_index in range(MAX_FILTERED_VCF_ROWS):
        first_row = row_index == 0
        visible_label = "visible" if first_row else "collapsed"
        row_label = row_index + 1

        with st.container(
            horizontal=True,
            vertical_alignment="top",
            gap="small",
        ):
            chromosome = st.selectbox(
                "CHROM" if first_row else f"Row {row_label} CHROM",
                STANDARD_PRIMARY_CHROMOSOMES,
                index=None,
                key=_manual_widget_key("chrom", row_index),
                placeholder="Select",
                help=(
                    "Select a standard human chromosome: "
                    "1-22, X, Y, or MT."
                ),
                on_change=_manual_chromosome_changed,
                args=(row_index,),
                label_visibility=visible_label,
                width=105,
            )
            chromosome_length = (
                get_primary_chromosome_length(
                    chromosome,
                    settings.GENOME_ASSEMBLY,
                )
                if chromosome is not None
                else max(
                    get_primary_chromosome_length(
                        available_chromosome,
                        settings.GENOME_ASSEMBLY,
                    )
                    for available_chromosome
                    in STANDARD_PRIMARY_CHROMOSOMES
                )
            )
            position = st.number_input(
                "POS" if first_row else f"Row {row_label} POS",
                min_value=1,
                max_value=chromosome_length,
                value=None,
                step=1,
                format="%d",
                key=_manual_widget_key("pos", row_index),
                placeholder=(
                    f"1 - {chromosome_length:,}"
                    if chromosome is not None
                    else "Select CHROM first"
                ),
                disabled=chromosome is None,
                help=(
                    "The allowed range follows the selected chromosome "
                    f"in {settings.GENOME_ASSEMBLY}."
                ),
                on_change=_clear_analysis_result,
                label_visibility=visible_label,
                width=190,
            )
            reference = st.text_input(
                "REF" if first_row else f"Row {row_label} REF",
                key=_manual_widget_key("ref", row_index),
                placeholder="e.g. A",
                on_change=_clear_analysis_result,
                label_visibility=visible_label,
                width=95,
            )
            alternate = st.text_input(
                "ALT" if first_row else f"Row {row_label} ALT",
                key=_manual_widget_key("alt", row_index),
                placeholder="e.g. G",
                help=(
                    "Use a nucleotide allele or a supported symbolic "
                    "allele such as <DEL>."
                ),
                on_change=_clear_analysis_result,
                label_visibility=visible_label,
                width=115,
            )
            quality = st.number_input(
                "QUAL" if first_row else f"Row {row_label} QUAL",
                min_value=0.0,
                value=None,
                step=0.01,
                format="%.2f",
                key=_manual_widget_key("qual", row_index),
                placeholder="Optional",
                on_change=_clear_analysis_result,
                label_visibility=visible_label,
                width=115,
            )
            filter_value = st.text_input(
                "FILTER" if first_row else f"Row {row_label} FILTER",
                key=_manual_widget_key("filter", row_index),
                placeholder="e.g. PASS",
                on_change=_clear_analysis_result,
                label_visibility=visible_label,
                width=120,
            )

        position_error = _manual_position_error(
            chromosome,
            position,
            row_index,
        )
        if position_error is not None:
            position_errors.append(position_error)
            st.error(position_error)

        table.at[row_index, "chrom"] = (
            pd.NA if chromosome is None else chromosome
        )
        table.at[row_index, "pos"] = (
            pd.NA if position is None else position
        )
        table.at[row_index, "ref"] = reference
        table.at[row_index, "alt"] = alternate
        table.at[row_index, "qual"] = (
            pd.NA if quality is None else quality
        )
        table.at[row_index, "filter"] = filter_value
    return table, tuple(position_errors)


def _prepare_input(
    input_mode: str,
    uploaded_vcf: object | None,
    manual_table: object,
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
            "manual_variants": None,
            "phenotypes": phenotype_ids,
            "llm_model": llm_model,
        }

    try:
        normalized_variants = _normalize_manual_table(manual_table)
    except ValueError as exc:
        st.error(str(exc))
        _clear_analysis_result()
        return None

    _clear_analysis_result()
    return {
        "uploaded_vcf": None,
        "manual_variants": normalized_variants,
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

        job = _analysis_job()
        job_state = job.view().state if job is not None else None
        job_present = job is not None
        cancellation_pending = job_state == "cancelling"

        uploaded_vcf = None
        manual_table: object = _manual_variant_table()
        position_errors: tuple[str, ...] = ()
        if input_mode == MANUAL_INPUT_MODE:
            st.caption(
                "Enter up to five already-filtered variants. "
                "CHROM, POS, REF, and ALT are required. Every row "
                "is annotated; the report focuses on the first listed "
                "variant."
            )
            manual_table, position_errors = (
                _render_manual_variant_table()
            )

        with st.form("analysis_input_form", border=False):
            if input_mode == VCF_INPUT_MODE:
                uploaded_vcf = st.file_uploader(
                    "VCF file",
                    type=("vcf", "gz"),
                    key="vcf_upload",
                    help=(
                        "Accepted formats: .vcf and .vcf.gz. "
                        f"The filtered file must contain 1 to "
                        f"{MAX_FILTERED_VCF_ROWS} data rows. Every row is "
                        "annotated; the report focuses on the first listed "
                        "variant. "
                        f"Maximum size: "
                        f"{settings.MAX_UPLOAD_BYTES // 1_000_000} MB."
                    ),
                )

            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                gap="small",
            ):
                submitted = st.form_submit_button(
                    "Analyze variants",
                    type="primary",
                    icon=":material/biotech:",
                    width="stretch",
                    disabled=job_present or bool(position_errors),
                )
                st.form_submit_button(
                    "Cancelling..." if cancellation_pending else "Cancel",
                    key="cancel_analysis",
                    icon=":material/cancel:",
                    width="stretch",
                    disabled=(
                        not job_present
                        or job_state not in {"running", "cancelling"}
                        or cancellation_pending
                    ),
                    help=(
                        "Stop the active analysis and remove its "
                        "temporary files and unpublished draft output."
                    ),
                    on_click=_cancel_active_analysis,
                )

        if submitted:
            return _prepare_input(
                input_mode or VCF_INPUT_MODE,
                uploaded_vcf,
                manual_table,
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


def _render_api_statuses(result: PipelineResult) -> None:
    """Show every external API state in a permanently visible panel."""

    with st.container(border=True):
        st.markdown("**External API status**")
        st.caption(
            "This panel updates during analysis. Failed API attempts "
            "are retried automatically; successful requests with "
            "missing evidence are not treated as failures."
        )
        for record in result["api_statuses"]:
            status = record["status"]
            icon = PIPELINE_STATUS_ICONS[status]
            source = API_SOURCE_LABELS[record["source"]]
            status_label = API_STATUS_LABELS[status]
            message = record["message"] or status_label
            st.write(
                f"{icon} **{source} — {status_label}:** {message}"
            )


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
    _render_api_statuses(result)
    _render_pipeline_issues(result)


def _start_submission(
    submission: AnalysisSubmission,
) -> None:
    """Start one cancellable background analysis."""

    if _analysis_job() is not None:
        return

    def runner(
        progress_callback: PipelineProgressCallback,
    ) -> PipelineResult:
        return execute_analysis(
            uploaded_vcf=submission["uploaded_vcf"],
            manual_variants=submission["manual_variants"],
            phenotypes=submission["phenotypes"],
            llm_model=submission["llm_model"],
            progress_callback=progress_callback,
        )

    _clear_analysis_result()
    st.session_state[ANALYSIS_NOTICE_KEY] = None
    job = AnalysisJob(runner)
    st.session_state[ANALYSIS_JOB_KEY] = job
    job.start()


def _finish_analysis_job(job: AnalysisJob) -> None:
    """Transfer one terminal job outcome into session state."""

    view = job.view()
    if view.state == "completed":
        st.session_state[PIPELINE_RESULT_KEY] = view.result
    else:
        st.session_state[PIPELINE_RESULT_KEY] = None

    if view.state == "cancelled":
        st.session_state[ANALYSIS_NOTICE_KEY] = (
            view.cleanup_warning
            or (
                "Analysis cancelled. Temporary uploads and draft "
                "outputs were removed."
            )
        )
        st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = (
            "warning" if view.cleanup_warning else "info"
        )
    elif view.state == "error":
        st.session_state[ANALYSIS_NOTICE_KEY] = (
            view.error_message
            or "An unexpected internal error stopped the analysis."
        )
        st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "error"

    st.session_state[ANALYSIS_JOB_KEY] = None
    st.rerun()


@st.fragment(run_every=0.5)
def _render_analysis_job(job: AnalysisJob) -> None:
    """Poll and render one cancellable background analysis."""

    view = job.view()
    if view.state in {"completed", "cancelled", "error"}:
        _finish_analysis_job(job)

    st.subheader("Analysis status")
    result = view.latest_result
    progress = result["progress_percent"] if result is not None else 0
    progress_text = (
        "Cancellation requested"
        if view.state == "cancelling"
        else (
            f"{progress}% complete"
            if result is not None
            else "Preparing the analysis request"
        )
    )
    st.progress(progress, text=progress_text)
    status = st.status(
        (
            "Cancelling analysis"
            if view.state == "cancelling"
            else "Analysis in progress"
        ),
        state="running",
        expanded=True,
    )
    if result is not None:
        _write_stage_records(status, result)
        _render_api_statuses(result)
    else:
        _render_api_statuses(create_pipeline_result())
    if view.state == "cancelling":
        status.info(
            "The current bounded operation will stop at the next safe "
            "cancellation point."
        )


def _render_analysis_notice() -> None:
    """Render and consume one terminal background-job notice."""

    notice = st.session_state.get(ANALYSIS_NOTICE_KEY)
    if not isinstance(notice, str) or not notice:
        return
    level = st.session_state.get(ANALYSIS_NOTICE_LEVEL_KEY, "info")
    if level == "error":
        st.error(notice)
    elif level == "warning":
        st.warning(notice)
    else:
        st.info(notice)
    st.session_state[ANALYSIS_NOTICE_KEY] = None


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
        _render_appearance_status()
        st.divider()
        _render_hpo_update_control()

    st.subheader("Analysis workflow")
    _render_workflow_overview()
    st.divider()
    llm_model = _render_llm_model_selector()
    submission = _render_variant_input(llm_model)
    _render_hpo_picker()
    st.divider()

    _render_analysis_notice()
    pipeline_result: PipelineResult | None = None
    if submission is not None:
        _start_submission(submission)
        st.rerun()
    analysis_job = _analysis_job()
    if analysis_job is not None:
        _render_analysis_job(analysis_job)
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
        render_evidence_review(pipeline_result)
