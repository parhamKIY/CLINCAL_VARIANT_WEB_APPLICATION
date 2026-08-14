"""Transient Draft Variant Report V2 to ReportData V4 projection."""

from __future__ import annotations

import re
from typing import cast

from backend.conflict_auditor import normalize_classification_label
from backend.report_data import ReportData, validate_report_data
from backend.variant_report import (
    DraftVariantReport,
    EvidenceSection,
    validate_draft_variant_report,
)


_HPO_PATTERN = re.compile(r"HP:[0-9]{7}")


def _availability(status: str) -> str:
    normalized = status.strip().lower().replace(" ", "_")
    if normalized in {"success", "available", "available_via_fallback"}:
        return "available"
    if normalized in {"not_found", "no_match", "no_exact_match"}:
        return "no_match"
    if normalized in {"unsupported", "not_supported"}:
        return "unsupported"
    if normalized in {"skipped", "not_assessed", "not_applicable"}:
        return "not_assessed"
    return "unavailable"


def _section_values(section: EvidenceSection) -> dict[str, str]:
    return {item["label"]: item["value"] for item in section["items"]}


def _section(
    report: DraftVariantReport,
    name: str,
) -> EvidenceSection | None:
    for item in report["reviewed_report"]["evidence_sections"]:
        if name.lower() in item["source"].lower():
            return item
    return None


def _number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _hpo_terms(values: list[str]) -> list[dict[str, str | None]]:
    retained: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for value in values:
        match = _HPO_PATTERN.search(value)
        if match is None or match.group(0) in seen:
            continue
        identifier = match.group(0)
        seen.add(identifier)
        retained.append({"hpo_id": identifier, "label": None})
    return retained


def _classifications(
    report: DraftVariantReport,
) -> tuple[
    list[dict[str, object]],
    str | None,
    str | None,
    str | None,
    bool,
    str | None,
]:
    findings: list[dict[str, object]] = []
    clinvar_value: str | None = None
    automated_value: str | None = None
    derived_value: str | None = None
    clinvar = _section(report, "ClinVar")
    if clinvar is not None:
        values = _section_values(clinvar)
        clinvar_value = values.get("Significance")
        status = _availability(clinvar["status"])
        if clinvar_value:
            status = "available"
        elif status == "available":
            status = "not_assessed"
        findings.append(
            {
                "source": clinvar["source"],
                "classification": clinvar_value,
                "review_status": values.get("Review status"),
                "criteria": [],
                "independent_evidence": True,
                "status": status,
            }
        )
    genebe = _section(report, "GeneBe")
    if genebe is not None:
        values = _section_values(genebe)
        automated_value = values.get("Automated ACMG classification")
        criteria_value = values.get("ACMG criteria")
        status = _availability(genebe["status"])
        if automated_value:
            status = "available"
        elif status == "available":
            status = "not_assessed"
        findings.append(
            {
                "source": genebe["source"],
                "classification": automated_value,
                "review_status": "Automated annotation only",
                "criteria": (
                    [
                        item.strip()
                        for item in re.split(r"[,;]", criteria_value)
                        if item.strip()
                    ]
                    if criteria_value
                    else []
                ),
                "independent_evidence": False,
                "status": status,
            }
        )
    audit = _section(report, "Classification evidence audit")
    audit_state: str | None = None
    if audit is not None:
        values = _section_values(audit)
        audit_state = values.get("State")
        derived_value = values.get(
            "MyVariant ClinVar-derived classification"
        )
        if derived_value:
            findings.append(
                {
                    "source": "MyVariant.info (ClinVar-derived rescue)",
                    "classification": derived_value,
                    "review_status": "Non-independent derived evidence",
                    "criteria": [],
                    "independent_evidence": False,
                    "status": "available",
                }
            )
    normalized = {
        normalized
        for value in (clinvar_value, automated_value, derived_value)
        if (normalized := normalize_classification_label(value)) is not None
    }
    if audit_state == "CONFLICTING_CLASSIFICATIONS" or len(normalized) > 1:
        return (
            findings,
            clinvar_value,
            automated_value,
            derived_value,
            True,
            "Multiple source-attributed classifications",
        )
    return (
        findings,
        clinvar_value,
        automated_value,
        derived_value,
        False,
        None,
    )


