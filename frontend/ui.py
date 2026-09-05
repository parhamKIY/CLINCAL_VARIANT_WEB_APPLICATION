"""Presentation-only Streamlit interface for the analysis pipeline."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from math import isfinite
from numbers import Integral, Real
from pathlib import Path
from typing import TypedDict, cast

import pandas as pd
import streamlit as st

from backend.database import DatabaseError, load_pipeline_state
from backend.clinical_entities import (
    ClinicalEntityError,
    normalize_clinical_entity_text,
    validate_clinical_entities,
)
from backend.excel_processing import (
    ExcelProcessingError,
    discover_excel_worksheets,
    parse_excel_input_records,
)
from backend.error_handling import safe_ui_error_message
from backend.execution_trace import AnalysisExecutionTrace
from backend.llm import LLMError, get_available_llm_models
from backend.llm_preflight import LLMPreflightResult, check_llm_connectivity
from backend.phenotype_llm import (
    PhenotypeExtractionError,
    extract_hpo_candidates,
)
from backend.pipeline import (
    PipelineProgressCallback,
    PipelineResult,
)
from backend.phenotype import (
    HPODataError,
    PhenotypeError,
    lookup_hpo_term,
    search_hpo_terms,
    update_hpo_data,
)
from backend.phenotype_selection import (
    PhenotypeSelectionError,
    accept_hpo_candidates,
    validate_hpo_candidates,
)
from backend.privacy import ClinicalDataPrivacyError
from backend.vcf_processing import (
    STANDARD_PRIMARY_CHROMOSOMES,
    VCFProcessingError,
    get_primary_chromosome_length,
    normalize_primary_chromosome,
)
from config import MAX_VARIANTS_PER_ANALYSIS, settings
from frontend.analysis_summary import (
    build_analysis_summary,
    build_identity_resolution_failure_presentation,
    input_validation_message,
)
from frontend.execution import (
    ANALYSIS_TIMEOUT_MESSAGE,
    AnalysisJob,
    FrontendExecutionError,
    UploadedVCF,
    execute_analysis,
    get_registered_analysis_job,
    load_last_session_analysis_id,
    prepare_analysis_recovery_request,
    recover_analysis_job,
    register_analysis_job,
    release_registered_analysis_job,
    save_last_session_analysis_id,
)
from frontend.evidence_review import (
    clear_evidence_review_state,
    render_evidence_review,
)
from frontend.evidence_graph import render_evidence_graph
from frontend.execution_trace import render_execution_trace
from frontend.provider_readiness import (
    current_provider_readiness_snapshot,
    initialize_provider_readiness_state,
    render_provider_readiness,
)
from frontend.results import render_analysis_results
from frontend.xlsx_selection import (
    XLSXSelectionError,
    select_excel_input_records,
    selected_source_rows_message,
    source_row_display_rows,
)


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
VCF_INPUT_MODE = "File upload"
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
HPO_MODEL_CANDIDATES_KEY = "hpo_model_candidates"
HPO_MODEL_REJECTIONS_KEY = "hpo_model_rejections"
PHENOTYPE_NON_HPO_MENTIONS_KEY = "phenotype_non_hpo_mentions"
PHENOTYPE_EXTRACTION_PROVENANCE_KEY = "phenotype_extraction_provenance"
CLINICAL_ENTITIES_KEY = "clinical_entities"
CLINICAL_ENTITY_DRAFTS_KEY = "clinical_entity_review_drafts"
CLINICAL_ENTITY_REVIEW_COMPLETE_KEY = "clinical_entity_review_complete"
CLINICAL_ENTITY_FINDINGS_EDITOR_KEY = "clinical_entity_findings_editor"
CLINICAL_ENTITY_DISEASE_EDITOR_KEY = "clinical_entity_disease_editor"
HPO_CANDIDATE_EDITOR_KEY = "hpo_candidate_editor"
PIPELINE_RESULT_KEY = "pipeline_result"
ANALYSIS_JOB_KEY = "analysis_job"
ANALYSIS_JOB_TOKEN_KEY = "analysis_job_token"
ANALYSIS_NOTICE_KEY = "analysis_notice"
ANALYSIS_NOTICE_LEVEL_KEY = "analysis_notice_level"
ANALYSIS_EXECUTION_TRACE_KEY = "analysis_execution_trace"
PHENOTYPE_MODEL_KEY = "selected_phenotype_extraction_model"
VARIANT_MODEL_KEY = "selected_variant_interpretation_model"
LLM_PROVIDER_MODELS_KEY = "llm_provider_models"
LLM_PROVIDER_MODELS_ERROR_KEY = "llm_provider_models_error"
LLM_PREFLIGHT_PHENOTYPE_KEY = "llm_preflight_phenotype"
LLM_PREFLIGHT_VARIANT_KEY = "llm_preflight_variant"
ANALYSIS_JOB_QUERY_PARAM = "analysis_job"
ANALYSIS_RESULT_QUERY_PARAM = "analysis"
PIPELINE_STAGE_LABELS = {
    "input": "Input validation",
    "vcf_processing": "Variant input processing",
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
class AnalysisSubmission(TypedDict):
    """One validated frontend request ready for pipeline execution."""

    uploaded_vcf: UploadedVCF | None
    manual_variants: list[dict[str, object]] | None
    excel_input_records: list[dict[str, object]] | None
    phenotypes: list[str]
    clinical_entities: list[dict[str, object]]
    input_type: str
    phenotype_extraction_model: str
    phenotype_extraction_provenance: dict[str, object] | None
    llm_model: str


@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def _provider_llm_models() -> tuple[tuple[str, ...], bool]:
    """Return a short-lived provider model catalog when supported."""

    try:
        return get_available_llm_models(), False
    except LLMError:
        return (), True


def _task_model_options(
    configured_model: str,
    provider_models: tuple[str, ...] = (),
    current_model: object = None,
) -> tuple[str, ...]:
    """Return one task default plus provider-advertised model IDs."""

    current_options = (
        (current_model.strip(),)
        if isinstance(current_model, str) and current_model.strip()
        else ()
    )
    return tuple(
        dict.fromkeys(
            (
                configured_model,
                *current_options,
                settings.LLM_MODEL,
                *provider_models,
            )
        )
    )


def _initialize_task_model(key: str, default: str) -> None:
    """Initialize one model role without discarding a typed custom ID."""

    current = st.session_state.get(key)
    if not isinstance(current, str) or not current.strip():
        st.session_state[key] = default


def _query_param_value(name: str) -> str | None:
    """Return one bounded scalar query parameter."""

    value = st.query_params.get(name)
    if isinstance(value, str) and 0 < len(value) <= 128:
        return value
    return None


def _clear_query_param(name: str) -> None:
    """Remove one application-owned query parameter when present."""

    if name in st.query_params:
        del st.query_params[name]


def _restore_refresh_state() -> None:
    """Reconnect active work or reload a persisted completed analysis."""

    if (
        st.session_state[ANALYSIS_JOB_KEY] is None
        and st.session_state[PIPELINE_RESULT_KEY] is None
    ):
        token = _query_param_value(ANALYSIS_JOB_QUERY_PARAM)
        job = get_registered_analysis_job(token)
        if job is None:
            job = recover_analysis_job(token)
        if job is not None:
            st.session_state[ANALYSIS_JOB_KEY] = job
            st.session_state[ANALYSIS_JOB_TOKEN_KEY] = token
            st.session_state[ANALYSIS_NOTICE_KEY] = (
                "Reconnected to the active analysis after page refresh; "
                "restart recovery is also enabled."
            )
            st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "info"
        elif token is not None:
            _clear_query_param(ANALYSIS_JOB_QUERY_PARAM)

    if (
        st.session_state[ANALYSIS_JOB_KEY] is not None
        or st.session_state[PIPELINE_RESULT_KEY] is not None
    ):
        return
    analysis_id = _query_param_value(ANALYSIS_RESULT_QUERY_PARAM)
    if analysis_id is None:
        return
    try:
        restored = load_pipeline_state(analysis_id)
    except DatabaseError:
        _clear_query_param(ANALYSIS_RESULT_QUERY_PARAM)
        st.session_state[ANALYSIS_NOTICE_KEY] = (
            "The saved analysis referenced by this page is unavailable."
        )
        st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "warning"
        return
    st.session_state[PIPELINE_RESULT_KEY] = restored
    context = restored["analysis_context"]
    restored_terms: list[dict[str, str]] = []
    for hpo_id in context["accepted_hpo_terms"]:
        try:
            term = lookup_hpo_term(hpo_id)
        except (HPODataError, PhenotypeError):
            restored_terms.append(
                {"id": hpo_id, "name": "Ontology label unavailable"}
            )
        else:
            restored_terms.append(
                {"id": term["id"], "name": term["name"]}
            )
    st.session_state[SELECTED_HPO_KEY] = restored_terms
    if context["phenotype_extraction_model"] is not None:
        st.session_state[PHENOTYPE_MODEL_KEY] = context[
            "phenotype_extraction_model"
        ]
    if context["variant_interpretation_model"] is not None:
        st.session_state[VARIANT_MODEL_KEY] = context[
            "variant_interpretation_model"
        ]
    st.session_state[PHENOTYPE_EXTRACTION_PROVENANCE_KEY] = deepcopy(
        context["phenotype_extraction_provenance"]
    )
    st.session_state[CLINICAL_ENTITIES_KEY] = deepcopy(
        context["clinical_entities"] or []
    )
    st.session_state[CLINICAL_ENTITY_DRAFTS_KEY] = (
        prepare_clinical_entity_review_rows(
            context["clinical_entities"] or []
        )
    )
    st.session_state[CLINICAL_ENTITY_REVIEW_COMPLETE_KEY] = True
    st.session_state[ANALYSIS_NOTICE_KEY] = (
        "Restored the saved analysis after page refresh."
    )
    st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "info"


def _initialize_session_state() -> None:
    """Initialize frontend-only state in one place."""

    st.session_state.setdefault(SELECTED_HPO_KEY, [])
    st.session_state.setdefault(HPO_RESULTS_KEY, [])
    st.session_state.setdefault(HPO_MODEL_CANDIDATES_KEY, [])
    st.session_state.setdefault(HPO_MODEL_REJECTIONS_KEY, [])
    st.session_state.setdefault(PHENOTYPE_NON_HPO_MENTIONS_KEY, {})
    st.session_state.setdefault(PHENOTYPE_EXTRACTION_PROVENANCE_KEY, None)
    st.session_state.setdefault(CLINICAL_ENTITIES_KEY, [])
    st.session_state.setdefault(CLINICAL_ENTITY_DRAFTS_KEY, [])
    st.session_state.setdefault(CLINICAL_ENTITY_REVIEW_COMPLETE_KEY, True)
    st.session_state.setdefault(PIPELINE_RESULT_KEY, None)
    st.session_state.setdefault(ANALYSIS_JOB_KEY, None)
    st.session_state.setdefault(ANALYSIS_JOB_TOKEN_KEY, None)
    st.session_state.setdefault(ANALYSIS_NOTICE_KEY, None)
    st.session_state.setdefault(ANALYSIS_NOTICE_LEVEL_KEY, "info")
    st.session_state.setdefault(ANALYSIS_EXECUTION_TRACE_KEY, None)
    st.session_state.setdefault(LLM_PROVIDER_MODELS_KEY, ())
    st.session_state.setdefault(LLM_PROVIDER_MODELS_ERROR_KEY, False)
    st.session_state.setdefault(LLM_PREFLIGHT_PHENOTYPE_KEY, None)
    st.session_state.setdefault(LLM_PREFLIGHT_VARIANT_KEY, None)
    initialize_provider_readiness_state()
    _initialize_task_model(
        PHENOTYPE_MODEL_KEY,
        settings.PHENOTYPE_EXTRACTION_MODEL,
    )
    _initialize_task_model(
        VARIANT_MODEL_KEY,
        settings.VARIANT_INTERPRETATION_MODEL,
    )
    _restore_refresh_state()


def _render_restore_last_session() -> None:
    """Offer to reload the previous analysis until the user acts on it.

    The banner is only shown when:
    - No analysis is currently active or already loaded in session state.
    - The ``?analysis=`` URL param is absent (bare root URL navigation).
    - A valid last-session analysis ID exists on disk.

    The banner must remain rendered across Streamlit reruns so its button
    event can be processed.
    """

    if (
        _analysis_job() is not None
        or st.session_state.get(PIPELINE_RESULT_KEY) is not None
    ):
        return
    if _query_param_value(ANALYSIS_RESULT_QUERY_PARAM) is not None:
        return
    if _query_param_value(ANALYSIS_JOB_QUERY_PARAM) is not None:
        return

    last_id = load_last_session_analysis_id()
    if last_id is None:
        return

    restore_error: str | None = None
    with st.container(border=True):
        col_text, col_button = st.columns([4, 1])
        with col_text:
            st.info(
                ":material/history: **Your previous analysis is saved.** "
                "Click **Restore** to reload it, or simply run a new analysis "
                "to start fresh.",
                icon=None,
            )
        with col_button:
            if st.button(
                "Restore",
                key="restore_last_session",
                icon=":material/restore:",
                type="primary",
                width="stretch",
            ):
                try:
                    load_pipeline_state(last_id)
                except DatabaseError:
                    restore_error = (
                        "The previous saved analysis is no longer available. "
                        "Run a new analysis to create a new restorable result."
                    )
                else:
                    st.query_params[ANALYSIS_RESULT_QUERY_PARAM] = last_id
                    st.rerun()
    if restore_error is not None:
        st.warning(restore_error)


def _clear_analysis_result() -> None:
    """Mark analysis inputs as changed without discarding the loaded result URL.

    This is a lightweight signal used by widget on_change callbacks. It clears
    in-memory display artifacts (selected evidence object, evidence review draft
    state) but deliberately does NOT remove the ``?analysis=`` URL query
    parameter. The loaded analysis therefore survives until the user explicitly
    submits a new one via the Analyze variants button.
    """

    st.session_state.pop("selected_evidence_object", None)
    clear_evidence_review_state()


def _discard_analysis_result() -> None:
    """Fully discard a completed analysis result and clear the URL param.

    Call this only when the user deliberately starts a new analysis (clicking
    Analyze variants or Cancel). Do NOT call it from widget on_change
    callbacks that merely change input values.
    """

    st.session_state[PIPELINE_RESULT_KEY] = None
    st.session_state[ANALYSIS_EXECUTION_TRACE_KEY] = None
    _clear_query_param(ANALYSIS_RESULT_QUERY_PARAM)
    if _analysis_job() is None:
        token = _query_param_value(ANALYSIS_JOB_QUERY_PARAM)
        release_registered_analysis_job(token)
        _clear_query_param(ANALYSIS_JOB_QUERY_PARAM)
    st.session_state.pop("selected_evidence_object", None)
    clear_evidence_review_state()


def _manual_widget_key(field: str, row_index: int) -> str:
    """Return one stable key for a manual-variant row widget."""

    return f"manual_variant_{field}_{row_index}"


def _manual_chromosome_changed(row_index: int) -> None:
    """Clear stale position state when a manual chromosome selection changes."""

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
    _discard_analysis_result()


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
    st.success(
        "HPO data updated to "
        f"{result['current_version']} "
        f"({result['active_term_count']:,} active terms)."
    )


def _clear_hpo_candidate_draft() -> None:
    """Discard unaccepted model suggestions when their source text changes."""

    st.session_state[HPO_MODEL_CANDIDATES_KEY] = []
    st.session_state[HPO_MODEL_REJECTIONS_KEY] = []
    st.session_state[PHENOTYPE_NON_HPO_MENTIONS_KEY] = {}
    st.session_state[PHENOTYPE_EXTRACTION_PROVENANCE_KEY] = None
    st.session_state[CLINICAL_ENTITIES_KEY] = []
    st.session_state[CLINICAL_ENTITY_DRAFTS_KEY] = []
    st.session_state[CLINICAL_ENTITY_REVIEW_COMPLETE_KEY] = True
    st.session_state.pop(CLINICAL_ENTITY_FINDINGS_EDITOR_KEY, None)
    st.session_state.pop(CLINICAL_ENTITY_DISEASE_EDITOR_KEY, None)
    st.session_state.pop(HPO_CANDIDATE_EDITOR_KEY, None)


def prepare_clinical_entity_review_rows(
    entities: object,
) -> list[dict[str, object]]:
    """Create editable reviewer rows without accepting extracted entities."""

    validated = validate_clinical_entities(entities)
    return [
        {"source_index": index, "include": True, **entity}
        for index, entity in enumerate(validated)
    ]


def accept_clinical_entity_review_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Validate explicitly retained reviewer rows in source order."""

    retained: list[tuple[int, dict[str, object]]] = []
    seen_indexes: set[int] = set()
    for row in rows:
        include = row.get("include")
        if not isinstance(include, bool):
            raise ClinicalEntityError(
                "Clinical entity review selection is invalid."
            )
        source_index = row.get("source_index")
        if (
            not isinstance(source_index, int)
            or isinstance(source_index, bool)
            or source_index < 0
            or source_index in seen_indexes
        ):
            raise ClinicalEntityError(
                "Clinical entity review order is invalid."
            )
        seen_indexes.add(source_index)
        if not include:
            continue
        original_text = row.get("original_text")
        retained.append(
            (
                source_index,
                {
                    "original_text": original_text,
                    "normalized_text": normalize_clinical_entity_text(
                        original_text
                    ),
                    "entity_type": row.get("entity_type"),
                    "assertion": row.get("assertion"),
                },
            )
        )
    retained.sort(key=lambda item: item[0])
    return [
        dict(entity)
        for entity in validate_clinical_entities(
            [entity for _, entity in retained]
        )
    ]


