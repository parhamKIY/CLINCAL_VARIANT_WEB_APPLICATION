"""Stage 56 deterministic Final Clinical Report composition."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, TypedDict, cast

from backend.privacy import (
    ClinicalDataPrivacyError,
    validate_human_review_content,
    validate_no_prohibited_fields,
)
from backend.references import (
    CanonicalReferenceError,
    render_canonical_reference_markdown,
    validated_reference_url,
    validate_canonical_reference,
)
from backend.report_lifecycle import (
    ReportLifecycleError,
    validate_variant_report_record,
)
from backend.variant_report import (
    DraftVariantReportError,
    VariantReportContent,
    validate_draft_variant_report,
)


FINAL_CLINICAL_REPORT_SCHEMA_VERSION = "2.1"
MAX_FINAL_CLINICAL_REPORT_BYTES = 4 * 1024 * 1024
FINAL_CLINICAL_REPORT_DISCLAIMER = (
    "This report is clinical decision-support material for review by a "
    "qualified healthcare professional. It does not establish an independent "
    "diagnosis and does not provide treatment or reproductive recommendations."
)
FINAL_CLINICAL_REPORT_SCOPE_NOTICE = (
    "This reviewer-approved evidence-synthesis report records an audited report "
    "disposition. It is not laboratory sign-out and does not independently "
    "classify variant pathogenicity."
)


class FinalClinicalReportError(ValueError):
    """Raised when a Final Clinical Report is incomplete or inconsistent."""


class FinalClinicalReport(TypedDict):
    """Reviewer-approved, deterministic final report artifact."""

    schema_version: str
    report_id: str
    analysis_id: str | None
    generated_at: str
    metadata: dict[str, object]
    phenotype_summary: dict[str, list[str]]
    main_findings: list[dict[str, object]]
    variant_sections: list[dict[str, object]]
    references: list[dict[str, object]]
    method_data_sources: dict[str, object]
    limitations: list[str]
    disclaimer: str
    audit_summary: dict[str, object]


_REPORT_FIELDS = frozenset(FinalClinicalReport.__required_keys__)
_METADATA_FIELDS = frozenset(
    {
        "pipeline_schema_version",
        "draft_report_schema_version",
        "variant_count",
        "selected_variant_count",
        "selected_variant_indexes",
        "assemblies",
        "finalization_state",
    }
)
_PHENOTYPE_FIELDS = frozenset(
    {"accepted_hpo_terms", "matched_hpo_terms"}
)
_FINDING_FIELDS = frozenset(
    {
        "variant_index",
        "report_id",
        "display_label",
        "gene",
        "interpretation_status",
        "interpretation_failure_type",
        "reviewer_summary",
        "interpretation_narrative",
    }
)
_SECTION_FIELDS = frozenset(
    {"variant_index", "report_id", "reviewed_report"}
)
_REFERENCE_GROUP_FIELDS = frozenset(
    {"variant_index", "report_id", "items"}
)
_METHOD_FIELDS = frozenset(
    {"composition_method", "providers", "upstream_sources", "note"}
)
_AUDIT_FIELDS = frozenset(
    {"confirmed_variant_indexes", "selected_reports"}
)
_AUDIT_REPORT_FIELDS = frozenset(
    {
        "variant_index",
        "report_id",
        "edit_count",
        "selection_decision_count",
        "updated_at",
        "confirmation_package_id",
        "confirmed_at",
        "unresolved_interpretation_acknowledgement",
    }
)


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise FinalClinicalReportError("generated_at must be ISO 8601 text.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalClinicalReportError("generated_at must use ISO 8601.") from exc
    if parsed.tzinfo is None:
        raise FinalClinicalReportError("generated_at must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unique_text(values: Sequence[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in result:
            result.append(value)
    return result


def _digest(value: Mapping[str, object]) -> str:
    payload = {key: item for key, item in value.items() if key != "report_id"}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]


def _selected_reviewed_report(
    report: Mapping[str, object],
    lifecycle_record: Mapping[str, object] | None,
) -> dict[str, object]:
    """Project the selected preliminary version into the final read-only view."""

    reviewed = deepcopy(cast(dict[str, object], report["reviewed_report"]))
    if lifecycle_record is None:
        return reviewed
    report_data = cast(dict[str, object], lifecycle_record["report_data"])
    selected = report_data.get("interpretation_version_selection")
    if not isinstance(selected, Mapping) or not selected.get("selection_history"):
        return reviewed
    interpretation = cast(dict[str, object], reviewed["variant_interpretation"])
    projected = cast(dict[str, object], report_data["interpretation"])
    preliminary = cast(dict[str, object], report_data["preliminary_classification"])
    interpretation.update(
        {
            "status": (
                "success"
                if projected["interpretation_status"] == "available"
                else "failed"
            ),
            "narrative": projected["current_reviewer_interpretation"],
            "model": projected["model"],
            "prompt_version": projected["prompt_version"],
            "generated_at": projected["generated_at"],
            "failure_type": projected["failure_type"],
            "conflict_assessment": projected["conflict_assessment"],
            "preliminary_classification_status": preliminary["status"],
            "preliminary_classification": preliminary["classification"],
            "classification_rationale": preliminary["rationale"],
            "limitations": preliminary["limitations"],
        }
    )
    notes = cast(list[str], reviewed["reviewer_notes"])
    revision = selected["selected_revision_number"]
    label = "initial" if revision == 0 else f"revised version {revision}"
    notes.append(f"Final report uses the {label} interpretation.")
    for history in selected["selection_history"]:
        version = history["selected_revision_number"]
        version_label = "initial" if version == 0 else f"revision {version}"
        notes.append(
            "Interpretation selection history: "
            f"{version_label} selected at {history['selected_at']} "
            f"({history['reviewer_context']})."
        )
    return reviewed


def _require_mapping(
    value: object,
    fields: frozenset[str],
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise FinalClinicalReportError(f"{path} has invalid fields.")
    return value


def _require_text(value: object, path: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise FinalClinicalReportError(f"{path} must be non-empty text.")


def _require_text_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise FinalClinicalReportError(f"{path} must be a text list.")
    return cast(list[str], value)


def compose_final_clinical_report(
    pipeline: Mapping[str, object],
    *,
    timestamp: str | None = None,
) -> FinalClinicalReport:
    """Compose only selected reviewer-edited reports without an LLM call."""

    variant_count = pipeline.get("variant_count")
    reports_value = pipeline.get("draft_variant_reports")
    lifecycle_value = pipeline.get("variant_report_records", [])
    packages_value = pipeline.get("reviewed_evidence_packages")
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or variant_count < 0
        or not isinstance(reports_value, list)
        or len(reports_value) != variant_count
        or not isinstance(lifecycle_value, list)
        or (lifecycle_value and len(lifecycle_value) != variant_count)
        or not isinstance(packages_value, list)
        or len(packages_value) != variant_count
    ):
        raise FinalClinicalReportError(
            "Final report composition requires one report and one final "
            "confirmation package per variant."
        )
    try:
        reports = [validate_draft_variant_report(item) for item in reports_value]
        lifecycle_records = [
            validate_variant_report_record(item) for item in lifecycle_value
        ]
    except (DraftVariantReportError, ReportLifecycleError) as exc:
        raise FinalClinicalReportError("Draft Variant Reports are invalid.") from exc
    if [item["variant_index"] for item in reports] != list(range(variant_count)):
        raise FinalClinicalReportError("Draft Variant Reports are out of order.")
    if lifecycle_records and [
        item["variant_index"] for item in lifecycle_records
    ] != list(range(variant_count)):
        raise FinalClinicalReportError("Report lifecycle records are out of order.")
    if any(not isinstance(item, Mapping) for item in packages_value):
        raise FinalClinicalReportError("Final confirmation packages are invalid.")
    packages = cast(list[Mapping[str, object]], packages_value)
    if [item.get("variant_index") for item in packages] != list(range(variant_count)):
        raise FinalClinicalReportError(
            "Final confirmation must cover every variant in input order."
        )
    packages_by_index = {
        cast(int, package["variant_index"]): package for package in packages
    }
    selected_indexes = (
        [
            record["variant_index"]
            for record in lifecycle_records
            if record["report_data"]["review_state"]["include_in_final_report"]
        ]
        if lifecycle_records
        else [
            report["variant_index"]
            for report in reports
            if report["include_in_final_report"]
        ]
    )
    selected = [reports[index] for index in selected_indexes]
    records_by_index = {
        record["variant_index"]: record for record in lifecycle_records
    }
    selected_views = [
        (
            report,
            _selected_reviewed_report(
                report,
                records_by_index.get(report["variant_index"]),
            ),
        )
        for report in selected
    ]
    unresolved_by_index = {
        record["variant_index"]: record["unresolved_interpretation_acknowledgement"]
        for record in lifecycle_records
        if record["lifecycle_state"]
        == "finalized_with_unresolved_interpretation"
    }
    if not unresolved_by_index:
        acknowledgements_value = pipeline.get(
            "unresolved_finalization_acknowledgements",
            (),
        )
        if isinstance(acknowledgements_value, Sequence) and not isinstance(
            acknowledgements_value,
            (str, bytes),
        ):
            unresolved_by_index = {
                item["variant_index"]: dict(item)
                for item in acknowledgements_value
                if isinstance(item, Mapping)
                and isinstance(item.get("variant_index"), int)
                and item.get("variant_index") in selected_indexes
                and reports[item["variant_index"]]["reviewed_report"][
                    "variant_interpretation"
                ]["status"]
                == "failed"
            }
    source_reports = selected or reports
    accepted_hpo = _unique_text(
        [
            term
            for report in source_reports
            for term in report["reviewed_report"]["phenotype_context"][
                "accepted_hpo_terms"
            ]
        ]
    )
    matched_hpo = _unique_text(
        [
            term
            for report in source_reports
            for term in report["reviewed_report"]["phenotype_context"][
                "matched_hpo_terms"
            ]
        ]
    )
    providers = _unique_text(
        [
            provider
            for _, reviewed in selected_views
            for provider in cast(dict[str, object], reviewed["provenance"])["providers"]
        ]
    )
    upstream_sources = _unique_text(
        [
            source
            for _, reviewed in selected_views
            for source in cast(dict[str, object], reviewed["provenance"])[
                "upstream_sources"
            ]
        ]
    )
    limitations = _unique_text(
        [
            limitation
            for _, reviewed in selected_views
            for limitation in cast(list[str], reviewed["limitations"])
        ]
    )
    if not limitations:
        limitations = [
            "No variants were selected for inclusion; no variant-level "
            "interpretation is presented."
        ]
    generated_at = _timestamp(timestamp)
    report: dict[str, object] = {
        "schema_version": FINAL_CLINICAL_REPORT_SCHEMA_VERSION,
        "report_id": "",
        "analysis_id": pipeline.get("analysis_id"),
        "generated_at": generated_at,
        "metadata": {
            "pipeline_schema_version": pipeline.get("schema_version"),
            "draft_report_schema_version": (
                reports[0]["schema_version"] if reports else "unavailable"
            ),
            "variant_count": variant_count,
            "selected_variant_count": len(selected),
            "selected_variant_indexes": [
                report["variant_index"] for report in selected
            ],
            "assemblies": _unique_text(
                [
                    report["reviewed_report"]["variant_summary"]["assembly"]
                    for report in source_reports
                ]
            ),
            "finalization_state": (
                "Finalized with unresolved variants"
                if unresolved_by_index
                else "Finalized"
            ),
        },
        "phenotype_summary": {
            "accepted_hpo_terms": accepted_hpo,
            "matched_hpo_terms": matched_hpo,
        },
        "main_findings": [
            {
                "variant_index": item["variant_index"],
                "report_id": item["report_id"],
                "display_label": cast(dict[str, object], reviewed["variant_summary"])[
                    "display_label"
                ],
                "gene": cast(dict[str, object], reviewed["variant_summary"])["gene"],
                "interpretation_status": cast(dict[str, object], reviewed["variant_interpretation"])["status"],
                "interpretation_failure_type": cast(dict[str, object], reviewed["variant_interpretation"])["failure_type"],
                "reviewer_summary": reviewed["reviewer_summary"],
                "interpretation_narrative": cast(dict[str, object], reviewed["variant_interpretation"])["narrative"],
            }
            for item, reviewed in selected_views
        ],
        "variant_sections": [
            {
                "variant_index": item["variant_index"],
                "report_id": item["report_id"],
                "reviewed_report": deepcopy(reviewed),
            }
            for item, reviewed in selected_views
        ],
        "references": [
            {
                "variant_index": item["variant_index"],
                "report_id": item["report_id"],
                "items": deepcopy(reviewed["literature_references"]),
            }
            for item, reviewed in selected_views
        ],
        "method_data_sources": {
            "composition_method": "deterministic_reviewer_approved_composition",
            "providers": providers,
            "upstream_sources": upstream_sources,
            "note": (
                "Composed from the persisted reviewer-edited report state. "
                "Finalization performs no interpretation regeneration and "
                "makes no additional LLM request."
            ),
        },
        "limitations": limitations,
        "disclaimer": FINAL_CLINICAL_REPORT_DISCLAIMER,
        "audit_summary": {
            "confirmed_variant_indexes": [
                cast(int, package["variant_index"]) for package in packages
            ],
            "selected_reports": [
                {
                    "variant_index": item["variant_index"],
                    "report_id": item["report_id"],
                    "edit_count": len(item["edit_history"]),
                    "selection_decision_count": len(item["selection_history"]),
                    "updated_at": item["updated_at"],
                    "confirmation_package_id": packages_by_index[
                        item["variant_index"]
                    ].get("package_id"),
                    "confirmed_at": packages_by_index[item["variant_index"]].get(
                        "confirmed_at"
                    ),
                    "unresolved_interpretation_acknowledgement": (
                        deepcopy(unresolved_by_index[item["variant_index"]])
                        if item["variant_index"] in unresolved_by_index
                        else None
                    ),
                }
                for item in selected
            ],
        },
    }
    report["report_id"] = f"final-clinical-report-{_digest(report)}"
    return validate_final_clinical_report(report)


def validate_final_clinical_report(value: object) -> FinalClinicalReport:
    """Validate structure, integrity, privacy, and reviewer-state boundaries."""

    report = _require_mapping(value, _REPORT_FIELDS, "final_clinical_report")
    if report["schema_version"] != FINAL_CLINICAL_REPORT_SCHEMA_VERSION:
        raise FinalClinicalReportError("Final report schema version is unsupported.")
    _require_text(report["report_id"], "final_clinical_report.report_id")
    if report["analysis_id"] is not None:
        _require_text(report["analysis_id"], "final_clinical_report.analysis_id")
    if _timestamp(cast(str, report["generated_at"])) != report["generated_at"]:
        raise FinalClinicalReportError("generated_at is not normalized.")
    metadata = _require_mapping(report["metadata"], _METADATA_FIELDS, "metadata")
    variant_count = metadata["variant_count"]
    selected_count = metadata["selected_variant_count"]
    selected_indexes = metadata["selected_variant_indexes"]
    if (
        isinstance(variant_count, bool)
        or not isinstance(variant_count, int)
        or variant_count < 0
        or isinstance(selected_count, bool)
        or not isinstance(selected_count, int)
        or selected_count < 0
        or selected_count > variant_count
        or not isinstance(selected_indexes, list)
        or selected_indexes != sorted(selected_indexes)
        or len(selected_indexes) != len(set(selected_indexes))
        or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= variant_count
            for index in selected_indexes
        )
        or selected_count != len(selected_indexes)
    ):
        raise FinalClinicalReportError("Final report selection metadata is invalid.")
    _require_text(metadata["pipeline_schema_version"], "metadata.pipeline_schema_version")
    _require_text(metadata["draft_report_schema_version"], "metadata.draft_report_schema_version")
    _require_text_list(metadata["assemblies"], "metadata.assemblies")
    finalization_state = metadata["finalization_state"]
    if finalization_state not in {
        "Finalized",
        "Finalized with unresolved variants",
    }:
        raise FinalClinicalReportError("Final report finalization state is invalid.")
    phenotype = _require_mapping(
        report["phenotype_summary"], _PHENOTYPE_FIELDS, "phenotype_summary"
    )
    _require_text_list(phenotype["accepted_hpo_terms"], "phenotype_summary.accepted_hpo_terms")
    _require_text_list(phenotype["matched_hpo_terms"], "phenotype_summary.matched_hpo_terms")
    findings = report["main_findings"]
    sections = report["variant_sections"]
    reference_groups = report["references"]
    if not all(isinstance(item, list) for item in (findings, sections, reference_groups)):
        raise FinalClinicalReportError("Final report variant collections are invalid.")
    if not (len(findings) == len(sections) == len(reference_groups) == selected_count):
        raise FinalClinicalReportError("Final report selected content counts do not match.")
    for position, index in enumerate(selected_indexes):
        finding = _require_mapping(findings[position], _FINDING_FIELDS, f"main_findings[{position}]")
        section = _require_mapping(sections[position], _SECTION_FIELDS, f"variant_sections[{position}]")
        group = _require_mapping(reference_groups[position], _REFERENCE_GROUP_FIELDS, f"references[{position}]")
        if any(item["variant_index"] != index for item in (finding, section, group)):
            raise FinalClinicalReportError("Final report variant order is inconsistent.")
        if not (finding["report_id"] == section["report_id"] == group["report_id"]):
            raise FinalClinicalReportError("Final report report IDs are inconsistent.")
        _require_text(finding["report_id"], f"main_findings[{position}].report_id")
        _require_text(finding["display_label"], f"main_findings[{position}].display_label")
        _require_text(finding["interpretation_status"], f"main_findings[{position}].interpretation_status")
        if finding["interpretation_status"] not in {"success", "failed"}:
            raise FinalClinicalReportError("Final report interpretation status is invalid.")
        if finding["interpretation_failure_type"] is not None:
            _require_text(
                finding["interpretation_failure_type"],
                f"main_findings[{position}].interpretation_failure_type",
            )
        for field in ("gene", "reviewer_summary", "interpretation_narrative"):
            _require_text(finding[field], f"main_findings[{position}].{field}", optional=True)
        reviewed_value = section["reviewed_report"]
        if not isinstance(reviewed_value, dict):
            raise FinalClinicalReportError(
                "Final report reviewed section is invalid."
            )
        reviewed = reviewed_value
        summary = cast(dict[str, object], reviewed.get("variant_summary", {}))
        interpretation = cast(
            dict[str, object],
            reviewed.get("variant_interpretation", {}),
        )
        if any(
            (
                finding["display_label"] != summary.get("display_label"),
                finding["gene"] != summary.get("gene"),
                finding["interpretation_status"]
                != interpretation.get("status"),
                finding["interpretation_failure_type"]
                != interpretation.get("failure_type"),
                finding["reviewer_summary"]
                != reviewed.get("reviewer_summary"),
                finding["interpretation_narrative"]
                != interpretation.get("narrative"),
            )
        ):
            raise FinalClinicalReportError("Final report finding does not match its reviewed section.")
        if group["items"] != reviewed.get("literature_references"):
            raise FinalClinicalReportError("Final report references do not match reviewed content.")
        if not isinstance(group["items"], list):
            raise FinalClinicalReportError("Final report references are invalid.")
        try:
            for reference in group["items"]:
                validate_canonical_reference(reference)
        except CanonicalReferenceError as exc:
            raise FinalClinicalReportError(
                "Final report references are not canonical."
            ) from exc
    methods = _require_mapping(report["method_data_sources"], _METHOD_FIELDS, "method_data_sources")
    if methods["composition_method"] != "deterministic_reviewer_approved_composition":
        raise FinalClinicalReportError("Final report composition method is invalid.")
    _require_text_list(methods["providers"], "method_data_sources.providers")
    _require_text_list(methods["upstream_sources"], "method_data_sources.upstream_sources")
    _require_text(methods["note"], "method_data_sources.note")
    _require_text_list(report["limitations"], "limitations")
    if report["disclaimer"] != FINAL_CLINICAL_REPORT_DISCLAIMER:
        raise FinalClinicalReportError("Final report disclaimer is invalid.")
    audit = _require_mapping(report["audit_summary"], _AUDIT_FIELDS, "audit_summary")
    confirmed_indexes = audit["confirmed_variant_indexes"]
    if confirmed_indexes != list(range(variant_count)):
        raise FinalClinicalReportError("Final report confirmation coverage is invalid.")
    audit_reports = audit["selected_reports"]
    if not isinstance(audit_reports, list) or len(audit_reports) != selected_count:
        raise FinalClinicalReportError("Final report audit summary is invalid.")
    for position, index in enumerate(selected_indexes):
        item = _require_mapping(audit_reports[position], _AUDIT_REPORT_FIELDS, f"audit_summary.selected_reports[{position}]")
        if item["variant_index"] != index or item["report_id"] != sections[position]["report_id"]:
            raise FinalClinicalReportError("Final report audit order is inconsistent.")
        for field in ("edit_count", "selection_decision_count"):
            if isinstance(item[field], bool) or not isinstance(item[field], int) or item[field] < 0:
                raise FinalClinicalReportError("Final report audit counts are invalid.")
        for field in ("updated_at", "confirmation_package_id", "confirmed_at"):
            _require_text(item[field], f"audit_summary.{field}")
        if (
            _timestamp(cast(str, item["updated_at"])) != item["updated_at"]
            or _timestamp(cast(str, item["confirmed_at"]))
            != item["confirmed_at"]
        ):
            raise FinalClinicalReportError(
                "Final report audit timestamps are not normalized."
            )
        acknowledgement = item["unresolved_interpretation_acknowledgement"]
        failed = findings[position]["interpretation_status"] == "failed"
        if failed:
            if not isinstance(acknowledgement, dict):
                raise FinalClinicalReportError(
                    "Selected unresolved interpretation lacks acknowledgement."
                )
            if finalization_state != "Finalized with unresolved variants":
                raise FinalClinicalReportError(
                    "Ordinary finalization cannot contain an unresolved interpretation."
                )
            if acknowledgement.get("failure_type") != findings[position]["interpretation_failure_type"]:
                raise FinalClinicalReportError(
                    "Unresolved finalization failure category is inconsistent."
                )
            for field in ("interpretation_fingerprint", "failure_type", "reason"):
                _require_text(acknowledgement.get(field), f"unresolved_acknowledgement.{field}")
            if _timestamp(cast(str, acknowledgement.get("acknowledged_at"))) != acknowledgement.get("acknowledged_at"):
                raise FinalClinicalReportError(
                    "Unresolved finalization acknowledgement timestamp is invalid."
                )
        elif acknowledgement is not None:
            raise FinalClinicalReportError(
                "Successful finalization cannot retain an unresolved acknowledgement."
            )
    if finalization_state == "Finalized with unresolved variants" and not any(
        finding["interpretation_status"] == "failed" for finding in findings
    ):
        raise FinalClinicalReportError(
            "Unresolved finalization state requires a selected unresolved interpretation."
        )
    try:
        validate_no_prohibited_fields(report, context="Final Clinical Report")
        validate_human_review_content(report, report, [])
    except ClinicalDataPrivacyError as exc:
        raise FinalClinicalReportError(
            "Final Clinical Report contains prohibited clinical data."
        ) from exc
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_FINAL_CLINICAL_REPORT_BYTES:
        raise FinalClinicalReportError("Final Clinical Report exceeds its size limit.")
    if report["report_id"] != f"final-clinical-report-{_digest(report)}":
        raise FinalClinicalReportError("Final Clinical Report failed its integrity check.")
    return cast(FinalClinicalReport, deepcopy(report))


def _md(value: object, fallback: str = "Not available") -> str:
    if value is None or value == "":
        return fallback
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("#", "\\#")
    )


def _list_text(values: object) -> str:
    if not isinstance(values, list) or not values:
        return "Not available"
    return ", ".join(_md(value) for value in values)


def render_final_clinical_report_markdown(value: object) -> str:
    """Render a professional, export-ready report without changing content."""

    report = validate_final_clinical_report(value)
    metadata = report["metadata"]
    phenotype = report["phenotype_summary"]
    lines = [
        "# Final Clinical Report",
        "",
        FINAL_CLINICAL_REPORT_SCOPE_NOTICE,
        "",
        "## Report metadata",
        "",
        f"- Report ID: {_md(report['report_id'])}",
        f"- Analysis ID: {_md(report['analysis_id'], 'Not persisted')}",
        f"- Generated at: {_md(report['generated_at'])}",
        f"- Genome assembly: {_list_text(metadata['assemblies'])}",
        f"- Variants reviewed: {metadata['variant_count']}",
        f"- Variants selected: {metadata['selected_variant_count']}",
        f"- Finalization state: {_md(metadata['finalization_state'])}",
        "",
        "## De-identified phenotype summary",
        "",
        f"- Accepted HPO terms: {_list_text(phenotype['accepted_hpo_terms'])}",
        f"- Matched HPO terms: {_list_text(phenotype['matched_hpo_terms'])}",
        "",
        "## Main findings",
        "",
    ]
    if not report["main_findings"]:
        lines.extend(["No variants were selected for this final report.", ""])
    for number, finding in enumerate(report["main_findings"], start=1):
        summary = finding["reviewer_summary"] or finding["interpretation_narrative"]
        lines.extend(
            [
                f"### Finding {number}: {_md(finding['display_label'])}",
                "",
                f"- Gene: {_md(finding['gene'])}",
                f"- Interpretation status: {_md(finding['interpretation_status'])}",
                *(
                    [
                        "- Interpretation failure category: "
                        f"{_md(finding['interpretation_failure_type'])}"
                    ]
                    if finding["interpretation_failure_type"]
                    else []
                ),
                f"- Approved summary: {_md(summary)}",
                "",
            ]
        )
    lines.extend(["## Detailed reviewed findings", ""])
    for number, section in enumerate(report["variant_sections"], start=1):
        content = cast(VariantReportContent, section["reviewed_report"])
        summary = content["variant_summary"]
        phenotype_context = content["phenotype_context"]
        conflict = content["conflict_summary"]
        interpretation = content["variant_interpretation"]
        lines.extend(
            [
                f"### Variant {number}: {_md(summary['display_label'])}",
                "",
                f"- Gene: {_md(summary['gene'])}",
                f"- Transcript: {_md(summary['transcript'])}",
                f"- HGVS c.: {_md(summary['hgvs_c'])}",
                f"- HGVS p.: {_md(summary['hgvs_p'])}",
                f"- Consequence: {_md(summary['consequence'])}",
                f"- Phenotype status: {_md(phenotype_context['phenotype_status'])}",
                f"- Matched HPO terms: {_list_text(phenotype_context['matched_hpo_terms'])}",
                f"- Conflict status: {_md(conflict['status'])}",
                f"- Conflict severity: {_md(conflict['severity'])}",
                "",
                "### Reviewer-approved interpretation",
                "",
                _md(interpretation["narrative"]),
                "",
                "### Reviewer summary and notes",
                "",
                f"- Summary: {_md(content['reviewer_summary'])}",
                *(
                    [f"- {_md(note)}" for note in content["reviewer_notes"]]
                    or ["- No reviewer notes recorded."]
                ),
                "",
                "### Evidence by source",
                "",
            ]
        )
        for evidence_section in content["evidence_sections"]:
            lines.append(
                f"- {_md(evidence_section['source'])} "
                f"({_md(evidence_section['status'])})"
            )
            for item in evidence_section["items"]:
                lines.append(f"- {_md(item['label'])}: {_md(item['value'])}")
        lines.append("")
    lines.extend(["## Literature References", ""])
    reference_number = 1
    if not report["references"]:
        lines.extend(["No references apply because no variants were selected.", ""])
    for group in report["references"]:
        lines.extend(
            [
                f"### Variant {cast(int, group['variant_index']) + 1}",
                "",
            ]
        )
        for reference in cast(list[object], group["items"]):
            lines.append(
                f"{reference_number}. "
                f"{render_canonical_reference_markdown(reference)}"
            )
            reference_number += 1
        if not group["items"]:
            lines.append("No scientific literature references were available.")
        lines.append("")
    lines.extend(["## Data Sources", ""])
    if not report["variant_sections"]:
        lines.extend(["No data sources apply because no variants were selected.", ""])
    for section in report["variant_sections"]:
        content = section["reviewed_report"]
        lines.extend(
            [
                f"### Variant {cast(int, section['variant_index']) + 1}",
                "",
            ]
        )
        for source in content["data_sources"]:
            details = [source["source"], source["status"], source["capability"]]
            if source["record_identifier"]:
                details.append(source["record_identifier"])
            if source["source"] == "MyVariant.info":
                details.append("Programmatic annotation source")
            label = _md(" — ".join(details)).replace("[", "\\[").replace(
                "]", "\\]"
            )
            url = validated_reference_url(source["human_url"])
            if url is not None and source["link_status"] == "validated":
                lines.append(f"- [{label}]({url})")
            else:
                lines.append(f"- {label}")
        if not content["data_sources"]:
            lines.append("No database or tool provenance was recorded.")
        lines.append("")
    methods = report["method_data_sources"]
    lines.extend(
        [
            "## Method and data-source note",
            "",
            _md(methods["note"]),
            "",
            f"- Providers: {_list_text(methods['providers'])}",
            f"- Upstream sources: {_list_text(methods['upstream_sources'])}",
            "",
            "## Limitations",
            "",
            *[f"- {_md(item)}" for item in report["limitations"]],
            "",
            "## Audit and provenance summary",
            "",
            f"- Confirmed variant indexes: {_list_text(report['audit_summary']['confirmed_variant_indexes'])}",
            f"- Selected report count: {len(report['audit_summary']['selected_reports'])}",
        ]
    )
    for item in report["audit_summary"]["selected_reports"]:
        lines.append(
            f"- Variant {cast(int, item['variant_index']) + 1}: "
            f"{item['edit_count']} edit(s), "
            f"{item['selection_decision_count']} selection decision(s), "
            f"confirmed {_md(item['confirmed_at'])}."
        )
    lines.extend(
        [
            "",
            "## Medical disclaimer",
            "",
            _md(report["disclaimer"]),
        ]
    )
    return "\n".join(lines).strip() + "\n"


def render_final_clinical_report_text(value: object) -> str:
    """Return the bounded UTF-8 text/Markdown export source."""

    return render_final_clinical_report_markdown(value)


__all__ = [
    "FINAL_CLINICAL_REPORT_DISCLAIMER",
    "FINAL_CLINICAL_REPORT_SCOPE_NOTICE",
    "FINAL_CLINICAL_REPORT_SCHEMA_VERSION",
    "FinalClinicalReport",
    "FinalClinicalReportError",
    "compose_final_clinical_report",
    "render_final_clinical_report_markdown",
    "render_final_clinical_report_text",
    "validate_final_clinical_report",
]
