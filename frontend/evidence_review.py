"""Stage 33 editable Output A review interface."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import cast

import streamlit as st

from backend.evidence_review import (
    EvidenceReviewError,
    EvidenceReviewReport,
    MAX_REVIEW_REPORT_BYTES,
    save_evidence_review_draft,
)
from backend.pipeline import PipelineResult


REVIEW_DRAFTS_KEY = "evidence_review_drafts"
REVIEW_SIGNATURE_KEY = "evidence_review_signature"
REVIEW_VARIANT_KEY = "selected_evidence_review_variant"
_REVIEW_WIDGET_PREFIX = "evidence_review_"


def clear_evidence_review_state() -> None:
    """Discard session drafts when their source analysis changes."""

    for key in list(st.session_state):
        if (
            key in {
                REVIEW_DRAFTS_KEY,
                REVIEW_SIGNATURE_KEY,
                REVIEW_VARIANT_KEY,
            }
            or str(key).startswith(_REVIEW_WIDGET_PREFIX)
        ):
            st.session_state.pop(key, None)


def _initialize_drafts(
    reports: list[dict[str, object]],
) -> list[EvidenceReviewReport]:
    signature = tuple(
        str(report.get("report_id", ""))
        for report in reports
    )
    if st.session_state.get(REVIEW_SIGNATURE_KEY) != signature:
        st.session_state[REVIEW_SIGNATURE_KEY] = signature
        st.session_state[REVIEW_DRAFTS_KEY] = deepcopy(reports)
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
    if status in {"conflict", "meaningful_conflict"}:
        st.warning(f"Pre-review conflict status: {status}")
    elif isinstance(status, str) and status:
        st.caption(f"Pre-review conflict status: {status}")


def _render_editor(
    report: EvidenceReviewReport,
    report_index: int,
    drafts: list[EvidenceReviewReport],
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
        save = st.form_submit_button("Save draft", type="primary")
    if not save:
        return
    try:
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
        drafts[report_index] = save_evidence_review_draft(
            report,
            reviewed,
            notes,
        )
    except (json.JSONDecodeError, EvidenceReviewError) as exc:
        st.error(f"Draft was not saved: {exc}")
        return
    st.success("Draft saved in this session.")


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


def render_evidence_review(result: PipelineResult) -> None:
    """Render editable reports without invoking final interpretation."""

    st.subheader("Output A — Editable detailed evidence report")
    reports = result.get("evidence_review_reports", [])
    if not reports:
        st.info("No editable evidence review reports are available.")
        return
    drafts = _initialize_drafts(reports)
    selected = st.selectbox(
        "Variant evidence report",
        options=list(range(len(drafts))),
        format_func=lambda index: _variant_label(drafts[index]),
        key=REVIEW_VARIANT_KEY,
    )
    report = drafts[selected]
    _render_conflict_status(report)
    st.caption(
        "Drafts remain local to this browser session. Final LLM "
        "interpretation is not run before confirmation."
    )
    editor_tab, original_tab, history_tab = st.tabs(
        ["Edit draft", "Original machine report", "Edit history"]
    )
    with editor_tab:
        _render_editor(report, selected, drafts)
    with original_tab:
        st.json(report["original_machine_report"], expanded=2)
    with history_tab:
        _render_history(report)


__all__ = [
    "REVIEW_DRAFTS_KEY",
    "clear_evidence_review_state",
    "render_evidence_review",
]
