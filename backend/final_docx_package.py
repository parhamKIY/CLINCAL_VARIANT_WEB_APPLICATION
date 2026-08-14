"""Deterministic Stage 100 package of reviewer-approved per-variant DOCX files."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from io import BytesIO
from typing import TypedDict, cast
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from backend.final_clinical_report import (
    FinalClinicalReportError,
    validate_final_clinical_report,
)
from backend.report_docx import ReportDocxError, render_report_data_docx
from backend.report_lifecycle import (
    ReportLifecycleError,
    VariantReportRecord,
    validate_variant_report_record,
)


FINAL_DOCX_PACKAGE_SCHEMA_VERSION = "1.0"
MAX_FINAL_DOCX_PACKAGE_BYTES = 55 * 1024 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class FinalDocxPackageError(ValueError):
    """Raised when approved report state cannot produce a safe final package."""


class FinalDocxPackage(TypedDict):
    """One deterministic download containing selected editable Word reports."""

    filename: str
    data: bytes
    manifest: dict[str, object]


def _zip_info(filename: str, *, compressed: bool) -> ZipInfo:
    info = ZipInfo(filename, date_time=_ZIP_TIMESTAMP)
    info.compress_type = ZIP_DEFLATED if compressed else ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def _validated_final_records(value: Mapping[str, object]) -> tuple[
    list[VariantReportRecord],
    dict[str, object],
]:
    if value.get("workflow_state") != "completed":
        raise FinalDocxPackageError(
            "The editable final package requires a completed analysis."
        )
    raw_records = value.get("variant_report_records")
    variant_count = value.get("variant_count")
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or variant_count < 0
        or not isinstance(raw_records, list)
        or len(raw_records) != variant_count
    ):
        raise FinalDocxPackageError(
            "The final package requires one report record per input variant."
        )
    try:
        records = [validate_variant_report_record(item) for item in raw_records]
        final_report = validate_final_clinical_report(
            value.get("final_clinical_report")
        )
    except (FinalClinicalReportError, ReportLifecycleError) as exc:
        raise FinalDocxPackageError(
            "The approved final report state is invalid."
        ) from exc
    if [record["variant_index"] for record in records] != list(
        range(variant_count)
    ):
        raise FinalDocxPackageError(
            "Final report records must preserve original input order."
        )
    if any(record["lifecycle_state"] != "finalized" for record in records):
        raise FinalDocxPackageError(
            "Every report must be confirmed and finalized before export."
        )
    if final_report["analysis_id"] != value.get("analysis_id"):
        raise FinalDocxPackageError(
            "The final report does not belong to this analysis."
        )
    return records, cast(dict[str, object], final_report)


def build_final_docx_package(value: object) -> FinalDocxPackage:
    """Package selected finalized ReportData as independent editable DOCX files."""

    if not isinstance(value, Mapping):
        raise FinalDocxPackageError("Final package input must be pipeline state.")
    records, final_report = _validated_final_records(value)
    metadata = cast(dict[str, object], final_report["metadata"])
    selected_indexes = cast(list[int], metadata["selected_variant_indexes"])
    if not selected_indexes:
        raise FinalDocxPackageError(
            "No reports are selected for the editable final package."
        )
    sections = cast(list[dict[str, object]], final_report["variant_sections"])
    if [section["report_id"] for section in sections] != [
        records[index]["report_id"] for index in selected_indexes
    ]:
        raise FinalDocxPackageError(
            "The selected final report does not match the report lifecycle."
        )

    rendered: list[tuple[VariantReportRecord, bytes]] = []
    manifest_reports: list[dict[str, object]] = []
    for record in (records[index] for index in selected_indexes):
        if not record["report_data"]["review_state"][
            "include_in_final_report"
        ]:
            raise FinalDocxPackageError(
                "The final package selection is inconsistent."
            )
        try:
            data = render_report_data_docx(record["report_data"])
        except ReportDocxError as exc:
            raise FinalDocxPackageError(
                "A selected editable Word report could not be rendered."
            ) from exc
        artifact = record["docx_artifact"]
        if (
            len(data) != artifact["size_bytes"]
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
        ):
            raise FinalDocxPackageError(
                "A selected DOCX no longer matches its approved artifact."
            )
        rendered.append((record, data))
        identity = record["report_data"]["variant_identity"]
        manifest_reports.append(
            {
                "variant_index": record["variant_index"],
                "variant_id": (
                    f"{identity['genome_build']}:{identity['chromosome']}:"
                    f"{identity['position']}:{identity['reference']}:"
                    f"{identity['alternate']}"
                ),
                "report_id": record["report_id"],
                "filename": artifact["filename"],
                "report_version": artifact["sha256"][:16],
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "template_version": artifact["template_version"],
                "confirmed_at": record["confirmed_at"],
                "finalized_at": record["finalized_at"],
            }
        )

    manifest: dict[str, object] = {
        "schema_version": FINAL_DOCX_PACKAGE_SCHEMA_VERSION,
        "analysis_id": value.get("analysis_id"),
        "final_report_id": final_report["report_id"],
        "selected_report_count": len(rendered),
        "selected_variant_indexes": selected_indexes,
        "reports": manifest_reports,
    }
    manifest_data = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8")
    output = BytesIO()
    with ZipFile(output, "w", allowZip64=False) as archive:
        archive.writestr(_zip_info("manifest.json", compressed=True), manifest_data)
        for record, data in rendered:
            archive.writestr(
                _zip_info(record["docx_artifact"]["filename"], compressed=False),
                data,
            )
    package_data = output.getvalue()
    if (
        not package_data.startswith(b"PK")
        or len(package_data) > MAX_FINAL_DOCX_PACKAGE_BYTES
    ):
        raise FinalDocxPackageError(
            "The editable final package is invalid or too large."
        )
    return {
        "filename": f"{final_report['report_id']}-editable-docx-reports.zip",
        "data": package_data,
        "manifest": manifest,
    }


__all__ = [
    "FINAL_DOCX_PACKAGE_SCHEMA_VERSION",
    "MAX_FINAL_DOCX_PACKAGE_BYTES",
    "FinalDocxPackage",
    "FinalDocxPackageError",
    "build_final_docx_package",
]