def _clinical_entity_review_changed() -> None:
    """Invalidate prior acceptance when a reviewer edits the draft."""

    st.session_state[CLINICAL_ENTITIES_KEY] = []
    st.session_state[CLINICAL_ENTITY_REVIEW_COMPLETE_KEY] = False
    _clear_analysis_result()


def _retain_hpo_candidates_for_reviewed_entities(
    entities: Sequence[Mapping[str, object]],
) -> None:
    """Keep model HPO suggestions linked to retained present findings."""

    accepted_phrases = {
        normalize_clinical_entity_text(entity["original_text"])
        for entity in entities
        if entity.get("entity_type") == "PHENOTYPE"
        and entity.get("assertion") == "PRESENT"
    }
    retained_candidates = []
    for candidate in st.session_state[HPO_MODEL_CANDIDATES_KEY]:
        try:
            source_phrase = normalize_clinical_entity_text(
                candidate.get("source_phrase_fa")
            )
        except ClinicalEntityError:
            continue
        if source_phrase in accepted_phrases:
            retained_candidates.append(candidate)
    st.session_state[HPO_MODEL_CANDIDATES_KEY] = retained_candidates
    st.session_state.pop(HPO_CANDIDATE_EDITOR_KEY, None)


def _render_clinical_entity_review() -> bool:
    """Render separated reviewer controls and return acceptance state."""

    drafts = st.session_state[CLINICAL_ENTITY_DRAFTS_KEY]
    if not drafts:
        return bool(st.session_state[CLINICAL_ENTITY_REVIEW_COMPLETE_KEY])

    with st.container(border=True):
        st.markdown("#### Clinical entity review")
        st.caption("Review every extracted mention before analysis.")
        edited_groups: list[pd.DataFrame] = []
        for entity_type, heading, editor_key in (
            (
                "PHENOTYPE",
                "Observed findings",
                CLINICAL_ENTITY_FINDINGS_EDITOR_KEY,
            ),
            (
                "DISEASE",
                "Disease/context mentions",
                CLINICAL_ENTITY_DISEASE_EDITOR_KEY,
            ),
        ):
            rows = [
                row for row in drafts if row["entity_type"] == entity_type
            ]
            if not rows:
                continue
            st.markdown(f"**{heading}** ({len(rows)})")
            if entity_type == "PHENOTYPE":
                st.caption(
                    "Reviewer-accepted findings may be used for phenotype-based "
                    "analysis; assertion state remains visible."
                )
            else:
                st.caption(
                    "Context only — disease mentions do not directly become "
                    "variant evidence."
                )
            edited_groups.append(
                st.data_editor(
                    pd.DataFrame(rows),
                    key=editor_key,
                    hide_index=True,
                    num_rows="fixed",
                    disabled=["source_index", "entity_type", "normalized_text"],
                    column_order=(
                        "include",
                        "original_text",
                        "entity_type",
                        "assertion",
                        "normalized_text",
                    ),
                    column_config={
                        "source_index": None,
                        "include": st.column_config.CheckboxColumn(
                            "Include", default=True
                        ),
                        "original_text": st.column_config.TextColumn(
                            "Entity text", required=True, max_chars=500
                        ),
                        "entity_type": st.column_config.TextColumn("Type"),
                        "assertion": st.column_config.SelectboxColumn(
                            "Assertion",
                            options=[
                                "PRESENT",
                                "SUSPECTED",
                                "NEGATED",
                                "HISTORICAL",
                            ],
                            required=True,
                        ),
                        "normalized_text": st.column_config.TextColumn(
                            "Normalized text"
                        ),
                    },
                    on_change=_clinical_entity_review_changed,
                )
            )

        is_review_complete = bool(
            st.session_state[CLINICAL_ENTITY_REVIEW_COMPLETE_KEY]
        )

        action_col, status_col = st.columns([1, 2])
        with action_col:
            accept_clicked = st.button(
                "Accept clinical entities",
                key="accept_clinical_entities",
                type="primary" if not is_review_complete else "secondary",
                icon=":material/check:",
                use_container_width=True,
            )

        if accept_clicked:
            rows = [
                row
                for edited in edited_groups
                for row in edited.to_dict(orient="records")
            ]
            try:
                accepted = accept_clinical_entity_review_rows(rows)
            except ClinicalEntityError as exc:
                with status_col:
                    st.error(
                        safe_ui_error_message(
                            exc,
                            context="clinical_entity_acceptance",
                        )
                    )
                return False
            st.session_state[CLINICAL_ENTITIES_KEY] = accepted
            st.session_state[CLINICAL_ENTITY_DRAFTS_KEY] = (
                prepare_clinical_entity_review_rows(accepted)
            )
            st.session_state[CLINICAL_ENTITY_REVIEW_COMPLETE_KEY] = True
            is_review_complete = True
            _retain_hpo_candidates_for_reviewed_entities(accepted)
            _clear_analysis_result()

        with status_col:
            if is_review_complete:
                st.success("Clinical entity review accepted.")
            else:
                st.warning("Clinical entities are awaiting reviewer confirmation.")

        return is_review_complete


