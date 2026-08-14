"""Persisted per-variant report lifecycle built around ReportData V4."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import json
from typing import Literal, TypedDict, cast

from backend.report_data import ReportData, ReportDataError, validate_report_data
from backend.report_data_projection import build_report_data_from_draft
from backend.report_docx import (
    ReportDocxError,
    render_report_data_docx,
    report_docx_filename,
)
from backend.variant_report import (
    DraftVariantReportError,
    validate_draft_variant_report,
)


REPORT_LIFECYCLE_SCHEMA_VERSION = "1.1"
UNPERSISTED_ANALYSIS_ID = "unpersisted-analysis"
ReportLifecycleState = Literal[
    "draft",
    "confirmed",
    "finalized",
    "finalized_with_unresolved_interpretation",
]
LIFECYCLE_STATES = frozenset(ReportLifecycleState.__args__)


class ReportLifecycleError(ValueError):
    """Raised when a persisted per-variant report record is inconsistent."""


class ReportDocxArtifact(TypedDict):
    """Deterministic metadata for the editable DOCX draft."""

    filename: str
    sha256: str
    size_bytes: int
    template_version: str


class UnresolvedInterpretationAcknowledgement(TypedDict):
    """Audited reviewer decision to include an unresolved interpretation."""

    variant_index: int
    interpretation_fingerprint: str
    failure_type: str
    reason: str
    acknowledged_at: str


class VariantReportRecord(TypedDict):
    """One ordered report from draft through confirmation and finalization."""

    schema_version: str
    report_id: str
    analysis_id: str
    variant_index: int
    lifecycle_state: ReportLifecycleState
    report_data: ReportData
    docx_artifact: ReportDocxArtifact
    created_at: str
    updated_at: str
    confirmed_at: str | None
    finalized_at: str | None
    unresolved_interpretation_acknowledgement: (
        UnresolvedInterpretationAcknowledgement | None
    )


RECORD_FIELDS = frozenset(VariantReportRecord.__required_keys__)
ARTIFACT_FIELDS = frozenset(ReportDocxArtifact.__required_keys__)
ACKNOWLEDGEMENT_FIELDS = frozenset(
    UnresolvedInterpretationAcknowledgement.__required_keys__
)


def _timestamp(value: object, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReportLifecycleError(f"{path} must be a normalized UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportLifecycleError(f"{path} is invalid.") from exc
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise ReportLifecycleError(f"{path} must be normalized.")
    return value


def _report_key(report: ReportData) -> str:
    return json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@lru_cache(maxsize=256)
def _rendered_docx(report_json: str) -> bytes:
    return render_report_data_docx(json.loads(report_json))


def _artifact(report: ReportData) -> ReportDocxArtifact:
    data = _rendered_docx(_report_key(report))
    return {
        "filename": report_docx_filename(report),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "template_version": report["template_version"],
    }


def build_variant_report_record(
    draft: object,
    *,
    analysis_id: str | None,
    confirmed_at: str | None = None,
    finalized_at: str | None = None,
    unresolved_interpretation_acknowledgement: Mapping[str, object] | None = None,
) -> VariantReportRecord:
    """Build one canonical lifecycle record and its DOCX draft metadata."""

    try:
        validated_draft = validate_draft_variant_report(draft)
        report = build_report_data_from_draft(
            validated_draft,
            analysis_id=analysis_id or UNPERSISTED_ANALYSIS_ID,
        )
        if confirmed_at is not None:
            _timestamp(confirmed_at, "confirmed_at")
            report["review_state"]["review_status"] = "confirmed"
            report["review_state"]["confirmed_at"] = confirmed_at
            report = validate_report_data(report)
        if finalized_at is not None and confirmed_at is None:
            raise ReportLifecycleError(
                "A finalized report record must already be confirmed."
            )
        unresolved = (
            validated_draft["reviewed_report"]["variant_interpretation"]["status"]
            == "failed"
        )
        selected = validated_draft["include_in_final_report"]
        if finalized_at is not None and selected and unresolved:
            if unresolved_interpretation_acknowledgement is None:
                raise ReportLifecycleError(
                    "An unresolved selected report requires a reviewer acknowledgement."
                )
            lifecycle_state: ReportLifecycleState = (
                "finalized_with_unresolved_interpretation"
            )
        elif finalized_at is not None and selected:
            lifecycle_state = "finalized"
        else:
            lifecycle_state = "confirmed" if confirmed_at is not None else "draft"
        effective_updated_at = (
            finalized_at or confirmed_at or validated_draft["updated_at"]
        )
        return validate_variant_report_record(
            {
                "schema_version": REPORT_LIFECYCLE_SCHEMA_VERSION,
                "report_id": validated_draft["report_id"],
                "analysis_id": analysis_id or UNPERSISTED_ANALYSIS_ID,
                "variant_index": validated_draft["variant_index"],
                "lifecycle_state": lifecycle_state,
                "report_data": report,
                "docx_artifact": _artifact(report),
                "created_at": validated_draft["created_at"],
                "updated_at": effective_updated_at,
                "confirmed_at": confirmed_at,
                "finalized_at": finalized_at,
                "unresolved_interpretation_acknowledgement": (
                    dict(unresolved_interpretation_acknowledgement)
                    if lifecycle_state
                    == "finalized_with_unresolved_interpretation"
                    else None
                ),
            }
        )
    except (DraftVariantReportError, ReportDataError, ReportDocxError) as exc:
        raise ReportLifecycleError(
            "The per-variant report lifecycle record could not be built."
        ) from exc


def build_variant_report_records(
    drafts: Sequence[Mapping[str, object]],
    *,
    analysis_id: str | None,
    confirmed_packages: Sequence[Mapping[str, object]] = (),
    finalized_at: str | None = None,
    unresolved_interpretation_acknowledgements: Sequence[
        Mapping[str, object]
    ] = (),
) -> list[VariantReportRecord]:
    """Build every record in immutable original input order."""

    confirmed_by_index: dict[int, str] = {}
    for package in confirmed_packages:
        index = package.get("variant_index")
        timestamp = package.get("confirmed_at")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not isinstance(timestamp, str)
        ):
            raise ReportLifecycleError("Confirmed package metadata is invalid.")
        confirmed_by_index[index] = timestamp
    acknowledgements_by_index: dict[int, Mapping[str, object]] = {}
    for acknowledgement in unresolved_interpretation_acknowledgements:
        index = acknowledgement.get("variant_index")
        if isinstance(index, bool) or not isinstance(index, int) or index in acknowledgements_by_index:
            raise ReportLifecycleError(
                "Unresolved interpretation acknowledgement metadata is invalid."
            )
        acknowledgements_by_index[index] = acknowledgement
    records = [
        build_variant_report_record(
            draft,
            analysis_id=analysis_id,
            confirmed_at=confirmed_by_index.get(index),
            finalized_at=(
                finalized_at
                if index in confirmed_by_index
                and bool(draft.get("include_in_final_report"))
                else None
            ),
            unresolved_interpretation_acknowledgement=acknowledgements_by_index.get(
                index
            ),
        )
        for index, draft in enumerate(drafts)
    ]
    if [record["variant_index"] for record in records] != list(range(len(records))):
        raise ReportLifecycleError(
            "Per-variant report records must preserve original input order."
        )
    return records


def validate_variant_report_record(value: object) -> VariantReportRecord:
    """Validate one lifecycle record, including deterministic DOCX metadata."""

    if not isinstance(value, Mapping) or set(value) != RECORD_FIELDS:
        raise ReportLifecycleError("variant_report_record has invalid fields.")
    item = dict(value)
    if item["schema_version"] != REPORT_LIFECYCLE_SCHEMA_VERSION:
        raise ReportLifecycleError("variant_report_record schema is unsupported.")
    if not isinstance(item["report_id"], str) or not item["report_id"].strip():
        raise ReportLifecycleError("variant_report_record.report_id is invalid.")
    if not isinstance(item["analysis_id"], str) or not item["analysis_id"].strip():
        raise ReportLifecycleError("variant_report_record.analysis_id is invalid.")
    index = item["variant_index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ReportLifecycleError("variant_report_record.variant_index is invalid.")
    state = item["lifecycle_state"]
    if state not in LIFECYCLE_STATES:
        raise ReportLifecycleError("variant_report_record.lifecycle_state is invalid.")
    try:
        report = validate_report_data(item["report_data"])
    except ReportDataError as exc:
        raise ReportLifecycleError(
            "variant_report_record.report_data is invalid."
        ) from exc
    if (
        report["report_id"] != item["report_id"]
        or report["analysis_id"] != item["analysis_id"]
        or report["input_index"] != index
    ):
        raise ReportLifecycleError("variant_report_record identity is inconsistent.")
    created_at = cast(str, _timestamp(item["created_at"], "created_at"))
    updated_at = cast(str, _timestamp(item["updated_at"], "updated_at"))
    confirmed_at = _timestamp(item["confirmed_at"], "confirmed_at", optional=True)
    finalized_at = _timestamp(item["finalized_at"], "finalized_at", optional=True)
    acknowledgement_value = item["unresolved_interpretation_acknowledgement"]
    acknowledgement: dict[str, object] | None = None
    if acknowledgement_value is not None:
        if (
            not isinstance(acknowledgement_value, Mapping)
            or set(acknowledgement_value) != ACKNOWLEDGEMENT_FIELDS
        ):
            raise ReportLifecycleError(
                "Unresolved interpretation acknowledgement is invalid."
            )
        acknowledgement = dict(acknowledgement_value)
        if acknowledgement["variant_index"] != index:
            raise ReportLifecycleError(
                "Unresolved interpretation acknowledgement index is inconsistent."
            )
        for field in ("interpretation_fingerprint", "failure_type", "reason"):
            if not isinstance(acknowledgement[field], str) or not acknowledgement[field].strip():
                raise ReportLifecycleError(
                    "Unresolved interpretation acknowledgement text is invalid."
                )
        acknowledged_at = _timestamp(
            acknowledgement["acknowledged_at"],
            "unresolved_interpretation_acknowledgement.acknowledged_at",
        )
        if acknowledged_at is None:
            raise ReportLifecycleError(
                "Unresolved interpretation acknowledgement timestamp is invalid."
            )
    created_value = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    updated_value = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    confirmed_value = (
        datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
        if confirmed_at is not None
        else None
    )
    finalized_value = (
        datetime.fromisoformat(finalized_at.replace("Z", "+00:00"))
        if finalized_at is not None
        else None
    )
    if updated_value < created_value:
        raise ReportLifecycleError("variant_report_record timestamps are out of order.")
    if confirmed_value is not None and confirmed_value < created_value:
        raise ReportLifecycleError("Report confirmation predates record creation.")
    if (
        finalized_value is not None
        and confirmed_value is not None
        and finalized_value < confirmed_value
    ):
        raise ReportLifecycleError("Report finalization predates confirmation.")
    if state == "draft" and (confirmed_at is not None or finalized_at is not None):
        raise ReportLifecycleError(
            "Draft report lifecycle timestamps are inconsistent."
        )
    if state == "confirmed" and (confirmed_at is None or finalized_at is not None):
        raise ReportLifecycleError(
            "Confirmed report lifecycle timestamps are inconsistent."
        )
    if state == "finalized" and (confirmed_at is None or finalized_at is None):
        raise ReportLifecycleError(
            "Finalized report lifecycle timestamps are inconsistent."
        )
    if state == "finalized_with_unresolved_interpretation" and (
        confirmed_at is None or finalized_at is None
    ):
        raise ReportLifecycleError(
            "Unresolved finalization timestamps are inconsistent."
        )
    selected = report["review_state"]["include_in_final_report"]
    unresolved = report["interpretation"]["interpretation_status"] == "unavailable"
    if state == "finalized" and (not selected or unresolved):
        raise ReportLifecycleError(
            "Ordinary finalization cannot contain an unselected or unresolved report."
        )
    if state == "finalized_with_unresolved_interpretation":
        if not selected or not unresolved or acknowledgement is None:
            raise ReportLifecycleError(
                "Unresolved finalization does not match the selected report state."
            )
        if acknowledgement["failure_type"] != report["interpretation"]["failure_type"]:
            raise ReportLifecycleError(
                "Unresolved finalization failure category is inconsistent."
            )
    elif acknowledgement is not None:
        raise ReportLifecycleError(
            "Only unresolved finalization may retain an acknowledgement."
        )
    if (report["review_state"]["review_status"] == "confirmed") != (
        confirmed_at is not None
    ):
        raise ReportLifecycleError("ReportData confirmation state is inconsistent.")
    artifact_value = item["docx_artifact"]
    if (
        not isinstance(artifact_value, Mapping)
        or set(artifact_value) != ARTIFACT_FIELDS
    ):
        raise ReportLifecycleError("variant_report_record.docx_artifact is invalid.")
    expected_artifact = _artifact(report)
    if dict(artifact_value) != expected_artifact:
        raise ReportLifecycleError(
            "variant_report_record DOCX metadata is inconsistent."
        )
    return deepcopy(cast(VariantReportRecord, item))


__all__ = [
    "REPORT_LIFECYCLE_SCHEMA_VERSION",
    "UNPERSISTED_ANALYSIS_ID",
    "ReportLifecycleError",
    "UnresolvedInterpretationAcknowledgement",
    "VariantReportRecord",
    "build_variant_report_record",
    "build_variant_report_records",
    "validate_variant_report_record",
]
