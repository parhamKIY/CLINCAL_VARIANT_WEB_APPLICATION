"""Stage 33 editable Output A review interface."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import cast

import streamlit as st

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
)


REVIEW_DRAFTS_KEY = "evidence_review_drafts"
REVIEW_SIGNATURE_KEY = "evidence_review_signature"
REVIEW_VARIANT_KEY = "selected_evidence_review_variant"
REVIEW_PACKAGES_KEY = "evidence_review_packages"
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
        st.session_state[REVIEW_PACKAGES_KEY] = {}
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


def _render_package_summary(package: ReviewedEvidencePackage) -> None:
    post_review = package["post_review_conflict"]
    status = post_review.get("status")
    st.success(f"Confirmed at {package['confirmed_at']}.")
    if status == "conflict":
        st.warning(
            "Post-review conflict status: conflict "
            f"(severity: {post_review.get('routing_severity')})."
        )
    else:
        st.caption("Post-review conflict status: no_conflict")
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
        "this variant. Final interpretation still requires Stage 35 "
        "LLM routing, which is not implemented yet."
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
            except (PipelineError, StopIteration) as exc:
                st.error(f"Evidence was not confirmed: {exc}")

    package = packages.get(report_id)
    if package is not None:
        _render_package_summary(package)


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


__all__ = [
    "REVIEW_DRAFTS_KEY",
    "REVIEW_PACKAGES_KEY",
    "clear_evidence_review_state",
    "render_evidence_review",
]