def _render_phenotype_extraction(phenotype_model: str) -> None:
    """Render optional extraction, local validation, editing, and acceptance."""

    st.markdown("**Optional Persian clinical entity extraction**")
    st.caption(
        "Enter only a short de-identified clinical description. Findings and "
        "disease/context mentions remain separate and are not used until "
        "you explicitly review them. HPO suggestions are also checked "
        "against the installed local ontology. "
        f"Selected model: {phenotype_model}."
    )
    clinical_text = st.text_area(
        "Persian clinical description",
        key="phenotype_clinical_text_fa",
        max_chars=4_000,
        height=120,
        placeholder="شرح کوتاه و بدون نام یا شناسه بیمار",
        on_change=_clear_hpo_candidate_draft,
    )
    if st.button(
        "Extract clinical entities",
        key="extract_hpo_candidates",
        icon=":material/auto_awesome:",
        disabled=not clinical_text.strip(),
    ):
        try:
            with st.spinner("Extracting and validating clinical entities..."):
                extraction = extract_hpo_candidates(
                    clinical_text,
                    model=phenotype_model,
                )
                validation = validate_hpo_candidates(
                    extraction["phenotype_candidates"]
                )
        except (
            ClinicalDataPrivacyError,
            HPODataError,
            LLMError,
            PhenotypeError,
            PhenotypeExtractionError,
            PhenotypeSelectionError,
        ) as exc:
            _clear_hpo_candidate_draft()
            st.error(
                safe_ui_error_message(
                    exc,
                    context="phenotype_extraction",
                )
            )
        else:
            extracted_entities = deepcopy(
                extraction.get("clinical_entities", [])
            )
            st.session_state[CLINICAL_ENTITIES_KEY] = []
            st.session_state[CLINICAL_ENTITY_DRAFTS_KEY] = (
                prepare_clinical_entity_review_rows(extracted_entities)
            )
            st.session_state[CLINICAL_ENTITY_REVIEW_COMPLETE_KEY] = (
                not bool(extracted_entities)
            )
            st.session_state[PHENOTYPE_EXTRACTION_PROVENANCE_KEY] = {
                "schema_version": extraction["schema_version"],
                "task": extraction["task"],
                "prompt_version": extraction["prompt_version"],
                "model": extraction["model"],
                "candidate_hpo_ids": [
                    candidate["hpo_id"]
                    for candidate in extraction["phenotype_candidates"]
                ],
            }
            st.session_state[PHENOTYPE_NON_HPO_MENTIONS_KEY] = {
                "Unmapped clinical phrases": extraction[
                    "unmapped_clinical_phrases"
                ]
            }
            st.session_state[HPO_MODEL_CANDIDATES_KEY] = [
                {"include": True, **candidate}
                for candidate in validation["validated_candidates"]
            ]
            st.session_state[HPO_MODEL_REJECTIONS_KEY] = list(
                validation["rejected_candidates"]
            )
            st.session_state.pop(HPO_CANDIDATE_EDITOR_KEY, None)
            st.session_state.pop(CLINICAL_ENTITY_FINDINGS_EDITOR_KEY, None)
            st.session_state.pop(CLINICAL_ENTITY_DISEASE_EDITOR_KEY, None)
            if (
                not validation["validated_candidates"]
                and not validation["rejected_candidates"]
            ):
                st.info(
                    "No supported HPO candidates were returned. Manual HPO "
                    "selection remains available."
                )

    for label, mentions in st.session_state[
        PHENOTYPE_NON_HPO_MENTIONS_KEY
    ].items():
        if mentions:
            st.info(
                f"{label}: "
                + " | ".join(
                    mention["source_phrase_fa"] for mention in mentions
                )
            )

    entity_review_complete = _render_clinical_entity_review()

    rejected_candidates = st.session_state[HPO_MODEL_REJECTIONS_KEY]
    if rejected_candidates:
        rejected_ids = ", ".join(
            candidate["hpo_id"] or "invalid ID"
            for candidate in rejected_candidates
        )
        st.warning(
            "Excluded model suggestions that did not pass local ontology "
            f"validation: {rejected_ids}."
        )

    candidates = st.session_state[HPO_MODEL_CANDIDATES_KEY]
    if not candidates:
        return
    if not entity_review_complete:
        st.info(
            "Review the extracted clinical entities before accepting linked "
            "HPO candidates."
        )
        return
    st.caption(
        "Edit identifiers or source phrases, remove rows, and choose which "
        "terms to include. The complete edited selection is revalidated "
        "locally when accepted."
    )
    edited = st.data_editor(
        pd.DataFrame(candidates),
        key=HPO_CANDIDATE_EDITOR_KEY,
        hide_index=True,
        num_rows="dynamic",
        disabled=["label"],
        column_order=(
            "include",
            "hpo_id",
            "label",
            "source_phrase_fa",
        ),
        column_config={
            "include": st.column_config.CheckboxColumn(
                "Include",
                default=True,
            ),
            "hpo_id": st.column_config.TextColumn(
                "HPO ID",
                required=True,
                max_chars=10,
            ),
            "label": st.column_config.TextColumn(
                "Local ontology label"
            ),
            "source_phrase_fa": st.column_config.TextColumn(
                "Persian source phrase",
                required=True,
                max_chars=500,
            ),
        },
    )
    if not st.button(
        "Accept HPO candidates",
        key="accept_hpo_candidates",
        type="primary",
        icon=":material/check:",
    ):
        return

    selected_rows = [
        row
        for row in edited.to_dict(orient="records")
        if row.get("include") is True
    ]
    candidate_rows = [
        {
            "hpo_id": row.get("hpo_id"),
            "label": row.get("label"),
            "source_phrase_fa": row.get("source_phrase_fa"),
        }
        for row in selected_rows
    ]
    existing_ids = [
        term["id"] for term in st.session_state[SELECTED_HPO_KEY]
    ]
    try:
        accepted = accept_hpo_candidates(
            candidate_rows,
            existing_hpo_ids=existing_ids,
        )
    except (HPODataError, PhenotypeSelectionError) as exc:
        st.error(
            safe_ui_error_message(
                exc,
                context="phenotype_acceptance",
            )
        )
        return

    st.session_state[SELECTED_HPO_KEY] = accepted
    st.session_state[HPO_MODEL_CANDIDATES_KEY] = []
    st.session_state[HPO_MODEL_REJECTIONS_KEY] = []
    _clear_analysis_result()
    st.success("Accepted the locally validated HPO candidate set.")
    st.rerun()


