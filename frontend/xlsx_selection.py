"""Presentation-safe contracts for explicit XLSX source-row selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from config import MAX_VARIANTS_PER_ANALYSIS


class XLSXSelectionError(ValueError):
    """Raised when an XLSX selection is outside the selected-input contract."""


def select_excel_input_records(
    records: Sequence[Mapping[str, object]],
    selected_rows: Sequence[object],
) -> list[dict[str, object]]:
    """Return exactly user-selected source rows in workbook order, without ranking."""

    if not selected_rows:
        raise XLSXSelectionError("Select at least one candidate row before analysis.")
    if len(selected_rows) > MAX_VARIANTS_PER_ANALYSIS:
        raise XLSXSelectionError(
            f"You selected {len(selected_rows)} variants. This application accepts "
            f"a maximum of {MAX_VARIANTS_PER_ANALYSIS} user-selected variants per "
            f"analysis. Please deselect at least "
            f"{len(selected_rows) - MAX_VARIANTS_PER_ANALYSIS} variants."
        )
    selected = set(selected_rows)
    available = {record.get("row") for record in records}
    if len(selected) != len(selected_rows) or not selected.issubset(available):
        raise XLSXSelectionError("The selected XLSX rows are no longer available.")
    return [dict(record) for record in records if record.get("row") in selected]


def _display_text(value: object) -> str | None:
    """Safely format string-intended columns for tabular display without dtype collisions."""
    if value is None:
        return None
    return str(value)


def source_row_display_rows(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Project source provenance for review; FILTER remains source metadata."""

    return [
        {
            "Source row": record.get("row"),
            "CHROM": _display_text(record.get("chrom")),
            "Start": record.get("start"),
            "End": record.get("end"),
            "REF": _display_text(record.get("ref")),
            "ALT": _display_text(record.get("alt")),
            "QUAL": record.get("qual"),
            "Source FILTER": _display_text(record.get("filter")),
            "DP": record.get("depth"),
            "AD": record.get("ad"),
            "GQ": record.get("gq"),
        }
        for record in records
    ]


def build_preprocessing_summary(
    results: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Count selected inputs separately from canonical downstream variants."""

    statuses = [item.get("status") for item in results]
    return {
        "selected_count": len(results),
        "canonical_count": sum(
            item.get("canonical_variant_index") is not None for item in results
        ),
        "unresolved_count": statuses.count("IDENTITY_UNRESOLVED"),
        "accepted_direct_count": statuses.count("ACCEPTED_DIRECT"),
        "normalized_count": statuses.count("NORMALIZED_AND_ACCEPTED"),
    }


def selected_source_rows_message(count: int) -> str:
    """Describe selection as pending validation, not accepted identity."""

    return (
        f"{count} source {'row' if count == 1 else 'rows'} selected. "
        f"{'Its' if count == 1 else 'Their'} variant "
        f"{'identity' if count == 1 else 'identities'} will be validated "
        "when analysis starts."
    )


__all__ = [
    "XLSXSelectionError",
    "build_preprocessing_summary",
    "select_excel_input_records",
    "selected_source_rows_message",
    "source_row_display_rows",
]
