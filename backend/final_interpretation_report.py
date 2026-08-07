"""Stage 36 final-interpretation-only report contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict, cast

from backend.llm_routing import (
    Stage35RoutingError,
    validate_llm_routing_result,
)


FINAL_INTERPRETATION_REPORT_SCHEMA_VERSION = "1.0"
FINAL_INTERPRETATION_REPORT_FILENAME = "final-interpretations.txt"
MAX_FINAL_INTERPRETATION_CHARS = 22_000
MAX_FINAL_INTERPRETATION_REPORT_BYTES = 128 * 1024
UNRESOLVED_PREFIX = "Conflict remains unresolved."
FAILURE_LABELS = {
    "interpretation_generation_failed": "Interpretation generation failed.",
    "interpretation_not_generated": "Interpretation was not generated.",
}

FinalInterpretationStatus = Literal["success", "failed"]
FinalInterpretationFailure = Literal[
    "interpretation_generation_failed",
    "interpretation_not_generated",
]


class FinalInterpretationReportError(ValueError):
    """Raised when Output B violates its bounded Stage 36 contract."""


class FinalInterpretationEntry(TypedDict):
    """One ordered interpretation or explicit failure status."""

    variant_index: int
    status: FinalInterpretationStatus
    final_interpretation: str | None
    resolution_status: str | None
    failure_status: FinalInterpretationFailure | None


class FinalInterpretationReport(TypedDict):
    """Output B without raw evidence, ranking, or model prompts."""

    schema_version: str
    variant_count: int
    entries: list[FinalInterpretationEntry]


ENTRY_FIELDS = frozenset(FinalInterpretationEntry.__required_keys__)
REPORT_FIELDS = frozenset(FinalInterpretationReport.__required_keys__)


def _normalize_interpretation(value: object) -> str:
    if not isinstance(value, str):
        raise FinalInterpretationReportError(
            "final_interpretation must be non-empty text."
        )
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized or len(normalized) > MAX_FINAL_INTERPRETATION_CHARS:
        raise FinalInterpretationReportError(
            "final_interpretation exceeds its bounded text contract."
        )
    if any(
        ord(character) < 32
        for character in normalized
        if character not in {"\n", "\t"}
    ):
        raise FinalInterpretationReportError(
            "final_interpretation contains unsupported control characters."
        )
    return normalized


def _success_entry(
    variant_index: int,
    item: Mapping[str, object],
) -> FinalInterpretationEntry:
    interpretation = _normalize_interpretation(item["final_interpretation"])
    resolution = cast(str, item["resolution_status"])
    if resolution == "unresolved" and not interpretation.startswith(
        UNRESOLVED_PREFIX
    ):
        interpretation = f"{UNRESOLVED_PREFIX} {interpretation}"
    return {
        "variant_index": variant_index,
        "status": "success",
        "final_interpretation": interpretation,
        "resolution_status": resolution,
        "failure_status": None,
    }


def _failure_entry(
    variant_index: int,
    failure_status: FinalInterpretationFailure,
) -> FinalInterpretationEntry:
    return {
        "variant_index": variant_index,
        "status": "failed",
        "final_interpretation": None,
        "resolution_status": None,
        "failure_status": failure_status,
    }


def build_final_interpretation_report(
    variant_count: int,
    routing_results: Sequence[Mapping[str, object]],
) -> FinalInterpretationReport:
    """Build ordered Output B from Stage 35 results only."""

    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or variant_count < 1
    ):
        raise FinalInterpretationReportError(
            "variant_count must be a positive integer."
        )
    if isinstance(routing_results, (str, bytes, Mapping)):
        raise FinalInterpretationReportError(
            "routing_results must be a sequence of result mappings."
        )

    by_index: dict[int, dict[str, object]] = {}
    for item in routing_results:
        try:
            validated = validate_llm_routing_result(item)
        except Stage35RoutingError as exc:
            raise FinalInterpretationReportError(
                "Output B received an invalid Stage 35 result."
            ) from exc
        index = validated["variant_index"]
        if index >= variant_count or index in by_index:
            raise FinalInterpretationReportError(
                "Stage 35 result indexes must be unique and in range."
            )
        by_index[index] = dict(validated)

    entries: list[FinalInterpretationEntry] = []
    for index in range(variant_count):
        item = by_index.get(index)
        if item is None:
            entries.append(_failure_entry(index, "interpretation_not_generated"))
        elif item["status"] == "failed":
            entries.append(
                _failure_entry(index, "interpretation_generation_failed")
            )
        else:
            entries.append(_success_entry(index, item))

    report: FinalInterpretationReport = {
        "schema_version": FINAL_INTERPRETATION_REPORT_SCHEMA_VERSION,
        "variant_count": variant_count,
        "entries": entries,
    }
    return validate_final_interpretation_report(report)


def validate_final_interpretation_report(
    value: object,
) -> FinalInterpretationReport:
    """Validate Output B while prohibiting evidence and ranking fields."""

    if not isinstance(value, dict) or set(value) != REPORT_FIELDS:
        raise FinalInterpretationReportError(
            "Final interpretation report has invalid fields."
        )
    if value["schema_version"] != FINAL_INTERPRETATION_REPORT_SCHEMA_VERSION:
        raise FinalInterpretationReportError(
            "Final interpretation report schema version is unsupported."
        )
    variant_count = value["variant_count"]
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or variant_count < 1
    ):
        raise FinalInterpretationReportError(
            "Final interpretation report variant_count is invalid."
        )
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) != variant_count:
        raise FinalInterpretationReportError(
            "Final interpretation report must cover every variant."
        )

    normalized_entries: list[FinalInterpretationEntry] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise FinalInterpretationReportError(
                "Final interpretation entry has invalid fields."
            )
        if entry["variant_index"] != index:
            raise FinalInterpretationReportError(
                "Final interpretation entries must preserve input order."
            )
        if entry["status"] == "success":
            interpretation = _normalize_interpretation(
                entry["final_interpretation"]
            )
            resolution = entry["resolution_status"]
            if resolution not in {"not_applicable", "resolved", "unresolved"}:
                raise FinalInterpretationReportError(
                    "Successful interpretation has invalid resolution status."
                )
            if entry["failure_status"] is not None:
                raise FinalInterpretationReportError(
                    "Successful interpretation cannot have a failure status."
                )
            if resolution == "unresolved" and not interpretation.startswith(
                UNRESOLVED_PREFIX
            ):
                raise FinalInterpretationReportError(
                    "Unresolved conflict must be explicit in the interpretation."
                )
            normalized_entries.append(
                {
                    "variant_index": index,
                    "status": "success",
                    "final_interpretation": interpretation,
                    "resolution_status": cast(str, resolution),
                    "failure_status": None,
                }
            )
        elif entry["status"] == "failed":
            if (
                entry["final_interpretation"] is not None
                or entry["resolution_status"] is not None
                or entry["failure_status"]
                not in {
                    "interpretation_generation_failed",
                    "interpretation_not_generated",
                }
            ):
                raise FinalInterpretationReportError(
                    "Failed interpretation entry is invalid."
                )
            normalized_entries.append(
                _failure_entry(
                    index,
                    cast(
                        FinalInterpretationFailure,
                        entry["failure_status"],
                    ),
                )
            )
        else:
            raise FinalInterpretationReportError(
                "Final interpretation entry status is unsupported."
            )

    return {
        "schema_version": FINAL_INTERPRETATION_REPORT_SCHEMA_VERSION,
        "variant_count": variant_count,
        "entries": normalized_entries,
    }


def render_final_interpretation_report_text(
    report: Mapping[str, object],
) -> str:
    """Render downloadable plain text containing no raw evidence."""

    validated = validate_final_interpretation_report(report)
    sections = ["Output B — Final interpretation only"]
    for entry in validated["entries"]:
        lines = [f"Variant {entry['variant_index'] + 1}"]
        if entry["status"] == "success":
            lines.extend(
                ["Final interpretation:", cast(str, entry["final_interpretation"])]
            )
        else:
            lines.extend(
                [
                    "Interpretation status: Failed",
                    "Failure status: "
                    f"{FAILURE_LABELS[cast(str, entry['failure_status'])]}",
                ]
            )
        sections.append("\n".join(lines))
    rendered = "\n\n".join(sections) + "\n"
    if len(rendered.encode("utf-8")) > MAX_FINAL_INTERPRETATION_REPORT_BYTES:
        raise FinalInterpretationReportError(
            "Rendered final interpretation report exceeds its size limit."
        )
    return rendered


__all__ = [
    "FAILURE_LABELS",
    "FINAL_INTERPRETATION_REPORT_FILENAME",
    "FINAL_INTERPRETATION_REPORT_SCHEMA_VERSION",
    "FinalInterpretationReport",
    "FinalInterpretationReportError",
    "build_final_interpretation_report",
    "render_final_interpretation_report_text",
    "validate_final_interpretation_report",
]