def _render_manual_hpo_selection() -> None:
    """Render supplemental local HPO search and selection controls."""

    with st.expander(
        "Add phenotype manually",
        expanded=False,
        icon=":material/add:",
    ):
        st.caption(
            "Optionally supplement the reviewed findings by searching the "
            "installed Human Phenotype Ontology."
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
                    st.rerun()
                elif choice is not None:
                    st.info(f"{choice['id']} is already selected.")


def _render_accepted_hpo_terms() -> None:
    """Show the reviewer-approved HPO context without a legacy duplicate."""

    selected_terms = st.session_state[SELECTED_HPO_KEY]
    if not selected_terms:
        return

    st.markdown("**Accepted HPO terms**")
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


def _render_hpo_picker(phenotype_model: str) -> None:
    """Render clinical entity review and supplemental HPO controls."""

    with st.container(border=True):
        st.subheader("Phenotypes")
        st.caption(
            "Phenotypes are used for phenotype-based analysis. Disease "
            "mentions are retained as clinical context and are not used as "
            "direct variant evidence."
        )
        _render_phenotype_extraction(phenotype_model)
        _render_accepted_hpo_terms()
        _render_manual_hpo_selection()


def _task_models_changed() -> None:
    """Clear unaccepted HPO candidate draft when either task-specific model changes.

    Changing the phenotype or interpretation model does NOT discard a
    completed analysis result — the existing report remains visible until
    the user submits a new analysis.
    """

    _clear_hpo_candidate_draft()
    # Reset preflight badges so stale results do not linger for the new model.
    st.session_state[LLM_PREFLIGHT_PHENOTYPE_KEY] = None
    st.session_state[LLM_PREFLIGHT_VARIANT_KEY] = None


def _run_preflight_check(model: str, state_key: str) -> None:
    """Run one preflight check and store the result in session state.

    Never raises.  Any exception is caught and stored as a failed result.
    """
    try:
        result = check_llm_connectivity(model)
    except Exception:  # noqa: BLE001
        from backend.llm_preflight import LLMPreflightResult
        from datetime import datetime, timezone
        result = LLMPreflightResult(
            ok=False,
            model=model,
            failure_category="Request failed",
            response_ms=None,
            checked_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        )
    st.session_state[state_key] = result


def _render_preflight_health(
    result: "LLMPreflightResult | None",
) -> None:
    """Render the connection-health row inside a model card.

    Three visual states:
    - Not checked: neutral grey text.
    - Functional:  green dot, latency, and last-checked time.
    - Non-functional: red dot and safe failure category.
    """
    if result is None:
        st.markdown(
            "<span style='color:#888;font-size:0.82rem;'>"
            "⬤ &nbsp;Not checked yet</span>",
            unsafe_allow_html=True,
        )
        return

    if result.ok:
        latency = (
            f"Response: {result.response_ms} ms"
            if result.response_ms is not None
            else ""
        )
        checked = ""
        if result.checked_at:
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(
                    result.checked_at.replace("Z", "+00:00")
                )
                checked = f"Checked: {dt.strftime('%H:%M')}"
            except ValueError:
                pass
        detail = "  ·  ".join(p for p in (latency, checked) if p)
        st.markdown(
            "<span style='color:#00c853;font-size:0.85rem;font-weight:600;'>"
            "⬤ Functional</span>"
            + (
                f"<span style='color:#888;font-size:0.78rem;'>&nbsp;&nbsp;{detail}</span>"
                if detail else ""
            ),
            unsafe_allow_html=True,
        )
    else:
        category = result.failure_category or "Request failed"
        st.markdown(
            "<span style='color:#ff1744;font-size:0.85rem;font-weight:600;'>"
            f"⬤ Non-functional</span>"
            "<span style='color:#ff6e6e;font-size:0.78rem;'>"
            f"&nbsp;&nbsp;{category}</span>",
            unsafe_allow_html=True,
        )


def _render_model_preflight_card(
    *,
    title: str,
    selector_label: str,
    selector_key: str,
    selector_help: str,
    selector_placeholder: str,
    configured_default: str,
    provider_models: tuple[str, ...],
    preflight_key: str,
    reload_button_key: str,
    job_active: bool,
) -> str | None:
    """Render one self-contained model health card and return the selected model.

    The card contains:
    - Section title
    - Model selectbox
    - Connection health badge (green / red / neutral)
    - Latency and last-checked time (on success)
    - Reload check button (disabled while a job is running)
    """
    st.markdown(
        f"<p style='font-size:0.78rem;font-weight:600;text-transform:uppercase;"
        f"letter-spacing:0.06em;color:#aaa;margin-bottom:2px;'>{title}</p>",
        unsafe_allow_html=True,
    )
    selected_model = st.selectbox(
        selector_label,
        _task_model_options(
            configured_default,
            provider_models,
            st.session_state.get(selector_key),
        ),
        key=selector_key,
        placeholder=selector_placeholder,
        filter_mode="contains",
        accept_new_options=True,
        disabled=job_active,
        on_change=_task_models_changed,
        help=selector_help,
        label_visibility="collapsed",
    )
    _render_preflight_health(st.session_state.get(preflight_key))
    st.caption(
        "Preflight connectivity test (does not use clinical data)",
        help=(
            "Sends a minimal anonymous request to verify that the selected "
            "model is reachable and the API key is accepted. "
            "No patient information is transmitted."
        ),
    )
    if st.button(
        "Reload check",
        key=reload_button_key,
        icon=":material/wifi_tethering:",
        disabled=job_active,
        help="Re-run the connectivity check for this model",
        use_container_width=True,
    ):
        _run_preflight_check(
            st.session_state.get(selector_key, ""),
            preflight_key,
        )
    return selected_model


def _render_task_model_selectors() -> tuple[str, str]:
    """Render one independent model-health card for each accepted LLM task."""

    with st.container(border=True):
        st.subheader("Task-specific models")
        job_active = _analysis_job() is not None

        # Provider catalog loader (unchanged behaviour)
        if st.button(
            "Load models from provider",
            key="load_provider_models",
            icon=":material/refresh:",
            disabled=job_active,
        ):
            _provider_llm_models.clear()
            provider_models, provider_error = _provider_llm_models()
            st.session_state[LLM_PROVIDER_MODELS_KEY] = provider_models
            st.session_state[LLM_PROVIDER_MODELS_ERROR_KEY] = provider_error
        provider_models = tuple(st.session_state[LLM_PROVIDER_MODELS_KEY])
        if provider_models:
            st.caption(
                f"Loaded {len(provider_models)} current provider models. "
                "The provider catalog does not include prices or clinical "
                "suitability metadata."
            )
        elif st.session_state[LLM_PROVIDER_MODELS_ERROR_KEY]:
            st.warning(
                "The provider model catalog is unavailable. Configured "
                "defaults and custom model IDs remain available."
            )
        else:
            st.caption(
                "Configured task defaults are shown. Load the provider "
                "catalog or enter another supported model identifier."
            )

        st.divider()

        phenotype_column, interpretation_column = st.columns(2, gap="large")
        with phenotype_column:
            with st.container(border=True):
                phenotype_model = _render_model_preflight_card(
                    title="Phenotype Extraction",
                    selector_label="Phenotype Extraction Model",
                    selector_key=PHENOTYPE_MODEL_KEY,
                    selector_help=(
                        "Used only for the optional de-identified Persian "
                        "clinical-text to HPO-candidate task."
                    ),
                    selector_placeholder="Select or enter a phenotype model",
                    configured_default=settings.PHENOTYPE_EXTRACTION_MODEL,
                    provider_models=provider_models,
                    preflight_key=LLM_PREFLIGHT_PHENOTYPE_KEY,
                    reload_button_key="preflight_reload_phenotype",
                    job_active=job_active,
                )
        with interpretation_column:
            with st.container(border=True):
                variant_model = _render_model_preflight_card(
                    title="Variant Interpretation",
                    selector_label="Variant Interpretation Model",
                    selector_key=VARIANT_MODEL_KEY,
                    selector_help=(
                        "Selected once for every variant; deterministic "
                        "conflict status remains evidence context only."
                    ),
                    selector_placeholder="Select or enter an interpretation model",
                    configured_default=settings.VARIANT_INTERPRETATION_MODEL,
                    provider_models=provider_models,
                    preflight_key=LLM_PREFLIGHT_VARIANT_KEY,
                    reload_button_key="preflight_reload_variant",
                    job_active=job_active,
                )

    return phenotype_model, variant_model


def _is_supported_upload_filename(filename: str) -> bool:
    """Return whether an uploaded filename has a supported suffix."""

    lowered_name = filename.casefold()
    return lowered_name.endswith(
        (".vcf", ".vcf.gz", ".xlsx")
    )


def _manual_variant_table() -> pd.DataFrame:
    """Return blank rows up to the centralized analysis limit."""

    return pd.DataFrame(
        {
            "chrom": pd.Series(
                [pd.NA] * MAX_VARIANTS_PER_ANALYSIS,
                dtype="string",
            ),
            "pos": pd.Series(
                [pd.NA] * MAX_VARIANTS_PER_ANALYSIS,
                dtype="Int64",
            ),
            "ref": pd.Series(
                [""] * MAX_VARIANTS_PER_ANALYSIS,
                dtype="string",
            ),
            "alt": pd.Series(
                [""] * MAX_VARIANTS_PER_ANALYSIS,
                dtype="string",
            ),
            "qual": pd.Series(
                [pd.NA] * MAX_VARIANTS_PER_ANALYSIS,
                dtype="Float64",
            ),
            "filter": pd.Series(
                [""] * MAX_VARIANTS_PER_ANALYSIS,
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
    """Render row-aware inputs up to the centralized analysis limit."""

    table = _manual_variant_table()
    position_errors: list[str] = []
    for row_index in range(MAX_VARIANTS_PER_ANALYSIS):
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
    phenotype_model: str,
    llm_model: str,
    excel_input_records: list[dict[str, object]] | None = None,
) -> AnalysisSubmission | None:
    """Validate frontend presence rules and build one submission."""

    if (
        st.session_state.get(CLINICAL_ENTITY_DRAFTS_KEY)
        and not st.session_state.get(CLINICAL_ENTITY_REVIEW_COMPLETE_KEY)
    ):
        st.error(
            "Review and accept or reject the extracted clinical entities "
            "before starting analysis."
        )
        return None
    phenotype_ids = [
        term["id"] for term in st.session_state[SELECTED_HPO_KEY]
    ]
    if input_mode == VCF_INPUT_MODE:
        if uploaded_vcf is None:
            st.error(
                "Upload a .vcf, .vcf.gz, or .xlsx file before analysis."
            )
            return None
        filename = str(getattr(uploaded_vcf, "name", ""))
        if not _is_supported_upload_filename(filename):
            st.error(
                "The uploaded file must end in .vcf, .vcf.gz, or .xlsx."
            )
            return None
        return {
            "uploaded_vcf": (
                None if excel_input_records is not None else cast(UploadedVCF, uploaded_vcf)
            ),
            "manual_variants": None,
            "excel_input_records": excel_input_records,
            "phenotypes": phenotype_ids,
            "clinical_entities": deepcopy(
                st.session_state.get(CLINICAL_ENTITIES_KEY, [])
            ),
            "input_type": (
                "excel"
                if filename.casefold().endswith(".xlsx")
                else (
                    "vcf_gz"
                    if filename.casefold().endswith(".vcf.gz")
                    else "vcf"
                )
            ),
            "phenotype_extraction_model": phenotype_model,
            "phenotype_extraction_provenance": st.session_state.get(
                PHENOTYPE_EXTRACTION_PROVENANCE_KEY
            ),
            "llm_model": llm_model,
        }

    try:
        normalized_variants = _normalize_manual_table(manual_table)
    except ValueError as exc:
        st.error(safe_ui_error_message(exc, context="manual_input"))
        return None

    return {
        "uploaded_vcf": None,
        "manual_variants": normalized_variants,
        "excel_input_records": None,
        "phenotypes": phenotype_ids,
        "clinical_entities": deepcopy(
            st.session_state.get(CLINICAL_ENTITIES_KEY, [])
        ),
        "input_type": "manual",
        "phenotype_extraction_model": phenotype_model,
        "phenotype_extraction_provenance": st.session_state.get(
            PHENOTYPE_EXTRACTION_PROVENANCE_KEY
        ),
        "llm_model": llm_model,
    }


def _xlsx_upload_payload(uploaded_vcf: object) -> bytes:
    """Read one Streamlit upload only for local workbook inspection."""

    payload = getattr(uploaded_vcf, "getvalue", lambda: None)()
    if not isinstance(payload, bytes):
        raise ExcelProcessingError("The Excel upload is empty or invalid.")
    return payload


def _xlsx_worksheet_changed() -> None:
    """Prevent a row selection from one worksheet applying to another."""

    st.session_state.pop("xlsx_selected_source_rows", None)
    _clear_analysis_result()


def _render_xlsx_row_selection(
    uploaded_vcf: object,
) -> list[dict[str, object]] | None:
    """Require an explicit worksheet and 1–10 source rows before analysis."""

    try:
        payload = _xlsx_upload_payload(uploaded_vcf)
        upload_identity = sha256(payload).hexdigest()
        if st.session_state.get("xlsx_upload_identity") != upload_identity:
            st.session_state["xlsx_upload_identity"] = upload_identity
            st.session_state.pop("xlsx_selected_worksheet", None)
            st.session_state.pop("xlsx_selected_source_rows", None)
        worksheets = discover_excel_worksheets(payload)
    except ExcelProcessingError as exc:
        st.error(safe_ui_error_message(exc, context="excel_upload"))
        return None

    st.caption(
        "Review the uploaded source rows before analysis. Source FILTER and "
        "quality values are retained as provenance; they do not select, rank, "
        "or exclude candidates."
    )
    worksheet_names = [str(item["name"]) for item in worksheets]
    selected_worksheet = st.selectbox(
        "Excel worksheet",
        worksheet_names,
        index=None,
        placeholder="Select a worksheet",
        key="xlsx_selected_worksheet",
        on_change=_xlsx_worksheet_changed,
    )
    if selected_worksheet is None:
        st.info("Select a worksheet to review its source rows.")
        return None
    try:
        records = parse_excel_input_records(
            payload,
            worksheet_name=selected_worksheet,
        )
    except ExcelProcessingError as exc:
        st.error(safe_ui_error_message(exc, context="excel_upload"))
        return None

    worksheet_count = next(
        int(item["candidate_count"])
        for item in worksheets
        if item["name"] == selected_worksheet
    )
    st.caption(f"{worksheet_count} source candidate rows in {selected_worksheet}.")
    st.dataframe(source_row_display_rows(records), hide_index=True)
    selected_rows = st.multiselect(
        "Selected source rows",
        [record["row"] for record in records],
        format_func=lambda row: f"Worksheet {selected_worksheet}, row {row}",
        placeholder="Select 1–10 source rows",
        key="xlsx_selected_source_rows",
        on_change=_clear_analysis_result,
    )
    try:
        selected_records = select_excel_input_records(records, selected_rows)
    except XLSXSelectionError as exc:
        if selected_rows:
            st.error(
                safe_ui_error_message(
                    exc,
                    context="excel_selection",
                )
            )
        else:
            st.info("Select 1–10 source rows before analysis.")
        return None
    st.info(selected_source_rows_message(len(selected_records)))
    with st.expander("Selected source rows", expanded=False):
        st.dataframe(source_row_display_rows(selected_records), hide_index=True)
    return selected_records


def _render_variant_input(
    phenotype_model: str,
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
                f"Enter up to {MAX_VARIANTS_PER_ANALYSIS} already-filtered "
                "variants. "
                "CHROM, POS, REF, and ALT are required. Every row "
                "is analyzed in order and receives a Draft Variant Report."
            )
            manual_table, position_errors = (
                _render_manual_variant_table()
            )

        excel_input_records: list[dict[str, object]] | None = None
        if input_mode == VCF_INPUT_MODE:
            uploaded_vcf = st.file_uploader(
                    "Variant file",
                    type=("vcf", "gz", "xlsx"),
                    key="vcf_upload",
                    help=(
                        "Accepted formats: .vcf, .vcf.gz, and .xlsx. "
                        "For Excel, select a worksheet and source rows before "
                        "analysis; required "
                        "columns are CHROM, POS, REF, and ALT, while QUAL "
                        "and FILTER are optional. "
                        f"The filtered input must produce 1 to "
                        f"{MAX_VARIANTS_PER_ANALYSIS} variants. Every row is "
                        "analyzed in order and receives a Draft Variant Report. "
                        f"Maximum size: "
                        f"{settings.MAX_UPLOAD_BYTES // 1_000_000} MB."
                    ),
            )
            filename = str(getattr(uploaded_vcf, "name", ""))
            if filename.casefold().endswith(".xlsx"):
                excel_input_records = _render_xlsx_row_selection(uploaded_vcf)

        with st.form("analysis_input_form", border=False):

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
                    disabled=(
                        job_present
                        or bool(position_errors)
                        or (
                            input_mode == VCF_INPUT_MODE
                            and uploaded_vcf is not None
                            and str(getattr(uploaded_vcf, "name", "")).casefold().endswith(".xlsx")
                            and excel_input_records is None
                        )
                    ),
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
                phenotype_model,
                llm_model,
                excel_input_records,
            )
    return None


def _result_status(
    result: PipelineResult,
) -> tuple[str, str, bool]:
    """Return the final status label, state, and expansion mode."""

    if build_identity_resolution_failure_presentation(result) is not None:
        return "Analysis could not proceed", "error", True
    if result["status"] == "success":
        return "Analysis completed", "complete", False
    if (
        result["status"] == "partial"
        and result["current_stage"] == "completed"
    ):
        return "Analysis completed partially", "complete", True
    if result["status"] == "partial":
        return "Analysis stopped with partial results", "error", True
    if result["status"] == "error":
        current_stage = result["current_stage"]
        stage_label = (
            _pipeline_stage_label(current_stage, result)
            if current_stage in PIPELINE_STAGE_LABELS
            else current_stage.replace("_", " ").title()
        )
        return f"Analysis failed during {stage_label}", "error", True
    return "Analysis in progress", "running", True


def _pipeline_stage_label(stage: str, result: PipelineResult) -> str:
    """Return an input-aware label without misnaming Excel as VCF."""

    if stage == "vcf_processing":
        input_type = result["analysis_context"]["input_type"]
        return {
            "excel": "Excel input",
            "manual": "Manual variant input",
            "vcf": "VCF input",
            "vcf_gz": "Compressed VCF input",
        }.get(input_type, PIPELINE_STAGE_LABELS[stage])
    return PIPELINE_STAGE_LABELS[stage]


def _write_stage_records(target: object, result: PipelineResult) -> None:
    """Write all non-pending stage records to a status container."""

    for record in result["stages"]:
        if record["status"] == "pending":
            continue
        label = _pipeline_stage_label(record["stage"], result)
        icon = PIPELINE_STATUS_ICONS[record["status"]]
        message = record["message"] or record["status"].title()
        if record["stage"] == "input" and record["status"] == "success":
            message = input_validation_message(
                result["analysis_context"]["input_type"]
            )
        target.write(f"{icon} **{label}:** {message}")


def _render_pipeline_issues(result: PipelineResult) -> None:
    """Render frontend-safe pipeline issues without tracebacks."""

    for issue in result["errors"]:
        stage = issue["stage"]
        stage_label = (
            _pipeline_stage_label(stage, result)
            if stage in PIPELINE_STAGE_LABELS
            else "Analysis"
        )
        message = f"{stage_label}: {issue['message']}"
        if issue["recoverable"]:
            st.warning(message)
        else:
            st.error(message)


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


def _render_trace_snapshot(snapshot: object) -> None:
    """Render the authoritative runtime execution journey when available."""

    render_execution_trace(snapshot)


def _render_analysis_summary(result: PipelineResult) -> None:
    """Render the concise product outcome before report/provider details."""

    identity_failure = build_identity_resolution_failure_presentation(result)
    if identity_failure is not None:
        with st.container(border=True):
            st.subheader(identity_failure["headline"])
            st.warning(identity_failure["message"])
            with st.container(horizontal=True):
                st.metric(
                    "Selected source rows",
                    identity_failure["selected_count"],
                    border=True,
                )
                st.metric(
                    "Canonical variants",
                    identity_failure["canonical_count"],
                    border=True,
                )
                st.metric(
                    "Rows requiring attention",
                    identity_failure["unresolved_count"],
                    border=True,
                )
            st.dataframe(
                identity_failure["rows"],
                hide_index=True,
                key="identity_resolution_failure_rows",
            )
        return
    summary = build_analysis_summary(result)
    variant_count = summary["variants_analyzed"]
    report_count = summary["draft_reports_prepared"]
    ready_count = summary["reports_ready"]
    partial_count = summary["partial_source_coverage"]
    attention_count = summary["interpretations_requiring_attention"]
    attention_label = (
        "interpretation requires"
        if attention_count == 1
        else "interpretations require"
    )
    with st.container(border=True):
        st.subheader(summary["headline"])
        st.caption(summary["input_message"])
        st.write(
            f"**{variant_count}** "
            f"{'variant' if variant_count == 1 else 'variants'} analyzed"
        )
        st.write(
            f"**{report_count}** draft "
            f"{'report' if report_count == 1 else 'reports'} prepared"
        )
        st.write(
            f"**{ready_count}** "
            f"{'report' if ready_count == 1 else 'reports'} ready"
        )
        st.write(
            f"**{partial_count}** "
            f"{'variant has' if partial_count == 1 else 'variants have'} "
            "partial source coverage"
        )
        st.write(
            f"**{attention_count}** "
            f"{attention_label} attention"
        )


def _start_submission(
    submission: AnalysisSubmission,
) -> None:
    """Start one cancellable background analysis."""

    if _analysis_job() is not None:
        return
    readiness_snapshot = current_provider_readiness_snapshot()
    execution_trace = AnalysisExecutionTrace()

    def runner(
        progress_callback: PipelineProgressCallback,
    ) -> PipelineResult:
        kwargs: dict[str, object] = {
            "uploaded_vcf": submission["uploaded_vcf"],
            "manual_variants": submission["manual_variants"],
            "phenotypes": submission["phenotypes"],
            "clinical_entities": submission["clinical_entities"],
            "input_type": submission["input_type"],
            "phenotype_extraction_model": submission[
                "phenotype_extraction_model"
            ],
            "phenotype_extraction_provenance": submission[
                "phenotype_extraction_provenance"
            ],
            "llm_model": submission["llm_model"],
            "readiness_snapshot": readiness_snapshot,
            "progress_callback": progress_callback,
            "execution_trace": execution_trace,
        }
        if submission["excel_input_records"] is not None:
            kwargs["excel_input_records"] = submission["excel_input_records"]
        return execute_analysis(**kwargs)

    _discard_analysis_result()
    st.session_state[ANALYSIS_NOTICE_KEY] = None
    job = AnalysisJob(runner, execution_trace=execution_trace)
    try:
        recovery_request = prepare_analysis_recovery_request(
            uploaded_vcf=submission["uploaded_vcf"],
            manual_variants=submission["manual_variants"],
            phenotypes=submission["phenotypes"],
            clinical_entities=submission["clinical_entities"],
            input_type=submission["input_type"],
            phenotype_extraction_model=submission[
                "phenotype_extraction_model"
            ],
            phenotype_extraction_provenance=submission[
                "phenotype_extraction_provenance"
            ],
            llm_model=submission["llm_model"],
            excel_input_records=submission["excel_input_records"],
        )
        token = register_analysis_job(
            job,
            recovery_request=recovery_request,
        )
    except FrontendExecutionError as exc:
        st.session_state[ANALYSIS_NOTICE_KEY] = safe_ui_error_message(
            exc,
            context="analysis_start",
        )
        st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "error"
        return
    st.session_state[ANALYSIS_JOB_KEY] = job
    st.session_state[ANALYSIS_JOB_TOKEN_KEY] = token
    st.query_params[ANALYSIS_JOB_QUERY_PARAM] = token
    try:
        job.start()
    except RuntimeError:
        release_registered_analysis_job(token)
        st.session_state[ANALYSIS_JOB_KEY] = None
        st.session_state[ANALYSIS_JOB_TOKEN_KEY] = None
        _clear_query_param(ANALYSIS_JOB_QUERY_PARAM)
        st.session_state[ANALYSIS_NOTICE_KEY] = (
            "The analysis worker could not be started."
        )
        st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "error"


def _finish_analysis_job(job: AnalysisJob) -> None:
    """Transfer one terminal job outcome into session state."""

    view = job.view()
    st.session_state[ANALYSIS_EXECUTION_TRACE_KEY] = deepcopy(
        view.execution_trace
    )
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
            ANALYSIS_TIMEOUT_MESSAGE
            if view.error_message == ANALYSIS_TIMEOUT_MESSAGE
            else safe_ui_error_message(
                FrontendExecutionError(
                    view.error_message or "analysis worker failed"
                ),
                context="analysis_worker",
            )
        )
        st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "error"

    token = st.session_state.get(ANALYSIS_JOB_TOKEN_KEY)
    st.session_state[ANALYSIS_JOB_TOKEN_KEY] = None
    durable_result = False
    if view.state == "completed" and view.result is not None:
        analysis_id = view.result.get("analysis_id")
        if isinstance(analysis_id, str):
            durable_result = save_last_session_analysis_id(analysis_id)
            if durable_result:
                st.query_params[ANALYSIS_RESULT_QUERY_PARAM] = analysis_id
            else:
                st.session_state[ANALYSIS_NOTICE_KEY] = (
                    "Analysis completed, but its saved state could not be "
                    "verified. Keep this page open or run the analysis again "
                    "before relying on restoration."
                )
                st.session_state[ANALYSIS_NOTICE_LEVEL_KEY] = "warning"
    release_registered_analysis_job(token)
    _clear_query_param(ANALYSIS_JOB_QUERY_PARAM)
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
    if view.state == "cancelling":
        status.info(
            "The current bounded operation will stop at the next safe "
            "cancellation point."
        )
    _render_trace_snapshot(view.execution_trace)


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
        st.divider()
        render_provider_readiness(job_active=_analysis_job() is not None)
        st.divider()
        if st.button(
            "Evidence Graph",
            key="open_evidence_graph",
            icon=":material/account_tree:",
            width="stretch",
        ):
            render_evidence_graph()

    st.subheader("Analysis workflow")
    _render_workflow_overview()
    st.divider()
    phenotype_model, variant_model = _render_task_model_selectors()
    _render_hpo_picker(phenotype_model)
    submission = _render_variant_input(phenotype_model, variant_model)
    st.divider()

    _render_restore_last_session()
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

    if pipeline_result is not None:
        st.divider()
        _render_analysis_summary(pipeline_result)
        report_tab, technical_tab = st.tabs(
            ["Clinical report review", "Analysis and provider details"]
        )
        with report_tab:
            render_evidence_review(
                pipeline_result,
                light_model=variant_model,
                strong_model=variant_model,
            )
        with technical_tab:
            _render_pipeline_status(pipeline_result)
            _render_trace_snapshot(
                st.session_state.get(ANALYSIS_EXECUTION_TRACE_KEY)
            )
            render_analysis_results(pipeline_result)
