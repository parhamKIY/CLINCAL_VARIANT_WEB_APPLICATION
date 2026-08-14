"""Pre-interpreted variant review and final-confirmation interface."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import cast

import streamlit as st

from backend.database import DatabaseError, save_pipeline_state
from backend.evidence_confirmation import ReviewedEvidencePackage
from backend.evidence_review import (
    EvidenceReviewError,
    EvidenceReviewReport,
    MAX_REVIEW_REPORT_BYTES,
    save_evidence_review_draft,
)
from backend.pipeline import (
    PipelineError,
    PipelineResult,
    acknowledge_unresolved_interpretation_inclusion,
    confirm_reviewed_evidence,
    finalize_reviewed_analysis,
    request_revised_variant_interpretation,
    retry_failed_variant_interpretation,
    select_final_interpretation_version,
    update_draft_variant_report,
)
from backend.report_data_projection import build_report_data_from_draft
from backend.report_docx import (
    ReportDocxError,
    render_report_data_docx,
    report_docx_filename,
)
from backend.variant_report import (
    DraftVariantReport,
    DraftVariantReportError,
    VariantReportContent,
    save_draft_variant_report,
    set_draft_variant_report_inclusion,
)
from frontend.report_preview import (
    render_draft_report_preview_pages,
    stable_allele_identity,
)
from frontend.report_viewer import render_final_clinical_report_viewer
from frontend.source_status import (
    build_reviewer_section_status,
    build_reviewer_source_status,
)
from frontend.technical_diagnostics import (
    ProviderDiagnostic,
    build_provider_diagnostics,
)
from frontend.variant_status import build_variant_status_cards
from frontend.warning_semantics import WarningNotice


REVIEW_DRAFTS_KEY = "evidence_review_drafts"
REVIEW_SIGNATURE_KEY = "evidence_review_signature"
REVIEW_VARIANT_KEY = "selected_evidence_review_variant"
REVIEW_PACKAGES_KEY = "evidence_review_packages"
REVIEW_NOTICE_KEY = "evidence_review_notice"
_REVIEW_WIDGET_PREFIX = "evidence_review_"

_NOTICE_STYLES = {
    "INFO": ("Info", "blue", ":material/info:"),
    "PARTIAL": ("Partial", "orange", ":material/warning:"),
    "ACTION REQUIRED": ("Action required", "red", ":material/error:"),
    "BLOCKING": ("Blocking", "red", ":material/block:"),
}


def interpretation_failure_message(failure_type: object) -> str:
    """Return reviewer-safe wording for a persisted model failure category."""

    if failure_type in {
        "request_timeout",
        "connection_error",
        "http_429",
        "http_5xx",
        "empty_response",
    }:
        return (
            "The interpretation service remained temporarily unavailable "
            "after bounded recovery."
        )
    if failure_type in {"output_schema_failure", "output_parse_failure"}:
        return (
            "The model returned a response, but no valid structured response "
            "remained after the repair attempt."
        )
    if failure_type in {"authentication_error", "configuration_error"}:
        return (
            "Interpretation could not run because the model provider "
            "configuration requires attention."
        )
    if failure_type == "safety_or_finish_failure":
        return "The model stopped before a usable interpretation was produced."
    return (
        "Interpretation could not be produced because the model workflow "
        "requires attention."
    )


def _render_variant_notice(
    variant_index: int,
    notice: WarningNotice,
) -> None:
    """Render one Stage 97 consequence notice without crash-like raw output."""

    label, color, icon = _NOTICE_STYLES[notice["severity"]]
    with st.container(horizontal=True, vertical_alignment="center"):
        st.badge(label, color=color, icon=icon)
        st.write(f"Variant {variant_index + 1}: {notice['message']}")


def _render_technical_diagnostics(
    variant_index: int,
    diagnostics: list[ProviderDiagnostic],
) -> None:
    """Render the Stage 98 developer drawer only when opened."""

    details = st.expander(
        "Show technical details",
        expanded=False,
        key=f"variant_provider_diagnostics_{variant_index}",
        icon=":material/monitoring:",
        on_change="rerun",
    )
    if getattr(details, "open", True):
        with details:
            if not diagnostics:
                st.caption("No provider diagnostic records are available.")
                return
            display_rows: list[dict[str, object]] = [
                {
                    **diagnostic,
                    "attempt_count": (
                        diagnostic["attempt_count"]
                        if diagnostic["attempt_count"] is not None
                        else "Not recorded"
                    ),
                    "latency_ms": (
                        diagnostic["latency_ms"]
                        if diagnostic["latency_ms"] is not None
                        else "Not recorded"
                    ),
                }
                for diagnostic in diagnostics
            ]
            st.dataframe(
                display_rows,
                column_order=(
                    "provider",
                    "variant_identity",
                    "status",
                    "attempt_count",
                    "latency_ms",
                    "fallback_used",
                    "failure_category",
                    "provider_note",
                ),
                column_config={
                    "provider": "Provider",
                    "variant_identity": "Variant identity",
                    "status": "Status",
                    "attempt_count": "Attempt count",
                    "latency_ms": "Latency (ms)",
                    "fallback_used": "Fallback used",
                    "failure_category": "Failure category",
                    "provider_note": "Provider-specific note",
                },
                hide_index=True,
                width="stretch",
                key=f"variant_provider_diagnostics_table_{variant_index}",
            )


def _render_variant_status_cards(result: PipelineResult) -> None:
    """Render Stage 96 variant-first status and warning cards."""

    st.markdown("### Variant status")
    for card in build_variant_status_cards(result):
        with st.container(border=True):
            st.markdown(f"**{card['heading']}**")
            st.write(f"Status: **{card['status']}**")
            for line in (
                card["annotation"],
                card["population"],
                card["clinvar"],
                card["phenotype"],
                card["interpretation"],
            ):
                st.write(line)
            for notice in card["notices"]:
                _render_variant_notice(card["variant_index"], notice)


def _render_selected_provider_diagnostics(
    result: PipelineResult,
    variant_index: int,
) -> None:
    """Render provider diagnostics for the selected variant on demand."""

    diagnostics_by_variant = build_provider_diagnostics(result)
    _render_technical_diagnostics(
        variant_index,
        diagnostics_by_variant.get(variant_index, []),
    )


def _select_review_variant(index: int) -> None:
    st.session_state[REVIEW_VARIANT_KEY] = index


def _request_report_edit(report_id: str) -> None:
    st.session_state[
        f"{_REVIEW_WIDGET_PREFIX}edit_requested_{report_id}"
    ] = True


def _close_report_edit(report_id: str) -> None:
    st.session_state[
        f"{_REVIEW_WIDGET_PREFIX}edit_requested_{report_id}"
    ] = False
    generation_key = (
        f"{_REVIEW_WIDGET_PREFIX}editor_generation_{report_id}"
    )
    st.session_state[generation_key] = (
        int(st.session_state.get(generation_key, 0)) + 1
    )


def _render_report_preview(
    report: DraftVariantReport,
    *,
    selected: int,
    total: int,
) -> None:
    """Render the report-first Stage 84 review surface."""

    pages = render_draft_report_preview_pages(report)
    identity = stable_allele_identity(report)
    summary = report["reviewed_report"]["variant_summary"]
    annotations = " · ".join(
        item
        for item in (
            summary.get("gene"),
            summary.get("hgvs_c"),
            summary.get("hgvs_p"),
        )
        if item
    )
    st.markdown(f"### Variant {selected + 1} of {total}")
    st.caption(identity + (f" · {annotations}" if annotations else ""))
    page_number = st.pagination(
        len(pages),
        default=1,
        max_visible_pages=len(pages),
        width="content",
        key=f"{_REVIEW_WIDGET_PREFIX}preview_page_{report['report_id']}",
    )
    st.caption(f"Report page {page_number} of {len(pages)}")
    st.html(pages[page_number - 1], width="stretch")
    with st.container(horizontal=True):
        st.button(
            "Previous",
            icon=":material/arrow_back:",
            disabled=selected == 0,
            on_click=_select_review_variant,
            args=(selected - 1,),
            key=f"{_REVIEW_WIDGET_PREFIX}previous_variant",
        )
        st.button(
            "Next",
            icon=":material/arrow_forward:",
            icon_position="right",
            disabled=selected >= total - 1,
            on_click=_select_review_variant,
            args=(selected + 1,),
            key=f"{_REVIEW_WIDGET_PREFIX}next_variant",
        )
        st.button(
            "Edit",
            icon=":material/edit:",
            help="Open the document-like report editor.",
            on_click=_request_report_edit,
            args=(report["report_id"],),
            key=f"{_REVIEW_WIDGET_PREFIX}edit_preview_{report['report_id']}",
        )


def clear_evidence_review_state() -> None:
    """Discard session drafts when their source analysis changes."""

    for key in list(st.session_state):
        if (
            key in {
                REVIEW_DRAFTS_KEY,
                REVIEW_SIGNATURE_KEY,
                REVIEW_VARIANT_KEY,
                REVIEW_PACKAGES_KEY,
                REVIEW_NOTICE_KEY,
            }
            or str(key).startswith(_REVIEW_WIDGET_PREFIX)
        ):
            st.session_state.pop(key, None)


def _initialize_drafts(
    reports: list[dict[str, object]],
    result: PipelineResult,
) -> list[EvidenceReviewReport]:
    signature = (
        str(result.get("analysis_id") or "unpersisted"),
        *(str(report.get("report_id", "")) for report in reports),
    )
    if st.session_state.get(REVIEW_SIGNATURE_KEY) != signature:
        st.session_state[REVIEW_SIGNATURE_KEY] = signature
        st.session_state[REVIEW_DRAFTS_KEY] = deepcopy(reports)
        packages_by_index = {
            package["variant_index"]: package
            for package in result.get("reviewed_evidence_packages", [])
        }
        st.session_state[REVIEW_PACKAGES_KEY] = {
            report["report_id"]: packages_by_index[report["variant_index"]]
            for report in reports
            if report["variant_index"] in packages_by_index
        }
        st.session_state.pop(REVIEW_VARIANT_KEY, None)
    return cast(
        list[EvidenceReviewReport],
        st.session_state[REVIEW_DRAFTS_KEY],
    )


def _variant_label(report: EvidenceReviewReport) -> str:
    original = report["original_machine_report"]
    variant = original["variant"]
    allele = ":".join(
        str(variant[field])
        for field in ("chrom", "pos", "ref", "alt")
    )
    gene = original.get("gene") or "gene unavailable"
    return (
        f"Variant {report['variant_index'] + 1} — "
        f"{allele} — {gene}"
    )


def _invalidate_confirmation(
    result: PipelineResult,
    report_id: str,
    variant_index: int,
    packages: dict[str, ReviewedEvidencePackage],
) -> None:
    """Remove a confirmation whenever its reviewed draft changes."""

    packages.pop(report_id, None)
    result["reviewed_evidence_packages"] = [
        package
        for package in result.get("reviewed_evidence_packages", [])
        if package.get("variant_index") != variant_index
    ]
    result["llm_routing_results"] = [
        item
        for item in result.get("llm_routing_results", [])
        if item.get("variant_index") != variant_index
    ]
    result["final_interpretation_report"] = None
    result["final_clinical_report"] = None
    result["workflow_state"] = (
        "awaiting_final_review"
        if result.get("variant_interpretation_results")
        else "awaiting_confirmation"
    )
    st.session_state.pop(
        f"{_REVIEW_WIDGET_PREFIX}privacy_{report_id}",
        None,
    )


def _persist_review_state(result: PipelineResult) -> bool:
    """Persist review progress when this analysis has a database identity."""

    if result.get("analysis_id") is None:
        return True
    try:
        save_pipeline_state(result)
    except DatabaseError:
        return False
    return True


def _set_notice(level: str, message: str) -> None:
    st.session_state[REVIEW_NOTICE_KEY] = (level, message)


def _render_notice() -> None:
    notice = st.session_state.pop(REVIEW_NOTICE_KEY, None)
    if not isinstance(notice, tuple) or len(notice) != 2:
        return
    level, message = notice
    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    else:
        st.error(message)


def _render_conflict_status(report: EvidenceReviewReport) -> None:
    audit = report["original_machine_report"].get("conflict_audit")
    pre_review = (
        audit.get("pre_review")
        if isinstance(audit, dict)
        else None
    )
    status = (
        pre_review.get("status")
        if isinstance(pre_review, dict)
        else None
    )
    severity = (
        pre_review.get("routing_severity")
        if isinstance(pre_review, dict)
        else None
    )
    if status == "conflict" and severity in {
        "moderate",
        "major",
        "critical",
    }:
        st.warning(f"Conflict detected before review — severity: {severity}.")
    else:
        st.success("No meaningful conflict detected before review.")


def _render_editor(
    report: EvidenceReviewReport,
    report_index: int,
    drafts: list[EvidenceReviewReport],
    result: PipelineResult,
) -> None:
    report_id = report["report_id"]
    editor_key = f"{_REVIEW_WIDGET_PREFIX}json_{report_id}"
    notes_key = f"{_REVIEW_WIDGET_PREFIX}notes_{report_id}"
    with st.form(f"{_REVIEW_WIDGET_PREFIX}form_{report_id}"):
        reviewed_text = st.text_area(
            "Reviewed evidence report (JSON)",
            value=json.dumps(
                report["reviewed_user_report"],
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            height=600,
            max_chars=MAX_REVIEW_REPORT_BYTES,
            key=editor_key,
            help=(
                "Every section is editable. New clinical, laboratory, "
                "family, or manual evidence fields may be added. Do not "
                "enter names, contact details, record numbers, or other "
                "direct identifiers."
            ),
        )
        notes_text = st.text_area(
            "Reviewer notes (one note per line)",
            value="\n".join(report["reviewer_notes"]),
            height=120,
            key=notes_key,
            help=(
                "Do not enter names, contact details, record numbers, "
                "dates of birth, or other protected health information."
            ),
        )
        with st.container(horizontal=True):
            save = st.form_submit_button(
                "Save draft",
                type="primary",
                icon=":material/save:",
            )
            reset = st.form_submit_button(
                "Reset to original",
                icon=":material/restart_alt:",
            )
    if not save and not reset:
        return
    try:
        if reset:
            reviewed = deepcopy(report["original_machine_report"])
            notes: list[str] = []
        else:
            reviewed = json.loads(reviewed_text)
            if not isinstance(reviewed, dict):
                raise EvidenceReviewError(
                    "The reviewed report must be a JSON object."
                )
            notes = [
                line.strip()
                for line in notes_text.splitlines()
                if line.strip()
            ]
        updated = save_evidence_review_draft(
            report,
            reviewed,
            notes,
        )
        drafts[report_index] = updated
        result["evidence_review_reports"][report_index] = dict(updated)
        packages = cast(
            dict[str, ReviewedEvidencePackage],
            st.session_state.setdefault(REVIEW_PACKAGES_KEY, {}),
        )
        _invalidate_confirmation(
            result,
            report_id,
            report["variant_index"],
            packages,
        )
        persisted = _persist_review_state(result)
    except (json.JSONDecodeError, EvidenceReviewError) as exc:
        st.error(f"Draft was not saved: {exc}")
        return
    action = "reset to the original evidence" if reset else "saved"
    if persisted:
        _set_notice("success", f"Draft {action}.")
    else:
        _set_notice(
            "warning",
            f"Draft {action} in this session, but database persistence failed.",
        )
    st.rerun()


def _render_history(report: EvidenceReviewReport) -> None:
    history = report["edit_history"]
    if not history:
        st.info("No draft edits have been saved.")
        return
    rows = [
        {
            "Timestamp": edit["timestamp"],
            "Path": edit["path"],
            "Change": edit["change_type"],
            "User added": edit["user_added"],
            "Old value": json.dumps(
                edit["old_value"],
                ensure_ascii=False,
                allow_nan=False,
            ),
            "New value": json.dumps(
                edit["new_value"],
                ensure_ascii=False,
                allow_nan=False,
            ),
        }
        for edit in history
    ]
    st.dataframe(rows, hide_index=True)


def _render_package_summary(
    package: ReviewedEvidencePackage,
) -> None:
    post_review = package["post_review_conflict"]
    status = post_review.get("status")
    st.success(f"Confirmed at {package['confirmed_at']}.")
    if status == "conflict":
        user_override = any(
            finding.get("conflict_type") == "user_override"
            for finding in post_review.get("findings", [])
            if isinstance(finding, dict)
        )
        label = (
            "Conflict after user edits"
            if user_override
            else "Conflict detected after review"
        )
        st.warning(
            f"{label} — severity: {post_review.get('routing_severity')}."
        )
    else:
        st.success("No meaningful conflict after review.")
    if package["user_added_evidence"]:
        st.caption(
            f"{len(package['user_added_evidence'])} user-added "
            "evidence field(s) are retained in the final review state."
        )


def _render_evidence_sections(content: VariantReportContent) -> None:
    """Render source findings with reviewer wording and retained provenance."""

    for section in content["evidence_sections"]:
        presented = build_reviewer_section_status(
            source=section["source"],
            status=section["status"],
            data_sources=content["data_sources"],
        )
        with st.expander(
            f"{section['source']} — {presented['category']}",
            expanded=presented["category"] in {
                "Evidence available",
                "Evidence rescue completed",
            },
        ):
            st.caption(presented["message"])
            if presented["recovery"]:
                st.caption(f"Recovery: {presented['recovery']}")
            if section["items"]:
                st.table(
                    [
                        {
                            "Finding": item["label"],
                            "Value": item["value"],
                        }
                        for item in section["items"]
                    ]
                )
            else:
                st.caption("No source finding was available.")


def _render_draft_variant_report(
    result: PipelineResult,
    variant_index: int,
) -> None:
    report = next(
        (
            item
            for item in result.get("draft_variant_reports", [])
            if item.get("variant_index") == variant_index
        ),
        None,
    )
    if report is None:
        st.error("No Draft Variant Report is available for this variant.")
        return
    content = report["reviewed_report"]
    summary = content["variant_summary"]
    st.markdown(f"### {summary['display_label']}")
    if content["reviewer_summary"]:
        st.info(content["reviewer_summary"])
    summary_rows = [
        {"Field": "Gene", "Value": summary.get("gene") or "Not available"},
        {"Field": "Transcript", "Value": summary.get("transcript") or "Not available"},
        {"Field": "Coding HGVS", "Value": summary.get("hgvs_c") or "Not available"},
        {"Field": "Protein HGVS", "Value": summary.get("hgvs_p") or "Not available"},
        {
            "Field": "Consequence",
            "Value": summary.get("consequence") or "Not available",
        },
        {"Field": "rsID", "Value": summary.get("rsid") or "Not available"},
    ]
    st.table(summary_rows)

    phenotype = content["phenotype_context"]
    st.markdown("#### Phenotype context")
    st.write(
        "Accepted HPO terms: "
        + (", ".join(phenotype["accepted_hpo_terms"]) or "None")
    )
    st.write(
        "Matched HPO terms: "
        + (", ".join(phenotype["matched_hpo_terms"]) or "None")
    )
    if phenotype["phenotype_score"] is not None:
        st.caption(f"Phenotype score: {phenotype['phenotype_score']}")
    for summary_item in phenotype["phenotype_to_gene_summary"]:
        st.write(f"- {summary_item}")
    for context in phenotype["disease_context"]:
        st.write(f"- {context}")

    fallback_messages = [
        provider.removeprefix("Fallback: ")
        for provider in content["provenance"]["providers"]
        if provider.startswith("Fallback: ")
    ]
    if fallback_messages:
        with st.container(border=True):
            st.markdown(
                ":material/swap_horiz: **Fallback evidence used**"
            )
            st.caption(
                "Primary and fallback sources remain distinct in report "
                "provenance."
            )
            for message in fallback_messages:
                st.write(f"- {message}")

    st.markdown("#### Evidence")
    _render_evidence_sections(content)

    conflict = content["conflict_summary"]
    st.markdown("#### Conflict summary")
    conflict_message = (
        f"Status: {conflict['status']} · Severity: {conflict['severity']}"
    )
    if conflict["detected"]:
        st.warning(conflict_message)
    else:
        st.success(conflict_message)
    for finding in conflict["findings"]:
        st.write(f"- {finding}")

    interpretation = content["variant_interpretation"]
    preliminary_status = interpretation.get("preliminary_classification_status")
    st.markdown("#### Preliminary classification")
    if interpretation["status"] != "success":
        st.caption("No preliminary classification is available for this interpretation.")
    elif preliminary_status == "classified":
        st.info(
            "Preliminary evidence-based classification: "
            f"{interpretation['preliminary_classification']}"
        )
    elif preliminary_status == "ambiguous":
        st.warning(
            "Preliminary evidence-based classification: "
            "Ambiguous — user review required"
        )
    else:
        st.caption(
            "No preliminary classification is available for this pre-Stage-124 analysis."
        )
    if interpretation.get("classification_rationale"):
        st.markdown("**Classification rationale**")
        st.write(interpretation["classification_rationale"])
    for limitation in interpretation.get("limitations", []):
        st.caption(f"Limitation: {limitation}")
    st.caption(
        "This preliminary LLM evidence synthesis requires qualified human review "
        "and is not an independent ACMG/AMP adjudication."
    )
    st.markdown("#### Variant interpretation")
    if interpretation["status"] == "failed":
        st.error(
            interpretation_failure_message(
                interpretation["failure_type"]
            )
        )
        st.caption(
            "Collected evidence remains available for review and the "
            "variant has not been removed."
        )
        if interpretation["narrative"]:
            st.markdown("**Reviewer-authored narrative**")
            st.write(interpretation["narrative"])
    else:
        st.write(interpretation["narrative"])
        st.markdown("**Conflict assessment**")
        st.write(interpretation["conflict_assessment"])
    for warning in interpretation["warnings"]:
        st.warning(warning)
    st.caption(
        f"Model: {interpretation['model']} · "
        f"Prompt: {interpretation['prompt_version']}"
    )

    if content["reviewer_notes"]:
        st.markdown("#### Reviewer notes")
        for note in content["reviewer_notes"]:
            st.write(f"- {note}")

    st.markdown("#### Literature References")
    if not content["literature_references"]:
        st.caption("No scientific literature reference was available.")
    for reference in content["literature_references"]:
        label = f"[{reference['reference_id']}] {reference['source']}"
        if reference["identifier"]:
            label += f" — {reference['identifier']}"
        if reference["title"]:
            label += f" — {reference['title']}"
        if (
            reference["url_status"] == "validated"
            and reference["canonical_url"]
        ):
            st.link_button(label, reference["canonical_url"])
        else:
            st.write(label)
            st.caption("No validated link is available for this reference.")

    st.markdown("#### Data Sources")
    if not content["data_sources"]:
        st.caption("No database or tool provenance was recorded.")
    for source in content["data_sources"]:
        presented = build_reviewer_source_status(source)
        label = f"{source['source']} — {presented['category']}"
        if source["record_identifier"]:
            label += f" — {source['record_identifier']}"
        if source["source"] == "MyVariant.info":
            label += " — Programmatic annotation source"
        if source["human_url"] and source["link_status"] == "validated":
            st.link_button(f"{label} — Open human record", source["human_url"])
        else:
            st.write(f"- {label}")
        st.caption(presented["message"])
        if presented["recovery"]:
            st.caption(f"Recovery: {presented['recovery']}")

    with st.expander("Provenance and limitations"):
        provenance = content["provenance"]
        st.write(
            "Providers: "
            + (", ".join(provenance["providers"]) or "None recorded")
        )
        st.write(
            "Upstream sources: "
            + (
                ", ".join(provenance["upstream_sources"])
                or "None recorded"
            )
        )
        for limitation in content["limitations"]:
            st.write(f"- {limitation}")


def _render_interpretation_retry(
    report: DraftVariantReport,
    result: PipelineResult,
    *,
    model: str | None,
) -> None:
    """Offer a selected-variant retry from persisted evidence only."""

    interpretation = report["reviewed_report"]["variant_interpretation"]
    if interpretation["status"] != "failed":
        return
    has_reviewer_edits_or_confirmation = bool(
        report["edit_history"]
        or any(
            package["variant_index"] == report["variant_index"]
            for package in result["reviewed_evidence_packages"]
        )
    )
    st.caption(
        "Retry uses the persisted Evidence Object for this variant. "
        "Annotation, ClinVar, population, and phenotype providers are not rerun."
    )
    if st.button(
        "Retry interpretation",
        type="primary",
        icon=":material/refresh:",
        disabled=has_reviewer_edits_or_confirmation,
        key=f"{_REVIEW_WIDGET_PREFIX}retry_{report['report_id']}",
    ):
        try:
            with st.spinner("Retrying interpretation from persisted evidence..."):
                updated = retry_failed_variant_interpretation(
                    result,
                    variant_index=report["variant_index"],
                    model=model,
                )
        except PipelineError as exc:
            st.error(f"Interpretation retry was not completed: {exc}")
            return
        result.clear()
        result.update(updated)
        st.session_state["pipeline_result"] = result
        persisted = _persist_review_state(result)
        _set_notice(
            "success" if persisted else "warning",
            (
                "Interpretation retry completed."
                if persisted
                else "Interpretation retry completed, but persistence failed."
            ),
        )
        st.rerun()
    if has_reviewer_edits_or_confirmation:
        st.caption(
            "Retry is disabled because report edits or confirmation already "
            "exist for this report."
        )
    elif report["selection_history"]:
        st.warning(
            "Retry replaces the prior failed interpretation and clears any "
            "unresolved-inclusion acknowledgement."
        )


def _render_revised_interpretation_request(
    report: DraftVariantReport,
    result: PipelineResult,
    *,
    model: str | None,
) -> None:
    """Collect an explicit reviewer context before a new model request."""

    revisions = [
        item
        for item in result.get("revised_interpretations", [])
        if item.get("variant_index") == report["variant_index"]
    ]
    with st.expander(
        "Request revised interpretation",
        icon=":material/autorenew:",
    ):
        st.caption(
            "Use this only after reviewing the retained evidence. Your context is "
            "versioned with the new interpretation; provider evidence and the "
            "initial interpretation remain unchanged. Do not enter personal data."
        )
        if revisions:
            st.markdown("**Prior revised interpretations**")
            for revision in revisions:
                outcome = revision["interpretation"]
                label = outcome.get("preliminary_classification") or "Ambiguous"
                with st.expander(
                    f"Version {revision['revision_number']}: {label}",
                    icon=":material/history:",
                ):
                    st.caption(f"Requested: {revision['requested_at']}")
                    st.markdown("**Reviewer context**")
                    st.write(revision["reviewer_context"])
                    st.markdown("**Classification rationale**")
                    st.write(outcome.get("classification_rationale"))
                    st.markdown("**Revised interpretation**")
                    st.write(outcome.get("interpretation"))
        with st.form(
            f"{_REVIEW_WIDGET_PREFIX}reinterpret_{report['report_id']}",
            border=False,
        ):
            reviewer_context = st.text_area(
                "Reviewer-approved context or conflict resolution",
                height=180,
                help=(
                    "Explain the review decision or the missing context to use. "
                    "This does not change the retained source evidence."
                ),
                key=f"{_REVIEW_WIDGET_PREFIX}reinterpret_context_{report['report_id']}",
            )
            request = st.form_submit_button(
                "Request revised interpretation",
                type="primary",
                icon=":material/autorenew:",
            )
    if not request:
        return
    try:
        with st.spinner("Generating a revised interpretation from retained evidence..."):
            updated = request_revised_variant_interpretation(
                result,
                variant_index=report["variant_index"],
                reviewer_context=reviewer_context,
                model=model,
            )
    except PipelineError as exc:
        st.error(f"Revised interpretation was not completed: {exc}")
        return
    result.clear()
    result.update(updated)
    st.session_state["pipeline_result"] = result
    persisted = _persist_review_state(result)
    _set_notice(
        "success" if persisted else "warning",
        (
            "Revised interpretation created; the initial interpretation remains available."
            if persisted
            else "Revised interpretation created, but persistence failed."
        ),
    )
    st.rerun()


def _render_interpretation_version_selection(
    report: DraftVariantReport,
    result: PipelineResult,
) -> None:
    """Let the reviewer explicitly choose the retained version for final output."""

    revisions = [
        item
        for item in result.get("revised_interpretations", [])
        if item.get("variant_index") == report["variant_index"]
    ]
    if not revisions:
        return
    history = [
        item
        for item in result.get("interpretation_version_selection_history", [])
        if item.get("variant_index") == report["variant_index"]
    ]
    selected_revision = (
        history[-1]["selected_revision_number"] if history else None
    )
    options = [0, *[item["revision_number"] for item in revisions]]
    labels = {
        0: "Initial interpretation",
        **{
            item["revision_number"]: (
                f"Revised interpretation {item['revision_number']} — "
                f"{item['interpretation'].get('preliminary_classification') or 'Ambiguous'}"
            )
            for item in revisions
        },
    }
    with st.container(border=True):
        st.markdown("#### Final-report interpretation version")
        st.caption(
            "Choose the reviewed initial or revised preliminary interpretation. "
            "This selection is audited, preserves source evidence, and must be "
            "made before evidence confirmation."
        )
        choice = st.selectbox(
            "Interpretation version for final output",
            options,
            index=options.index(selected_revision)
            if selected_revision in options
            else 0,
            format_func=lambda item: labels[item],
            key=(
                f"{_REVIEW_WIDGET_PREFIX}interpretation_version_"
                f"{report['report_id']}"
            ),
        )
        if st.button(
            "Save interpretation version",
            icon=":material/save:",
            key=(
                f"{_REVIEW_WIDGET_PREFIX}save_interpretation_version_"
                f"{report['report_id']}"
            ),
        ):
            try:
                updated = select_final_interpretation_version(
                    result,
                    variant_index=report["variant_index"],
                    selected_revision_number=choice,
                    reviewer_context="local_streamlit_session",
                )
            except PipelineError as exc:
                st.error(f"Interpretation version was not saved: {exc}")
                return
            result.clear()
            result.update(updated)
            st.session_state["pipeline_result"] = result
            persisted = _persist_review_state(result)
            _set_notice(
                "success" if persisted else "warning",
                (
                    "Interpretation version saved for final output."
                    if persisted
                    else "Interpretation version saved in this session, but persistence failed."
                ),
            )
            st.rerun()


def _render_report_editor(
    report: DraftVariantReport,
    result: PipelineResult,
) -> None:
    report_id = report["report_id"]
    edit_key = f"{_REVIEW_WIDGET_PREFIX}edit_requested_{report_id}"
    generation_key = f"{_REVIEW_WIDGET_PREFIX}editor_generation_{report_id}"
    generation = int(st.session_state.get(generation_key, 0))
    content = report["reviewed_report"]
    interpretation = content["variant_interpretation"]
    expanded = bool(st.session_state.get(edit_key, False))
    with st.expander(
        "Edit clinical report",
        expanded=expanded,
        key=f"{_REVIEW_WIDGET_PREFIX}document_editor_{report_id}_{expanded}",
        icon=":material/edit_document:",
    ):
        st.caption(
            "Only the four reviewer-owned report regions below are editable. "
            "Allele identity, generated evidence, source attribution, and the "
            "machine original remain read-only."
        )
        with st.form(
            f"{_REVIEW_WIDGET_PREFIX}report_form_{report_id}_{generation}",
            border=False,
        ):
            with st.container(border=True):
                st.caption("Page 1 · Brief Interpretation(s)")
                st.markdown("#### Brief interpretation")
                st.caption(
                    "Edit the concise reviewer-facing summary shown below the "
                    "prominent result block."
                )
                reviewer_summary = st.text_area(
                    "Reviewer summary",
                    value=content["reviewer_summary"] or "",
                    height=120,
                    help=(
                        "Optional reviewer-authored finding summary. Do not enter "
                        "protected health information."
                    ),
                    key=(
                        f"{_REVIEW_WIDGET_PREFIX}summary_{report_id}_{generation}"
                    ),
                )
            with st.container(border=True):
                st.caption("Page 2 · Variant interpretation")
                st.markdown("#### Variant interpretation")
                st.caption(
                    "Revise the evidence-grounded narrative. Generated evidence "
                    "and citations are not changed by this edit."
                )
                narrative = st.text_area(
                    "Variant interpretation narrative",
                    value=interpretation["narrative"] or "",
                    height=300,
                    key=(
                        f"{_REVIEW_WIDGET_PREFIX}narrative_{report_id}_{generation}"
                    ),
                )
            with st.container(border=True):
                st.caption("Page 3 · Variant(s) classification")
                st.markdown("#### Classification summary")
                st.caption(
                    "Edit the reviewer wording about conflicts. Provider "
                    "classifications remain immutable and source-attributed."
                )
                conflict_assessment = st.text_area(
                    "Conflict assessment wording",
                    value=interpretation["conflict_assessment"] or "",
                    height=160,
                    key=(
                        f"{_REVIEW_WIDGET_PREFIX}classification_"
                        f"{report_id}_{generation}"
                    ),
                )
            with st.container(border=True):
                st.caption("Page 3 · Comments and scope")
                st.markdown("#### Reviewer notes")
                notes_text = st.text_area(
                    "Reviewer notes (one note per line)",
                    value="\n".join(content["reviewer_notes"]),
                    height=140,
                    key=(
                        f"{_REVIEW_WIDGET_PREFIX}notes_{report_id}_{generation}"
                    ),
                )
            with st.container(
                horizontal=True,
                horizontal_alignment="right",
            ):
                cancel = st.form_submit_button(
                    "Cancel",
                    icon=":material/close:",
                )
                reset = st.form_submit_button(
                    "Reset editable fields",
                    icon=":material/restart_alt:",
                )
                save = st.form_submit_button(
                    "Save report edits",
                    type="primary",
                    icon=":material/save:",
                )
    if cancel:
        _close_report_edit(report_id)
        st.rerun()
    if not save and not reset:
        return
    original = report["machine_original_report"]
    try:
        updated_report = save_draft_variant_report(
            report,
            reviewer_summary=(
                original["reviewer_summary"]
                if reset
                else reviewer_summary
            ),
            interpretation_narrative=(
                original["variant_interpretation"]["narrative"]
                if reset
                else narrative
            ),
            conflict_assessment=(
                original["variant_interpretation"][
                    "conflict_assessment"
                ]
                if reset
                else conflict_assessment
            ),
            reviewer_notes=(
                original["reviewer_notes"]
                if reset
                else [
                    line.strip()
                    for line in notes_text.splitlines()
                    if line.strip()
                ]
            ),
            reviewer_context="local_streamlit_session",
        )
        updated_result = update_draft_variant_report(
            result,
            updated_report,
        )
    except (DraftVariantReportError, PipelineError) as exc:
        st.error(f"Draft Variant Report was not saved: {exc}")
        return
    result.clear()
    result.update(updated_result)
    evidence_report = next(
        (
            item
            for item in result["evidence_review_reports"]
            if item["variant_index"] == report["variant_index"]
        ),
        None,
    )
    if evidence_report is not None:
        packages = cast(
            dict[str, ReviewedEvidencePackage],
            st.session_state.setdefault(REVIEW_PACKAGES_KEY, {}),
        )
        retained_indexes = {
            package["variant_index"]
            for package in result.get("reviewed_evidence_packages", [])
        }
        if report["variant_index"] not in retained_indexes:
            packages.pop(evidence_report["report_id"], None)
            st.session_state.pop(
                f"{_REVIEW_WIDGET_PREFIX}privacy_"
                f"{evidence_report['report_id']}",
                None,
            )
    st.session_state["pipeline_result"] = result
    persisted = _persist_review_state(result)
    action = "reset" if reset else "saved"
    if persisted:
        _set_notice("success", f"Draft Variant Report {action}.")
    else:
        _set_notice(
            "warning",
            f"Report {action} in this session, but persistence failed.",
        )
    _close_report_edit(report_id)
    st.rerun()


def _render_inclusion_control(
    report: DraftVariantReport,
    result: PipelineResult,
) -> None:
    """Render selection and the audited unresolved-inclusion exception."""

    interpretation = report["reviewed_report"]["variant_interpretation"]
    if interpretation["status"] == "failed":
        acknowledgements = {
            item["variant_index"]: item
            for item in result.get("unresolved_finalization_acknowledgements", [])
            if isinstance(item, dict)
        }
        existing = acknowledgements.get(report["variant_index"], {})
        st.warning(
            "This interpretation remains unresolved. It is excluded by default. "
            "Including it requires an explicit reviewer acknowledgement."
        )
        with st.form(
            f"{_REVIEW_WIDGET_PREFIX}unresolved_selection_{report['report_id']}",
            border=True,
        ):
            include = st.checkbox(
                "Include this unresolved variant in Final Report",
                value=report["include_in_final_report"],
                key=(
                    f"{_REVIEW_WIDGET_PREFIX}include_final_"
                    f"{report['report_id']}"
                ),
            )
            reason = st.text_area(
                "Reason for including an unresolved interpretation",
                value=str(existing.get("reason") or ""),
                max_chars=500,
                disabled=not include,
                help="Record the reviewer rationale without patient identifiers.",
                key=(
                    f"{_REVIEW_WIDGET_PREFIX}unresolved_reason_"
                    f"{report['report_id']}"
                ),
            )
            submitted = st.form_submit_button(
                "Save final report selection",
                icon=":material/save:",
            )
        if not submitted:
            return
        try:
            updated_result = result
            if include != report["include_in_final_report"]:
                updated_report = set_draft_variant_report_inclusion(
                    report,
                    include,
                    reviewer_context="local_streamlit_session",
                )
                updated_result = update_draft_variant_report(
                    result,
                    updated_report,
                )
            if include:
                updated_result = acknowledge_unresolved_interpretation_inclusion(
                    updated_result,
                    variant_index=report["variant_index"],
                    reason=reason,
                )
        except (DraftVariantReportError, PipelineError) as exc:
            st.error(f"Final Report selection was not saved: {exc}")
            return
        result.clear()
        result.update(updated_result)
        st.session_state["pipeline_result"] = result
        persisted = _persist_review_state(result)
        _set_notice(
            "success" if persisted else "warning",
            (
                "Unresolved inclusion acknowledgement saved."
                if include and persisted
                else "Variant excluded from the Final Report selection."
                if not include and persisted
                else "Selection was saved in this session, but persistence failed."
            ),
        )
        st.rerun()
        return

    include = st.checkbox(
        "Include this variant in Final Report",
        value=report["include_in_final_report"],
        key=(
            f"{_REVIEW_WIDGET_PREFIX}include_final_"
            f"{report['report_id']}"
        ),
    )
    st.caption(
        "This is a reporting choice only; it does not rank or prioritize "
        "variants. Excluded variants and their full audit history remain "
        "recoverable."
    )
    if include == report["include_in_final_report"]:
        return
    try:
        updated_report = set_draft_variant_report_inclusion(
            report,
            include,
            reviewer_context="local_streamlit_session",
        )
        updated_result = update_draft_variant_report(
            result,
            updated_report,
        )
    except (DraftVariantReportError, PipelineError) as exc:
        st.error(f"Final Report selection was not saved: {exc}")
        return
    result.clear()
    result.update(updated_result)
    evidence_report = next(
        (
            item
            for item in result["evidence_review_reports"]
            if item["variant_index"] == report["variant_index"]
        ),
        None,
    )
    if evidence_report is not None:
        packages = cast(
            dict[str, ReviewedEvidencePackage],
            st.session_state.setdefault(REVIEW_PACKAGES_KEY, {}),
        )
        packages.pop(evidence_report["report_id"], None)
        st.session_state.pop(
            f"{_REVIEW_WIDGET_PREFIX}privacy_"
            f"{evidence_report['report_id']}",
            None,
        )
    st.session_state["pipeline_result"] = result
    persisted = _persist_review_state(result)
    decision = "included" if include else "excluded"
    if persisted:
        _set_notice(
            "success",
            f"Variant {decision} in the Final Report selection.",
        )
    else:
        _set_notice(
            "warning",
            f"Variant {decision} in this session, but persistence failed.",
        )
    st.rerun()


def _render_editable_docx_download(
    report: DraftVariantReport,
    result: PipelineResult,
) -> None:
    """Regenerate the editable Word artifact from current reviewed fields."""

    try:
        records = result["variant_report_records"]
        if records:
            record = records[report["variant_index"]]
            if record["report_id"] != report["report_id"]:
                raise ValueError(
                    "Report lifecycle identity does not match the editor."
                )
            report_data = record["report_data"]
        else:
            report_data = build_report_data_from_draft(
                report,
                analysis_id=str(
                    result.get("analysis_id") or "unpersisted-analysis"
                ),
            )
        docx_data = render_report_data_docx(report_data)
        filename = report_docx_filename(report_data)
    except (DraftVariantReportError, ReportDocxError, ValueError) as exc:
        st.warning(f"Editable Word report is unavailable: {exc}")
        return
    st.download_button(
        "Download editable DOCX",
        data=docx_data,
        file_name=filename,
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        icon=":material/download:",
        key=f"{_REVIEW_WIDGET_PREFIX}download_docx_{report['report_id']}",
    )
    st.caption(
        "Regenerated from the current reviewed report. Saving a material edit "
        "invalidates prior confirmation before this artifact is rebuilt."
    )


def _render_report_comparison(report: DraftVariantReport) -> None:
    history = report["edit_history"]
    if not history:
        st.info("The reviewed report still matches the machine original.")
    else:
        st.dataframe(
            [
                {
                    "Sequence": edit["sequence"],
                    "Field": edit["field_path"],
                    "Old value": json.dumps(
                        edit["old_value"],
                        ensure_ascii=False,
                    ),
                    "New value": json.dumps(
                        edit["new_value"],
                        ensure_ascii=False,
                    ),
                    "Timestamp": edit["timestamp"],
                    "Context": edit["reviewer_context"],
                }
                for edit in history
            ],
            hide_index=True,
        )
    st.markdown("**Final Report selection history**")
    if report["selection_history"]:
        st.dataframe(
            [
                {
                    "Sequence": decision["sequence"],
                    "Old value": decision["old_value"],
                    "New value": decision["new_value"],
                    "Timestamp": decision["timestamp"],
                    "Context": decision["reviewer_context"],
                }
                for decision in report["selection_history"]
            ],
            hide_index=True,
        )
    else:
        st.caption("No Final Report selection changes have been recorded.")
    original_column, reviewed_column = st.columns(2)
    with original_column:
        st.markdown("**Machine original**")
        st.json(report["machine_original_report"], expanded=False)
    with reviewed_column:
        st.markdown("**Current reviewed report**")
        st.json(report["reviewed_report"], expanded=False)


def _render_confirmation(
    report: EvidenceReviewReport,
    result: PipelineResult,
) -> None:
    packages = cast(
        dict[str, ReviewedEvidencePackage],
        st.session_state.setdefault(REVIEW_PACKAGES_KEY, {}),
    )
    report_id = report["report_id"]

    st.caption(
        "Confirming builds an immutable reviewed evidence state for this "
        "variant. Interpretation was already generated during analysis; "
        "confirmation does not call a model. It confirms evidence review and "
        "report wording, not a pathogenicity classification or laboratory sign-out."
    )
    if any(
        item.get("variant_index") == report["variant_index"]
        for item in result.get("revised_interpretations", [])
    ):
        st.info(
            "A revised interpretation exists. Select the initial or revised "
            "version in Clinical report review before confirming evidence."
        )
    privacy_attested = st.checkbox(
        (
            "I confirm that this reviewed evidence contains no names, "
            "contact details, record numbers, dates of birth, or other "
            "protected health information."
        ),
        key=f"{_REVIEW_WIDGET_PREFIX}privacy_{report_id}",
    )
    if st.button(
        "Confirm evidence",
        key=f"{_REVIEW_WIDGET_PREFIX}confirm_{report_id}",
        disabled=not privacy_attested,
    ):
        evidence_objects = result.get("evidence_objects", [])
        variant_index = report["variant_index"]
        if (
            variant_index >= len(evidence_objects)
            or report["original_machine_report"]
            != evidence_objects[variant_index]
        ):
            st.error(
                "This draft no longer matches the current analysis."
            )
        else:
            try:
                confirmed = confirm_reviewed_evidence(result, [report])
                result["reviewed_evidence_packages"] = confirmed[
                    "reviewed_evidence_packages"
                ]
                package = next(
                    item
                    for item in result["reviewed_evidence_packages"]
                    if item["variant_index"] == variant_index
                )
                packages[report_id] = cast(
                    ReviewedEvidencePackage,
                    package,
                )
                if not _persist_review_state(result):
                    st.warning(
                        "Evidence was confirmed in this session, but "
                        "database persistence failed."
                    )
            except (PipelineError, StopIteration) as exc:
                st.error(f"Evidence was not confirmed: {exc}")

    package = packages.get(report_id)
    if package is not None:
        _render_package_summary(package)


def _render_finalization_action(result: PipelineResult) -> None:
    variant_count = result["variant_count"]
    confirmed_indexes = {
        package["variant_index"]
        for package in result.get("reviewed_evidence_packages", [])
    }
    fully_confirmed = (
        variant_count > 0
        and confirmed_indexes == set(range(variant_count))
    )
    completed = result.get("workflow_state") == "completed"
    failed_count = sum(
        item.get("status") == "failed"
        for item in result.get("variant_interpretation_results", [])
    )
    included_count = sum(
        bool(report.get("include_in_final_report"))
        for report in result.get("draft_variant_reports", [])
    )
    selected_unresolved = [
        item["variant_index"]
        for item in result.get("variant_interpretation_results", [])
        if item.get("status") == "failed"
        and item.get("variant_index") < len(result.get("draft_variant_reports", []))
        and result["draft_variant_reports"][item["variant_index"]].get(
            "include_in_final_report"
        )
    ]
    acknowledged_indexes = {
        item.get("variant_index")
        for item in result.get("unresolved_finalization_acknowledgements", [])
        if isinstance(item, dict)
    }
    missing_acknowledgements = [
        index for index in selected_unresolved if index not in acknowledged_indexes
    ]

    with st.container(border=True):
        st.markdown("**Finalize reviewed analysis**")
        st.caption(
            f"Confirmed variants: {len(confirmed_indexes)} of "
            f"{variant_count}. Finalization validates the persisted review "
            "state and does not make another LLM call. Final Report "
            f"selection: {included_count} of {variant_count}, in original "
            "input order. It records an audited report disposition, not a "
            "laboratory sign-out or independent ACMG/AMP adjudication."
        )
        if not fully_confirmed:
            st.info(
                "Confirm the reviewed state for every variant before "
                "finalization."
            )
        if missing_acknowledgements:
            st.warning(
                "Selected unresolved interpretations require an explicit "
                "reviewer acknowledgement before finalization."
            )
        if st.button(
            "Finalize review",
            type="primary",
            icon=":material/task_alt:",
            disabled=not fully_confirmed or completed or bool(missing_acknowledgements),
            key=f"{_REVIEW_WIDGET_PREFIX}finalize_review",
        ):
            try:
                with st.spinner("Finalizing reviewed analysis..."):
                    generated = finalize_reviewed_analysis(result)
            except PipelineError as exc:
                st.error(f"Review could not be finalized: {exc}")
                return
            result.clear()
            result.update(generated)
            st.session_state["pipeline_result"] = result
            persisted = _persist_review_state(result)
            if persisted:
                _set_notice("success", "Audited report disposition recorded.")
            else:
                _set_notice(
                    "warning",
                    "Review finalized, but database persistence failed.",
                )
            st.rerun()
        if completed:
            final_report = result.get("final_clinical_report")
            metadata = (
                final_report.get("metadata")
                if isinstance(final_report, dict)
                else None
            )
            state = metadata.get("finalization_state") if isinstance(metadata, dict) else None
            if state == "Finalized with unresolved variants":
                st.warning("Finalized with unresolved variants.")
            elif failed_count:
                st.warning(
                    f"Final review excludes {failed_count} unresolved "
                    "interpretation(s)."
                )
            else:
                st.success(
                    "Audited report disposition is recorded. No additional LLM "
                    "call was made; this is not a laboratory sign-out."
                )


def render_evidence_review(
    result: PipelineResult,
    *,
    light_model: str | None = None,
    strong_model: str | None = None,
) -> None:
    """Render pre-interpreted drafts and final review controls."""

    retry_model = strong_model or light_model
    st.subheader("Draft Variant Review — Evidence and interpretation")
    _render_notice()
    reports = result.get("evidence_review_reports", [])
    if not reports:
        st.info("No editable evidence review reports are available.")
        return
    drafts = _initialize_drafts(reports, result)
    _render_variant_status_cards(result)
    selected_value = st.session_state.get(REVIEW_VARIANT_KEY, 0)
    selected = (
        selected_value
        if isinstance(selected_value, int)
        and not isinstance(selected_value, bool)
        and 0 <= selected_value < len(drafts)
        else 0
    )
    st.session_state[REVIEW_VARIANT_KEY] = selected
    report = drafts[selected]
    draft_variant_report = next(
        (
            item
            for item in result.get("draft_variant_reports", [])
            if item["variant_index"] == report["variant_index"]
        ),
        None,
    )
    if draft_variant_report is None:
        st.selectbox(
            "Variant evidence report",
            options=list(range(len(drafts))),
            format_func=lambda index: _variant_label(drafts[index]),
            key=REVIEW_VARIANT_KEY,
        )
        _render_conflict_status(report)
        st.info(
            "Draft Variant Report V2 is unavailable for this legacy "
            "analysis state. Evidence remains readable."
        )
        legacy_view = st.segmented_control(
            "Evidence review details",
            (
                "Edit evidence draft",
                "Original evidence",
                "Evidence edit history",
            ),
            default="Edit evidence draft",
            required=True,
            key=f"{_REVIEW_WIDGET_PREFIX}legacy_detail_view",
            persist_state="page",
        )
        if legacy_view == "Edit evidence draft":
            _render_editor(report, selected, drafts, result)
        elif legacy_view == "Original evidence":
            st.json(report["original_machine_report"], expanded=2)
        else:
            _render_history(report)
        return
    draft_variant_report = cast(
        DraftVariantReport,
        draft_variant_report,
    )
    _render_report_preview(
        draft_variant_report,
        selected=selected,
        total=len(drafts),
    )
    _render_interpretation_retry(
        draft_variant_report,
        result,
        model=retry_model,
    )
    st.caption(
        "Narrative fields and reviewer notes are editable with an "
        "append-only audit trail. Variant identity, provider evidence, "
        "provenance, and the machine original remain immutable."
    )
    selected_count = sum(
        bool(item.get("include_in_final_report"))
        for item in result.get("draft_variant_reports", [])
    )
    st.caption(
        f"Final Report selection: {selected_count} of "
        f"{result['variant_count']} variants."
    )
    _render_conflict_status(report)
    review_view = st.segmented_control(
        "Review surface",
        ("Clinical report review", "Technical details"),
        default="Clinical report review",
        required=True,
        key=f"{_REVIEW_WIDGET_PREFIX}surface_view",
        persist_state="page",
    )
    if review_view == "Clinical report review":
        _render_inclusion_control(draft_variant_report, result)
        _render_report_editor(draft_variant_report, result)
        _render_revised_interpretation_request(
            draft_variant_report,
            result,
            model=retry_model,
        )
        _render_interpretation_version_selection(
            draft_variant_report,
            result,
        )
        _render_editable_docx_download(draft_variant_report, result)
    else:
        technical_view = st.segmented_control(
            "Technical detail",
            (
                "Provider diagnostics",
                "Draft Variant Report",
                "Compare report",
                "Edit evidence draft",
                "Original evidence",
                "Evidence edit history",
                "Final confirmation",
            ),
            default="Provider diagnostics",
            required=True,
            key=f"{_REVIEW_WIDGET_PREFIX}technical_detail_view",
            persist_state="page",
        )
        if technical_view == "Provider diagnostics":
            _render_selected_provider_diagnostics(
                result,
                report["variant_index"],
            )
        elif technical_view == "Draft Variant Report":
            _render_draft_variant_report(result, report["variant_index"])
        elif technical_view == "Compare report":
            _render_report_comparison(draft_variant_report)
        elif technical_view == "Edit evidence draft":
            _render_editor(report, selected, drafts, result)
        elif technical_view == "Original evidence":
            st.json(report["original_machine_report"], expanded=2)
        elif technical_view == "Evidence edit history":
            _render_history(report)
        else:
            _render_confirmation(drafts[selected], result)
    _render_finalization_action(result)
    if result.get("workflow_state") == "completed":
        render_final_clinical_report_viewer(result)


__all__ = [
    "REVIEW_DRAFTS_KEY",
    "REVIEW_PACKAGES_KEY",
    "clear_evidence_review_state",
    "interpretation_failure_message",
    "render_evidence_review",
]
