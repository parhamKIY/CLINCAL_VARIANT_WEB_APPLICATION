"""Stage 33 editable Output A review interface."""

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
    confirm_reviewed_evidence,
    resume_confirmed_analysis,
)


REVIEW_DRAFTS_KEY = "evidence_review_drafts"
REVIEW_SIGNATURE_KEY = "evidence_review_signature"
REVIEW_VARIANT_KEY = "selected_evidence_review_variant"
REVIEW_PACKAGES_KEY = "evidence_review_packages"
REVIEW_NOTICE_KEY = "evidence_review_notice"
_REVIEW_WIDGET_PREFIX = "evidence_review_"


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
    result["workflow_state"] = "awaiting_confirmation"


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
                "family, or manual evidence fields may be added."
            ),
        )
        notes_text = st.text_area(
            "Reviewer notes (one note per line)",
            value="\n".join(report["reviewer_notes"]),
            height=120,
            key=notes_key,
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
    routing_result: dict[str, object] | None = None,
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
    if (
        routing_result is not None
        and routing_result.get("resolution_status") == "unresolved"
    ):
        st.error("Unresolved conflict in the final interpretation.")
    if package["user_added_evidence"]:
        st.caption(
            f"{len(package['user_added_evidence'])} user-added "
            "evidence field(s) will be visible to the LLM."
        )


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
        "Confirming builds an immutable Reviewed Evidence Package for "
        "this variant. Stage 35 interpretation routing is available only "
        "after confirmation."
    )
    if st.button(
        "Confirm evidence",
        key=f"{_REVIEW_WIDGET_PREFIX}confirm_{report_id}",
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
        routing_result = next(
            (
                item
                for item in result.get("llm_routing_results", [])
                if item.get("variant_index") == report["variant_index"]
            ),
            None,
        )
        _render_package_summary(package, routing_result)


def _render_interpretation_action(
    result: PipelineResult,
    *,
    light_model: str | None,
    strong_model: str | None,
) -> None:
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

    with st.container(border=True):
        st.markdown("**Generate final interpretation**")
        st.caption(
            f"Confirmed variants: {len(confirmed_indexes)} of {variant_count}. "
            "No-conflict variants use the low-cost model; meaningful "
            "conflicts use the strong model."
        )
        if not fully_confirmed:
            st.info(
                "Confirm the reviewed evidence for every variant before "
                "generating Output B."
            )
        if st.button(
            "Generate interpretation",
            type="primary",
            icon=":material/auto_awesome:",
            disabled=not fully_confirmed or completed,
            key=f"{_REVIEW_WIDGET_PREFIX}generate_interpretation",
        ):
            try:
                with st.spinner("Generating confirmed interpretations..."):
                    generated = resume_confirmed_analysis(
                        result,
                        light_model=light_model,
                        strong_model=strong_model,
                    )
            except PipelineError as exc:
                st.error(f"Interpretation could not be generated: {exc}")
                return
            result.clear()
            result.update(generated)
            st.session_state["pipeline_result"] = result
            persisted = _persist_review_state(result)
            if persisted:
                _set_notice("success", "Output B generated.")
            else:
                _set_notice(
                    "warning",
                    "Output B generated, but database persistence failed.",
                )
            st.rerun()
        if completed:
            st.success("Output B has been generated from confirmed evidence.")


def render_evidence_review(
    result: PipelineResult,
    *,
    light_model: str | None = None,
    strong_model: str | None = None,
) -> None:
    """Render editable reports without invoking final interpretation."""

    st.subheader("Output A — Editable detailed evidence report")
    _render_notice()
    reports = result.get("evidence_review_reports", [])
    if not reports:
        st.info("No editable evidence review reports are available.")
        return
    drafts = _initialize_drafts(reports, result)
    selected = st.selectbox(
        "Variant evidence report",
        options=list(range(len(drafts))),
        format_func=lambda index: _variant_label(drafts[index]),
        key=REVIEW_VARIANT_KEY,
    )
    report = drafts[selected]
    _render_conflict_status(report)
    st.caption(
        "Every reviewed JSON field can be edited, added, or deleted. "
        "The immutable original remains available for comparison. Final "
        "interpretation is blocked until every variant is confirmed."
    )
    editor_tab, original_tab, history_tab, confirm_tab = st.tabs(
        [
            "Edit draft",
            "Original machine report",
            "Edit history",
            "Confirm evidence",
        ]
    )
    with editor_tab:
        _render_editor(report, selected, drafts, result)
    with original_tab:
        st.json(report["original_machine_report"], expanded=2)
    with history_tab:
        _render_history(report)
    with confirm_tab:
        _render_confirmation(drafts[selected], result)
    _render_interpretation_action(
        result,
        light_model=light_model,
        strong_model=strong_model,
    )


__all__ = [
    "REVIEW_DRAFTS_KEY",
    "REVIEW_PACKAGES_KEY",
    "clear_evidence_review_state",
    "render_evidence_review",
]
