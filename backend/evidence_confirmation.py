"""Stage 34 human confirmation gate and Reviewed Evidence Package."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict, cast

from backend.conflict_auditor import ConflictAuditResult, audit_evidence_conflicts
from backend.evidence_review import (
    EDIT_RECORD_FIELDS,
    MAX_EDIT_HISTORY,
    EvidenceEditRecord,
    EvidenceReviewError,
    validate_bounded_json_tree,
    validate_evidence_review_report,
)
from backend.report import EvidenceObject, validate_evidence_object


REVIEWED_PACKAGE_SCHEMA_VERSION = "1.0"
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_NOTES = 50
MAX_PACKAGE_NOTE_LENGTH = 4_000
_PACKAGE_ID_PATTERN = re.compile(r"reviewed-package-[0-9a-f]{24}")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

PACKAGE_FIELDS = frozenset(
    {
        "schema_version",
        "package_id",
        "variant_index",
        "status",
        "original_machine_report",
        "reviewed_user_report",
        "user_added_evidence",
        "reviewer_notes",
        "provenance",
        "edit_history",
        "pre_review_conflict",
        "post_review_conflict",
        "confirmed_at",
    }
)


class EvidenceConfirmationError(ValueError):
    """Raised when a Stage 34 human confirmation input is invalid."""


class ReviewedEvidencePackage(TypedDict):
    """Confirmed, immutable snapshot ready for LLM routing."""

    schema_version: str
    package_id: str
    variant_index: int
    status: Literal["confirmed"]
    original_machine_report: EvidenceObject
    reviewed_user_report: dict[str, Any]
    user_added_evidence: list[EvidenceEditRecord]
    reviewer_notes: list[str]
    provenance: dict[str, Any]
    edit_history: list[EvidenceEditRecord]
    pre_review_conflict: ConflictAuditResult
    post_review_conflict: ConflictAuditResult
    confirmed_at: str


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise EvidenceConfirmationError(
            "confirmed_at must be a non-empty string."
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceConfirmationError(
            "confirmed_at must use ISO 8601."
        ) from exc
    if parsed.tzinfo is None:
        raise EvidenceConfirmationError(
            "confirmed_at must include a timezone."
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_notes(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_PACKAGE_NOTES:
        raise EvidenceConfirmationError(
            "reviewer_notes must be a bounded list."
        )
    notes: list[str] = []
    for index, note in enumerate(value):
        if (
            not isinstance(note, str)
            or not note
            or len(note) > MAX_PACKAGE_NOTE_LENGTH
            or _CONTROL_PATTERN.search(note) is not None
        ):
            raise EvidenceConfirmationError(
                f"reviewer_notes[{index}] contains invalid text."
            )
        notes.append(note)
    return notes


def _validate_edit_records(
    value: object,
    *,
    path: str,
    require_added: bool,
) -> list[EvidenceEditRecord]:
    if not isinstance(value, list) or len(value) > MAX_EDIT_HISTORY:
        raise EvidenceConfirmationError(f"{path} must be a bounded list.")
    validated: list[EvidenceEditRecord] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict) or set(item) != EDIT_RECORD_FIELDS:
            raise EvidenceConfirmationError(
                f"{item_path} has invalid fields."
            )
        change_type = item["change_type"]
        if change_type not in {"added", "modified", "deleted"}:
            raise EvidenceConfirmationError(
                f"{item_path}.change_type is unsupported."
            )
        if item["user_added"] is not (change_type == "added"):
            raise EvidenceConfirmationError(
                f"{item_path}.user_added is inconsistent."
            )
        if require_added and not item["user_added"]:
            raise EvidenceConfirmationError(
                f"{item_path} must be a user-added record."
            )
        validate_bounded_json_tree(
            item["old_value"],
            path=f"{item_path}.old_value",
        )
        validate_bounded_json_tree(
            item["new_value"],
            path=f"{item_path}.new_value",
        )
        validated.append(
            {
                "path": item["path"],
                "change_type": change_type,
                "old_value": deepcopy(item["old_value"]),
                "new_value": deepcopy(item["new_value"]),
                "user_added": item["user_added"],
                "timestamp": _timestamp(item["timestamp"]),
            }
        )
    return validated


def _extract_reviewed_values(
    reviewed_report: Mapping[str, object],
) -> dict[str, object]:
    """Read the flat fields a user could edit for override detection."""

    conditions = reviewed_report.get("clinvar_conditions")
    return {
        "classification": reviewed_report.get("clinvar_significance"),
        "assembly": reviewed_report.get("assembly"),
        "transcript": reviewed_report.get("transcript"),
        "gene": reviewed_report.get("gene"),
        "conditions": (
            conditions if isinstance(conditions, list) else []
        ),
    }


def _package_id(
    original: Mapping[str, object],
    variant_index: int,
    confirmed_at: str,
) -> str:
    canonical = json.dumps(
        {
            "original_machine_report": original,
            "variant_index": variant_index,
            "confirmed_at": confirmed_at,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"reviewed-package-{digest}"


def confirm_evidence_review(
    report: Mapping[str, object],
    *,
    timestamp: str | None = None,
) -> ReviewedEvidencePackage:
    """Build a Stage 34 Reviewed Evidence Package from a confirmed draft.

    Preserves the immutable original evidence, re-runs the deterministic
    conflict audit against the user-reviewed values, and never calls the
    LLM. Raises if the draft was edited after the requested confirmation
    time, since confirmation must reflect the latest saved draft.
    """

    try:
        validated = validate_evidence_review_report(deepcopy(report))
    except EvidenceReviewError as exc:
        raise EvidenceConfirmationError(str(exc)) from exc

    original = validated["original_machine_report"]
    pre_review = _mapping(original.get("conflict_audit")).get("pre_review")
    if not isinstance(pre_review, dict):
        raise EvidenceConfirmationError(
            "original_machine_report is missing a pre-review conflict "
            "result."
        )

    confirmed_at = _timestamp(timestamp)
    if _timestamp_value(confirmed_at) < _timestamp_value(
        validated["updated_at"]
    ):
        raise EvidenceConfirmationError(
            "confirmed_at cannot precede the latest saved draft edit."
        )

    reviewed_values = _extract_reviewed_values(
        validated["reviewed_user_report"]
    )
    post_review = audit_evidence_conflicts(
        original,
        phase="post_review",
        reviewed_values=reviewed_values,
    )
    user_added_evidence = [
        deepcopy(edit)
        for edit in validated["edit_history"]
        if edit["user_added"]
    ]

    package: ReviewedEvidencePackage = {
        "schema_version": REVIEWED_PACKAGE_SCHEMA_VERSION,
        "package_id": _package_id(
            original,
            validated["variant_index"],
            confirmed_at,
        ),
        "variant_index": validated["variant_index"],
        "status": "confirmed",
        "original_machine_report": original,
        "reviewed_user_report": validated["reviewed_user_report"],
        "user_added_evidence": user_added_evidence,
        "reviewer_notes": validated["reviewer_notes"],
        "provenance": deepcopy(_mapping(original.get("provenance"))),
        "edit_history": validated["edit_history"],
        "pre_review_conflict": cast(
            ConflictAuditResult,
            deepcopy(pre_review),
        ),
        "post_review_conflict": post_review,
        "confirmed_at": confirmed_at,
    }
    size = len(
        json.dumps(
            package,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if size > MAX_PACKAGE_BYTES:
        raise EvidenceConfirmationError(
            "Reviewed evidence package exceeds the size limit."
        )
    return package


def validate_reviewed_evidence_package(
    value: object,
) -> ReviewedEvidencePackage:
    """Validate a Stage 34 package and detect tampering on reload."""

    if not isinstance(value, dict) or set(value) != PACKAGE_FIELDS:
        raise EvidenceConfirmationError(
            "Reviewed evidence package has invalid fields."
        )
    if value["schema_version"] != REVIEWED_PACKAGE_SCHEMA_VERSION:
        raise EvidenceConfirmationError(
            "Reviewed evidence package schema version is unsupported."
        )
    if value["status"] != "confirmed":
        raise EvidenceConfirmationError(
            "Reviewed evidence package status must be confirmed."
        )
    variant_index = value["variant_index"]
    if (
        isinstance(variant_index, bool)
        or not isinstance(variant_index, int)
        or variant_index < 0
    ):
        raise EvidenceConfirmationError(
            "variant_index must be a non-negative integer."
        )
    try:
        original = validate_evidence_object(
            deepcopy(value["original_machine_report"])
        )
    except Exception as exc:
        raise EvidenceConfirmationError(
            "original_machine_report is invalid."
        ) from exc

    reviewed = value["reviewed_user_report"]
    if not isinstance(reviewed, dict):
        raise EvidenceConfirmationError(
            "reviewed_user_report must be a dictionary."
        )
    validate_bounded_json_tree(reviewed, path="reviewed_user_report")

    provenance = value["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance != _mapping(original.get("provenance"))
    ):
        raise EvidenceConfirmationError(
            "provenance must match the immutable original evidence."
        )

    pre_review = value["pre_review_conflict"]
    if (
        not isinstance(pre_review, dict)
        or pre_review
        != _mapping(original.get("conflict_audit")).get("pre_review")
    ):
        raise EvidenceConfirmationError(
            "pre_review_conflict must match the immutable original "
            "evidence."
        )

    post_review = value["post_review_conflict"]
    if (
        not isinstance(post_review, dict)
        or post_review.get("phase") != "post_review"
        or post_review.get("final_classification") is not None
    ):
        raise EvidenceConfirmationError(
            "post_review_conflict is invalid."
        )

    confirmed_at = _timestamp(value["confirmed_at"])
    edit_history = _validate_edit_records(
        value["edit_history"],
        path="edit_history",
        require_added=False,
    )
    user_added_evidence = _validate_edit_records(
        value["user_added_evidence"],
        path="user_added_evidence",
        require_added=True,
    )
    reviewer_notes = _validate_notes(value["reviewer_notes"])

    package_id = value["package_id"]
    if (
        not isinstance(package_id, str)
        or _PACKAGE_ID_PATTERN.fullmatch(package_id) is None
        or package_id
        != _package_id(original, variant_index, confirmed_at)
    ):
        raise EvidenceConfirmationError(
            "package_id does not match the immutable original evidence."
        )

    validated: ReviewedEvidencePackage = {
        "schema_version": REVIEWED_PACKAGE_SCHEMA_VERSION,
        "package_id": package_id,
        "variant_index": variant_index,
        "status": "confirmed",
        "original_machine_report": original,
        "reviewed_user_report": deepcopy(reviewed),
        "user_added_evidence": user_added_evidence,
        "reviewer_notes": reviewer_notes,
        "provenance": deepcopy(provenance),
        "edit_history": edit_history,
        "pre_review_conflict": cast(
            ConflictAuditResult,
            deepcopy(pre_review),
        ),
        "post_review_conflict": cast(
            ConflictAuditResult,
            deepcopy(post_review),
        ),
        "confirmed_at": confirmed_at,
    }
    if len(
        json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > MAX_PACKAGE_BYTES:
        raise EvidenceConfirmationError(
            "Reviewed evidence package exceeds the size limit."
        )
    return validated


__all__ = [
    "MAX_PACKAGE_BYTES",
    "PACKAGE_FIELDS",
    "REVIEWED_PACKAGE_SCHEMA_VERSION",
    "EvidenceConfirmationError",
    "ReviewedEvidencePackage",
    "confirm_evidence_review",
    "validate_reviewed_evidence_package",
]
