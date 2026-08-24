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


REPORT_LIFECYCLE_SCHEMA_VERSION = "1.0"
UNPERSISTED_ANALYSIS_ID = "unpersisted-analysis"
ReportLifecycleState = Literal["draft", "confirmed", "finalized"]
LIFECYCLE_STATES = frozenset(ReportLifecycleState.__args__)


class ReportLifecycleError(ValueError):
    """Raised when a persisted per-variant report record is inconsistent."""


class ReportDocxArtifact(TypedDict):
    """Deterministic metadata for the editable DOCX draft."""

    filename: str
    sha256: str
    size_bytes: int
    template_version: str


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


RECORD_FIELDS = frozenset(VariantReportRecord.__required_keys__)
ARTIFACT_FIELDS = frozenset(ReportDocxArtifact.__required_keys__)


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
        lifecycle_state: ReportLifecycleState = (
            "finalized"
            if finalized_at is not None
            else ("confirmed" if confirmed_at is not None else "draft")
        )
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
    records = [
        build_variant_report_record(
            draft,
            analysis_id=analysis_id,
            confirmed_at=confirmed_by_index.get(
                cast(int, draft.get("variant_index"))
            ),
            finalized_at=(
                finalized_at
                if draft.get("variant_index") in confirmed_by_index
                else None
            ),
        )
        for draft in drafts
    ]
    record_indexes = [record["variant_index"] for record in records]
    if record_indexes != sorted(set(record_indexes)):
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
    "VariantReportRecord",
    "build_variant_report_record",
    "build_variant_report_records",
    "validate_variant_report_record",
]
