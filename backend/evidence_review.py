"""Editable, bounded Stage 33 evidence-review report contract."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from backend.privacy import (
    ClinicalDataPrivacyError,
    validate_human_review_content,
)
from backend.report import EvidenceObject, validate_evidence_object


EVIDENCE_REVIEW_SCHEMA_VERSION = "1.0"
MAX_REVIEW_REPORTS = 100
MAX_REVIEW_REPORT_BYTES = 256 * 1024
MAX_REVIEW_TREE_DEPTH = 16
MAX_REVIEW_TREE_NODES = 10_000
MAX_REVIEW_STRING_LENGTH = 10_000
MAX_REVIEW_KEY_LENGTH = 200
MAX_REVIEW_NOTES = 50
MAX_REVIEW_NOTE_LENGTH = 4_000
MAX_EDIT_HISTORY = 500
MAX_EDITS_PER_SAVE = 200
MAX_EDIT_PATH_LENGTH = 2_000
MAX_REVIEW_STATE_BYTES = 4 * 1024 * 1024
REVIEW_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "variant_index",
        "status",
        "original_machine_report",
        "reviewed_user_report",
        "reviewer_notes",
        "edit_history",
        "created_at",
        "updated_at",
    }
)
EDIT_RECORD_FIELDS = frozenset(
    {
        "path",
        "change_type",
        "old_value",
        "new_value",
        "user_added",
        "timestamp",
    }
)
_REPORT_ID_PATTERN = re.compile(r"evidence-review-[0-9a-f]{24}")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MISSING = object()


class EvidenceReviewError(ValueError):
    """Raised when an editable evidence-review report is invalid."""


class EvidenceEditRecord(TypedDict):
    """One bounded, append-only user draft change."""

    path: str
    change_type: Literal["added", "modified", "deleted"]
    old_value: object | None
    new_value: object | None
    user_added: bool
    timestamp: str


class EvidenceReviewReport(TypedDict):
    """Original and user-reviewed Output A versions for one variant."""

    schema_version: str
    report_id: str
    variant_index: int
    status: Literal["draft"]
    original_machine_report: EvidenceObject
    reviewed_user_report: dict[str, Any]
    reviewer_notes: list[str]
    edit_history: list[EvidenceEditRecord]
    created_at: str
    updated_at: str


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if not isinstance(value, str) or not value.strip():
        raise EvidenceReviewError("Review timestamp must be non-empty.")
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(
            normalized.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceReviewError(
            "Review timestamp must use ISO 8601."
        ) from exc
    if parsed.tzinfo is None:
        raise EvidenceReviewError(
            "Review timestamp must include a timezone."
        )
    return parsed.astimezone(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_text(
    value: object,
    *,
    path: str,
    maximum: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _CONTROL_PATTERN.search(value) is not None
    ):
        raise EvidenceReviewError(f"{path} contains invalid text.")
    return value


def _validate_review_tree(value: object, *, path: str) -> None:
    nodes = 0

    def visit(item: object, item_path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_REVIEW_TREE_NODES:
            raise EvidenceReviewError(
                f"{path} exceeds the node limit."
            )
        if depth > MAX_REVIEW_TREE_DEPTH:
            raise EvidenceReviewError(
                f"{path} exceeds the nesting limit."
            )
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise EvidenceReviewError(
                    f"{item_path} must be finite."
                )
            return
        if isinstance(item, str):
            if (
                len(item) > MAX_REVIEW_STRING_LENGTH
                or _CONTROL_PATTERN.search(item) is not None
            ):
                raise EvidenceReviewError(
                    f"{item_path} contains invalid text."
                )
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{item_path}[{index}]", depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                _validate_text(
                    key,
                    path=f"{item_path} key",
                    maximum=MAX_REVIEW_KEY_LENGTH,
                )
                visit(
                    child,
                    f"{item_path}.{key}",
                    depth + 1,
                )
            return
        raise EvidenceReviewError(
            f"{item_path} contains a non-JSON value."
        )

    visit(value, path, 0)
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceReviewError(
            f"{path} is not valid JSON."
        ) from exc
    if len(serialized) > MAX_REVIEW_REPORT_BYTES:
        raise EvidenceReviewError(
            f"{path} exceeds {MAX_REVIEW_REPORT_BYTES} bytes."
        )


def _validate_notes(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_REVIEW_NOTES:
        raise EvidenceReviewError(
            "reviewer_notes must be a bounded list."
        )
    return [
        _validate_text(
            note,
            path=f"reviewer_notes[{index}]",
            maximum=MAX_REVIEW_NOTE_LENGTH,
        )
        for index, note in enumerate(value)
    ]


def _escape_path(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _report_id(
    original: EvidenceObject,
    variant_index: int,
) -> str:
    canonical = json.dumps(
        original,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(
        variant_index.to_bytes(8, "big") + canonical
    ).hexdigest()[:24]
    return f"evidence-review-{digest}"


def _diff_values(
    old: object,
    new: object,
    *,
    path: str,
    timestamp: str,
    output: list[EvidenceEditRecord],
) -> None:
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            child_path = f"{path}/{_escape_path(key)}"
            _diff_values(
                old.get(key, _MISSING),
                new.get(key, _MISSING),
                path=child_path,
                timestamp=timestamp,
                output=output,
            )
        return
    if isinstance(old, list) and isinstance(new, list):
        for index in range(max(len(old), len(new))):
            _diff_values(
                old[index] if index < len(old) else _MISSING,
                new[index] if index < len(new) else _MISSING,
                path=f"{path}/{index}",
                timestamp=timestamp,
                output=output,
            )
        return

    if old is _MISSING:
        change_type: Literal["added", "modified", "deleted"] = (
            "added"
        )
    elif new is _MISSING:
        change_type = "deleted"
    else:
        change_type = "modified"
    output.append(
        {
            "path": path or "/",
            "change_type": change_type,
            "old_value": (
                None if old is _MISSING else deepcopy(old)
            ),
            "new_value": (
                None if new is _MISSING else deepcopy(new)
            ),
            "user_added": change_type == "added",
            "timestamp": timestamp,
        }
    )
    if len(output) > MAX_EDITS_PER_SAVE:
        raise EvidenceReviewError(
            "One draft save exceeds the edit limit."
        )


def _validate_edit_history(value: object) -> list[EvidenceEditRecord]:
    if not isinstance(value, list) or len(value) > MAX_EDIT_HISTORY:
        raise EvidenceReviewError(
            "edit_history must be a bounded list."
        )
    validated: list[EvidenceEditRecord] = []
    for index, item in enumerate(value):
        path = f"edit_history[{index}]"
        if not isinstance(item, dict) or set(item) != EDIT_RECORD_FIELDS:
            raise EvidenceReviewError(f"{path} has invalid fields.")
        edit_path = _validate_text(
            item["path"],
            path=f"{path}.path",
            maximum=MAX_EDIT_PATH_LENGTH,
        )
        if not edit_path.startswith("/"):
            raise EvidenceReviewError(
                f"{path}.path must be absolute."
            )
        change_type = item["change_type"]
        if change_type not in {"added", "modified", "deleted"}:
            raise EvidenceReviewError(
                f"{path}.change_type is unsupported."
            )
        if item["user_added"] is not (change_type == "added"):
            raise EvidenceReviewError(
                f"{path}.user_added is inconsistent."
            )
        _validate_review_tree(
            item["old_value"],
            path=f"{path}.old_value",
        )
        _validate_review_tree(
            item["new_value"],
            path=f"{path}.new_value",
        )
        validated.append(
            {
                "path": edit_path,
                "change_type": change_type,
                "old_value": deepcopy(item["old_value"]),
                "new_value": deepcopy(item["new_value"]),
                "user_added": item["user_added"],
                "timestamp": _timestamp(item["timestamp"]),
            }
        )
    return validated


def validate_evidence_review_report(
    value: object,
) -> EvidenceReviewReport:
    """Validate and isolate one Stage 33 editable report."""

    if not isinstance(value, dict) or set(value) != REVIEW_REPORT_FIELDS:
        raise EvidenceReviewError(
            "Evidence review report has invalid fields."
        )
    if value["schema_version"] != EVIDENCE_REVIEW_SCHEMA_VERSION:
        raise EvidenceReviewError(
            "Evidence review schema version is unsupported."
        )
    report_id = _validate_text(
        value["report_id"],
        path="report_id",
        maximum=64,
    )
    if _REPORT_ID_PATTERN.fullmatch(report_id) is None:
        raise EvidenceReviewError("report_id has an invalid format.")
    variant_index = value["variant_index"]
    if (
        isinstance(variant_index, bool)
        or not isinstance(variant_index, int)
        or variant_index < 0
        or variant_index >= MAX_REVIEW_REPORTS
    ):
        raise EvidenceReviewError(
            "variant_index must be a non-negative integer."
        )
    if value["status"] != "draft":
        raise EvidenceReviewError(
            "Stage 33 review status must remain draft."
        )
    original = validate_evidence_object(
        deepcopy(value["original_machine_report"])
    )
    reviewed = deepcopy(value["reviewed_user_report"])
    if not isinstance(reviewed, dict):
        raise EvidenceReviewError(
            "reviewed_user_report must be a dictionary."
        )
    _validate_review_tree(
        reviewed,
        path="reviewed_user_report",
    )
    created_at = _timestamp(value["created_at"])
    updated_at = _timestamp(value["updated_at"])
    created_value = _timestamp_value(created_at)
    updated_value = _timestamp_value(updated_at)
    if updated_value < created_value:
        raise EvidenceReviewError(
            "updated_at cannot precede created_at."
        )
    edit_history = _validate_edit_history(value["edit_history"])
    reviewer_notes = _validate_notes(value["reviewer_notes"])
    try:
        validate_human_review_content(
            reviewed,
            reviewer_notes,
            edit_history,
        )
    except ClinicalDataPrivacyError as exc:
        raise EvidenceReviewError(
            "Human review content contains prohibited clinical data."
        ) from exc
    if any(
        _timestamp_value(record["timestamp"]) < created_value
        or _timestamp_value(record["timestamp"]) > updated_value
        for record in edit_history
    ):
        raise EvidenceReviewError(
            "Edit timestamps must fall within the draft lifetime."
        )
    if report_id != _report_id(original, variant_index):
        raise EvidenceReviewError(
            "report_id does not match the immutable original report."
        )
    validated: EvidenceReviewReport = {
        "schema_version": EVIDENCE_REVIEW_SCHEMA_VERSION,
        "report_id": report_id,
        "variant_index": variant_index,
        "status": "draft",
        "original_machine_report": original,
        "reviewed_user_report": reviewed,
        "reviewer_notes": reviewer_notes,
        "edit_history": edit_history,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    if len(
        json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > MAX_REVIEW_STATE_BYTES:
        raise EvidenceReviewError(
            "Evidence review state exceeds the size limit."
        )
    return validated


def build_evidence_review_reports(
    evidence_objects: Iterable[Mapping[str, object]],
    *,
    timestamp: str | None = None,
    variant_indices: Sequence[int] | None = None,
) -> list[EvidenceReviewReport]:
    """Build one immutable-original editable report per ordered variant."""

    if isinstance(evidence_objects, (str, bytes, dict)):
        raise EvidenceReviewError(
            "Evidence objects must be an iterable of dictionaries."
        )
    items = list(evidence_objects)
    if len(items) > MAX_REVIEW_REPORTS:
        raise EvidenceReviewError(
            "Evidence review report count exceeds the limit."
        )
    created_at = _timestamp(timestamp)
    indexes = (
        list(variant_indices)
        if variant_indices is not None
        else list(range(len(items)))
    )
    if (
        len(indexes) != len(items)
        or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in indexes
        )
        or indexes != sorted(set(indexes))
    ):
        raise EvidenceReviewError(
            "Variant indexes must be unique non-negative integers in ascending order."
        )
    reports: list[EvidenceReviewReport] = []
    for position, evidence in enumerate(items):
        index = indexes[position]
        original = validate_evidence_object(deepcopy(evidence))
        report: EvidenceReviewReport = {
            "schema_version": EVIDENCE_REVIEW_SCHEMA_VERSION,
            "report_id": _report_id(original, index),
            "variant_index": index,
            "status": "draft",
            "original_machine_report": original,
            "reviewed_user_report": deepcopy(original),
            "reviewer_notes": [],
            "edit_history": [],
            "created_at": created_at,
            "updated_at": created_at,
        }
        reports.append(validate_evidence_review_report(report))
    return reports


def save_evidence_review_draft(
    report: Mapping[str, object],
    reviewed_user_report: Mapping[str, object],
    reviewer_notes: Sequence[str] = (),
    *,
    timestamp: str | None = None,
) -> EvidenceReviewReport:
    """Save a session draft while preserving original source evidence."""

    current = validate_evidence_review_report(deepcopy(report))
    if not isinstance(reviewed_user_report, Mapping):
        raise EvidenceReviewError(
            "Reviewed user report must be a dictionary."
        )
    reviewed = deepcopy(dict(reviewed_user_report))
    _validate_review_tree(
        reviewed,
        path="reviewed_user_report",
    )
    if isinstance(reviewer_notes, (str, bytes)):
        raise EvidenceReviewError(
            "Reviewer notes must be a sequence of strings."
        )
    notes = _validate_notes(list(reviewer_notes))
    saved_at = _timestamp(timestamp)
    changes: list[EvidenceEditRecord] = []
    _diff_values(
        current["reviewed_user_report"],
        reviewed,
        path="",
        timestamp=saved_at,
        output=changes,
    )
    _diff_values(
        current["reviewer_notes"],
        notes,
        path="/reviewer_notes",
        timestamp=saved_at,
        output=changes,
    )
    history = [*current["edit_history"], *changes]
    if len(history) > MAX_EDIT_HISTORY:
        raise EvidenceReviewError(
            "Draft edit history exceeds the limit."
        )
    updated: EvidenceReviewReport = {
        **current,
        "reviewed_user_report": reviewed,
        "reviewer_notes": notes,
        "edit_history": history,
        "updated_at": saved_at,
    }
    return validate_evidence_review_report(updated)


def validate_bounded_json_tree(value: object, *, path: str = "value") -> None:
    """Validate one bounded, JSON-safe tree using Stage 33 limits."""

    _validate_review_tree(value, path=path)


__all__ = [
    "EDIT_RECORD_FIELDS",
    "EVIDENCE_REVIEW_SCHEMA_VERSION",
    "MAX_EDIT_HISTORY",
    "EvidenceEditRecord",
    "EvidenceReviewError",
    "EvidenceReviewReport",
    "MAX_REVIEW_REPORT_BYTES",
    "build_evidence_review_reports",
    "save_evidence_review_draft",
    "validate_bounded_json_tree",
    "validate_evidence_review_report",
]