def build_report_data_from_draft(
    value: object,
    *,
    analysis_id: str,
) -> ReportData:
    """Project current reviewed fields into a validated transient ReportData V4."""

    report = validate_draft_variant_report(value)
    content = report["reviewed_report"]
    original = report["machine_original_report"]
    variant = content["variant_summary"]
    phenotype = content["phenotype_context"]
    interpretation = content["variant_interpretation"]
    quality = content["call_quality"]
    (
        classification_findings,
        clinvar,
        automated,
        _derived,
        source_conflict,
        source_conflict_summary,
    ) = (
        _classifications(report)
    )

    accepted = _hpo_terms(phenotype["accepted_hpo_terms"])
    matched_ids = {
        item["hpo_id"] for item in _hpo_terms(phenotype["matched_hpo_terms"])
    }
    matched = [item for item in accepted if item["hpo_id"] in matched_ids]
    phenotype_status = phenotype["phenotype_status"]
    concordance = {
        "exact_match": "supported",
        "partial_match": "partially_supported",
        "no_exact_match": "no_supported_association",
        "not_applicable": "not_assessed",
    }.get(phenotype_status, "unavailable")
    phenotype_summary = " ".join(
        [
            *phenotype["phenotype_to_gene_summary"],
            *phenotype["disease_context"],
        ]
    ) or None
    phenotype_score = phenotype["phenotype_score"]
    if phenotype_score is not None and phenotype_score > 1.0:
        phenotype_score = (
            phenotype_score / 100.0
            if phenotype_score <= 100.0
            else None
        )
    genome_build = {
        "hg19": "GRCh37",
        "hg38": "GRCh38",
    }.get(variant["assembly"].lower(), variant["assembly"])

    population_findings: list[dict[str, object]] = []
    for section in content["evidence_sections"]:
        source_lower = section["source"].lower()
        if "population" not in source_lower and "gnomad" not in source_lower:
            continue
        values = _section_values(section)
        frequency = _number(
            values.get("Allele frequency")
            or values.get("Population frequency")
        )
        fallback = "fallback" in source_lower
        status = "available" if frequency is not None else _availability(
            section["status"]
        )
        if status == "available" and frequency is None:
            status = "not_assessed"
        population_findings.append(
            {
                "source": section["source"],
                "dataset": values.get("Dataset"),
                "frequency": frequency,
                "status": status,
                "provider_role": "fallback" if fallback else "primary",
                "fallback_used": fallback,
            }
        )

    disease_associations = [
        {
            "source": "Retained disease context",
            "disease_name": item,
            "disease_id": None,
            "inheritance": None,
            "relationship_scope": "gene_disease_context",
            "status": "available",
        }
        for item in phenotype["disease_context"][:20]
    ]
    stable_ids = (
        [
            {
                "source": "Draft Variant Report exact mapping",
                "identifier_type": "rsID",
                "identifier": variant["rsid"],
                "status": "available",
            }
        ]
        if variant["rsid"]
        else []
    )

    narrative_edits: list[dict[str, object]] = []
    for edit in report["edit_history"]:
        if edit["field_path"] != "/variant_interpretation/narrative":
            continue
        narrative_edits.append(
            {
                "sequence": len(narrative_edits) + 1,
                "old_value": edit["old_value"],
                "new_value": edit["new_value"],
                "timestamp": edit["timestamp"],
                "reviewer_context": edit["reviewer_context"],
            }
        )

    conflict = content["conflict_summary"]
    source_attributions = [
        cast(str, item["source"])
        for item in classification_findings
        if item["status"] == "available"
    ]
    warning_messages = [
        *interpretation["warnings"],
        *content["limitations"],
    ][:50]
    projected: dict[str, object] = {
        "schema_version": "4.0",
        "report_id": report["report_id"],
        "analysis_id": analysis_id,
        "input_index": report["variant_index"],
        "variant_identity": {
            "genome_build": genome_build,
            "chromosome": variant["chrom"],
            "position": variant["pos"],
            "reference": variant["ref"],
            "alternate": variant["alt"],
            "gene": variant["gene"],
            "gene_id": variant["gene_id"],
            "transcript": variant["transcript"],
            "hgvs_c": variant["hgvs_c"],
            "hgvs_p": variant["hgvs_p"],
            "zygosity": None,
        },
        "phenotype_summary": {
            "accepted_hpo_terms": accepted,
            "matched_hpo_terms": matched,
            "concordance": concordance,
            "score": phenotype_score,
            "summary": phenotype_summary,
            "evidence_status": (
                "available" if accepted else "not_assessed"
            ),
        },
        "conclusive_result": {
            "gene": variant["gene"],
            "hgvs_c": variant["hgvs_c"],
            "hgvs_p": variant["hgvs_p"],
            "zygosity": None,
            "classification": None,
            "classification_source": None,
            "status": "not_assessed",
        },
        "call_quality": {
            "qual": quality["qual"],
            "filter": quality["filter"],
            "status": quality["status"],
            "acknowledged_at": quality["acknowledged_at"],
            "override_reason": (
                None if quality["override"] is None else quality["override"]["reason"]
            ),
            "override_timestamp": (
                None if quality["override"] is None else quality["override"]["timestamp"]
            ),
        },
        "main_findings": {
            "population_frequencies": population_findings,
            "disease_associations": disease_associations,
            "computational_evidence": [],
            "stable_variant_ids": stable_ids,
            "classifications": classification_findings,
        },
        "interpretation": {
            "original_model_interpretation": original[
                "variant_interpretation"
            ]["narrative"],
            "current_reviewer_interpretation": interpretation["narrative"],
            "model": interpretation["model"],
            "prompt_version": interpretation["prompt_version"],
            "generated_at": interpretation["generated_at"],
            "edit_history": narrative_edits,
            "interpretation_status": (
                "available" if interpretation["narrative"] else "unavailable"
            ),
            "failure_type": interpretation["failure_type"],
            "conflict_assessment": interpretation["conflict_assessment"],
        },
        "classification_summary": {
            "reviewer_confirmed_classification": None,
            "clinvar_classification": clinvar,
            "automated_classification": automated,
            "conflict_status": (
                "conflict"
                if conflict["detected"]
                or source_conflict
                else "none"
            ),
            "conflict_severity": conflict["severity"],
            "source_attributions": source_attributions,
            "independent_acmg_adjudication": False,
            "summary": source_conflict_summary or interpretation["conflict_assessment"],
        },
        "literature_references": [
            item
            for item in content["literature_references"]
            if item["identifier_type"] in {"PMID", "PMCID", "DOI"}
        ],
        "data_sources": content["data_sources"],
        "warnings": [
            {
                "severity": "PARTIAL",
                "code": f"DRAFT_REPORT_WARNING_{index + 1}",
                "message": message,
                "capability": None,
            }
            for index, message in enumerate(warning_messages)
        ],
        "provenance": {
            "source_report_schema_version": report["schema_version"],
            "evidence_schema_version": content["provenance"][
                "evidence_schema_version"
            ],
            "interpretation_schema_version": content["provenance"][
                "interpretation_schema_version"
            ],
            "providers": content["provenance"]["providers"],
            "upstream_sources": content["provenance"]["upstream_sources"],
            "generated_at": report["updated_at"],
        },
        "review_state": {
            "review_status": "draft",
            "include_in_final_report": report["include_in_final_report"],
            "confirmed_at": None,
            "reviewer_summary": content["reviewer_summary"],
            "reviewer_notes": content["reviewer_notes"],
            "selection_history": report["selection_history"],
        },
        "template_version": "professor-report-v1",
    }
    return validate_report_data(projected)


__all__ = ["build_report_data_from_draft"]
