"""Stage 116 call-quality evaluation and pre-annotation gate."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal, TypedDict, cast


CALL_QUALITY_SCHEMA_VERSION = "1.0"
MAX_FILTER_LENGTH = 500
MAX_OVERRIDE_REASON_LENGTH = 500
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CallQualityStatus = Literal["passed", "not_evaluated", "failed"]


class CallQualityError(ValueError):
    """Raised when call-quality state or a reviewer decision is unsafe."""


class CallQualityOverride(TypedDict):
    """Audited reviewer exception for one failed upstream FILTER value."""

    reason: str
    timestamp: str


class CallQualityRecord(TypedDict):
    """Call-quality state attached immutably to one normalized allele."""

    schema_version: str
    status: CallQualityStatus
    acknowledged_at: str | None
    override: CallQualityOverride | None


class CallQualityEvidence(TypedDict):
    """Immutable input-call facts carried into evidence and reporting."""

    schema_version: str
    qual: float | None
    filter: str | None
    status: CallQualityStatus
    acknowledged_at: str | None
    override: CallQualityOverride | None


CALL_QUALITY_RECORD_FIELDS = frozenset(CallQualityRecord.__required_keys__)
CALL_QUALITY_EVIDENCE_FIELDS = frozenset(CallQualityEvidence.__required_keys__)
CALL_QUALITY_OVERRIDE_FIELDS = frozenset(CallQualityOverride.__required_keys__)


def normalize_filter_value(value: object) -> str | None:
    """Preserve one bounded FILTER code string while normalizing absence."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise CallQualityError("FILTER must be text when supplied.")
    normalized = value.strip()
    if not normalized or normalized == ".":
        return None
    if (
        len(normalized) > MAX_FILTER_LENGTH
        or _CONTROL_CHARACTER_PATTERN.search(normalized) is not None
    ):
        raise CallQualityError("FILTER is invalid.")
    return normalized


def evaluate_call_quality(value: object) -> CallQualityStatus:
    """Derive the Stage 116 state without interpreting caller-specific codes."""

    normalized = normalize_filter_value(value)
    if normalized is None:
        return "not_evaluated"
    return "passed" if normalized.casefold() == "pass" else "failed"


def _timestamp(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CallQualityError(f"{path} must be a timestamp.")
    normalized = value.strip()
    if _CONTROL_CHARACTER_PATTERN.search(normalized) is not None:
        raise CallQualityError(f"{path} must be a timestamp.")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CallQualityError(f"{path} must be a timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CallQualityError(f"{path} must include a timezone.")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _reason(value: object) -> str:
    if not isinstance(value, str):
        raise CallQualityError("Call-quality override reason must be text.")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_OVERRIDE_REASON_LENGTH
        or _CONTROL_CHARACTER_PATTERN.search(normalized) is not None
    ):
        raise CallQualityError("Call-quality override reason is invalid.")
    return normalized


def validate_call_quality_override(value: object) -> CallQualityOverride:
    """Validate one bounded reviewer override without changing its meaning."""

    if not isinstance(value, Mapping) or set(value) != CALL_QUALITY_OVERRIDE_FIELDS:
        raise CallQualityError("Call-quality override has invalid fields.")
    return {
        "reason": _reason(value["reason"]),
        "timestamp": _timestamp(value["timestamp"], path="override.timestamp"),
    }


def validate_call_quality_record(value: object) -> CallQualityRecord:
    """Validate one persisted allele call-quality record."""

    if not isinstance(value, Mapping) or set(value) != CALL_QUALITY_RECORD_FIELDS:
        raise CallQualityError("Call-quality record has invalid fields.")
    if value["schema_version"] != CALL_QUALITY_SCHEMA_VERSION:
        raise CallQualityError("Call-quality record schema is unsupported.")
    status = value["status"]
    if status not in {"passed", "not_evaluated", "failed"}:
        raise CallQualityError("Call-quality status is invalid.")
    acknowledged_at = value["acknowledged_at"]
    acknowledgement = (
        None
        if acknowledged_at is None
        else _timestamp(acknowledged_at, path="acknowledged_at")
    )
    override_value = value["override"]
    override = (
        None
        if override_value is None
        else validate_call_quality_override(override_value)
    )
    if status == "passed" and (acknowledgement is not None or override is not None):
        raise CallQualityError("Passed calls cannot have reviewer exceptions.")
    if status == "not_evaluated" and override is not None:
        raise CallQualityError("Only failed calls can have an override.")
    if status == "failed" and acknowledgement is not None:
        raise CallQualityError("Failed calls require an override, not acknowledgement.")
    return {
        "schema_version": CALL_QUALITY_SCHEMA_VERSION,
        "status": cast(CallQualityStatus, status),
        "acknowledged_at": acknowledgement,
        "override": override,
    }


def _qual(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CallQualityError("QUAL must be numeric when supplied.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise CallQualityError("QUAL must be finite.")
    return normalized


def validate_call_quality_evidence(value: object) -> CallQualityEvidence:
    """Validate one immutable call-quality payload used by report contracts."""

    if not isinstance(value, Mapping) or set(value) != CALL_QUALITY_EVIDENCE_FIELDS:
        raise CallQualityError("Call-quality evidence has invalid fields.")
    record = validate_call_quality_record(
        {
            "schema_version": value["schema_version"],
            "status": value["status"],
            "acknowledged_at": value["acknowledged_at"],
            "override": value["override"],
        }
    )
    raw_filter = normalize_filter_value(value["filter"])
    if evaluate_call_quality(raw_filter) != record["status"]:
        raise CallQualityError("Call-quality evidence status does not match FILTER.")
    return {
        "schema_version": record["schema_version"],
        "qual": _qual(value["qual"]),
        "filter": raw_filter,
        "status": record["status"],
        "acknowledged_at": record["acknowledged_at"],
        "override": record["override"],
    }


def build_call_quality_evidence(variant: Mapping[str, object]) -> CallQualityEvidence:
    """Copy input QUAL/FILTER with its gate decision into report-safe form."""

    raw_filter = normalize_filter_value(variant.get("filter"))
    record_value = variant.get("call_quality")
    record = (
        build_call_quality_records([{"input_index": 0, "filter": raw_filter}])[0]
        if record_value is None
        else validate_call_quality_record(record_value)
    )
    return validate_call_quality_evidence(
        {
            "schema_version": record["schema_version"],
            "qual": _qual(variant.get("qual")),
            "filter": raw_filter,
            "status": record["status"],
            "acknowledged_at": record["acknowledged_at"],
            "override": record["override"],
        }
    )


def _decision_map(
    values: Mapping[int, object] | None,
    *,
    name: str,
) -> dict[int, object]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise CallQualityError(f"{name} must be an index-keyed mapping.")
    normalized: dict[int, object] = {}
    for index, value in values.items():
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise CallQualityError(f"{name} has an invalid variant index.")
        normalized[index] = value
    return normalized


def build_call_quality_records(
    variants: Sequence[Mapping[str, object]],
    *,
    acknowledgements: Mapping[int, object] | None = None,
    overrides: Mapping[int, object] | None = None,
    require_ready: bool = False,
) -> list[CallQualityRecord]:
    """Build ordered quality state and enforce any requested reviewer decisions."""

    acknowledgement_map = _decision_map(acknowledgements, name="acknowledgements")
    override_map = _decision_map(overrides, name="overrides")
    records: list[CallQualityRecord] = []
    for expected_index, variant in enumerate(variants):
        if not isinstance(variant, Mapping) or variant.get("input_index") != expected_index:
            raise CallQualityError("Call-quality variants must preserve input order.")
        status = evaluate_call_quality(variant.get("filter"))
        acknowledgement = acknowledgement_map.pop(expected_index, None)
        override_value = override_map.pop(expected_index, None)
        if status == "passed" and (acknowledgement is not None or override_value is not None):
            raise CallQualityError("Passed calls do not accept reviewer exceptions.")
        if status == "not_evaluated":
            if override_value is not None:
                raise CallQualityError("Only failed calls can have an override.")
            record: CallQualityRecord = {
                "schema_version": CALL_QUALITY_SCHEMA_VERSION,
                "status": status,
                "acknowledged_at": (
                    None
                    if acknowledgement is None
                    else _timestamp(acknowledgement, path="acknowledgements")
                ),
                "override": None,
            }
        elif status == "failed":
            if acknowledgement is not None:
                raise CallQualityError("Failed calls require an override, not acknowledgement.")
            record = {
                "schema_version": CALL_QUALITY_SCHEMA_VERSION,
                "status": status,
                "acknowledged_at": None,
                "override": (
                    None
                    if override_value is None
                    else validate_call_quality_override(override_value)
                ),
            }
        else:
            record = {
                "schema_version": CALL_QUALITY_SCHEMA_VERSION,
                "status": status,
                "acknowledged_at": None,
                "override": None,
            }
        records.append(validate_call_quality_record(record))
    if acknowledgement_map or override_map:
        raise CallQualityError("Call-quality decisions reference an unknown variant.")
    if require_ready and any(
        record["status"] == "not_evaluated" and record["acknowledged_at"] is None
        or record["status"] == "failed" and record["override"] is None
        for record in records
    ):
        raise CallQualityError(
            "Call quality requires reviewer acknowledgement or an audited override before analysis."
        )
    return deepcopy(records)


__all__ = [
    "CALL_QUALITY_SCHEMA_VERSION",
    "CallQualityError",
    "CallQualityEvidence",
    "CallQualityOverride",
    "CallQualityRecord",
    "CallQualityStatus",
    "build_call_quality_records",
    "build_call_quality_evidence",
    "evaluate_call_quality",
    "normalize_filter_value",
    "validate_call_quality_override",
    "validate_call_quality_record",
    "validate_call_quality_evidence",
]
