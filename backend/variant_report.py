"""Draft Variant Report V2 contracts and deterministic composition."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal, TypedDict, cast

from backend.fallback_transparency import build_fallback_notices
from backend.privacy import (
    ClinicalDataPrivacyError,
    validate_human_review_content,
    validate_no_prohibited_fields,
)
from backend.provider_resilience import capability_availability
from backend.reference_model import (
    DataSourceRecord,
    REFERENCE_MODEL_SCHEMA_VERSION,
    ReferenceModelError,
    build_reference_model_v2,
    validate_data_source_record,
    validate_reference_model_v2,
)
from backend.references import (
    CanonicalReference,
    CanonicalReferenceError,
    cited_reference_ids,
    validate_canonical_reference,
)
from backend.report import EvidenceObject, validate_evidence_object
from backend.variant_interpretation import (
    VariantInterpretationResult,
    validate_variant_interpretation_result,
)


DRAFT_VARIANT_REPORT_SCHEMA_VERSION = "2.2"
MAX_DRAFT_VARIANT_REPORT_BYTES = 128 * 1024
MAX_REPORT_TEXT_CHARS = 20_000
MAX_EVIDENCE_VALUE_CHARS = 4_000
MAX_REPORT_LIST_ITEMS = 50
MAX_EVIDENCE_SECTIONS = 16
MAX_EVIDENCE_ITEMS_PER_SECTION = 20
MAX_REPORT_REFERENCES = 50
MAX_REPORT_EDIT_HISTORY = 200
MAX_SELECTION_HISTORY = 200
MAX_REVIEWER_NOTES = 50
MAX_REVIEWER_CONTEXT_CHARS = 200


class DraftVariantReportError(ValueError):
    """Raised when a Draft Variant Report violates the V2 contract."""


class VariantSummary(TypedDict):
    """Compact normalized identity and transcript context."""

    display_label: str
    assembly: str
    chrom: str
    pos: int
    ref: str
    alt: str
    gene: str | None
    gene_id: str | None
    transcript: str | None
    hgvs_c: str | None
    hgvs_p: str | None
    consequence: str | None
    rsid: str | None


class PhenotypeContext(TypedDict):
    """Accepted HPO and evidence-backed relationship summary."""

    accepted_hpo_terms: list[str]
    matched_hpo_terms: list[str]
    phenotype_score: float | None
    phenotype_status: str
    phenotype_to_gene_summary: list[str]
    disease_context: list[str]


class EvidenceItem(TypedDict):
    """One readable label/value pair derived from machine evidence."""

    label: str
    value: str


class EvidenceSection(TypedDict):
    """One provider-aware block in the report evidence table."""

    source: str
    status: str
    items: list[EvidenceItem]


class ConflictSummary(TypedDict):
    """Deterministic conflict result supplied to the interpretation model."""

    detected: bool
    status: str
    severity: str
    findings: list[str]


class InterpretationSection(TypedDict):
    """Validated model result or an explicit unavailable state."""

    status: str
    narrative: str | None
    conflict_assessment: str | None
    warnings: list[str]
    model: str
    prompt_version: str
    generated_at: str
    failure_type: str | None


class ReportProvenance(TypedDict):
    """Compact provenance kept accessible outside the main evidence table."""

    evidence_schema_version: str
    interpretation_schema_version: str
    providers: list[str]
    upstream_sources: list[str]
    interpretation_model: str
    interpretation_prompt_version: str


class VariantReportContent(TypedDict):
    """Professional per-variant finding structure shown to reviewers."""

    variant_summary: VariantSummary
    phenotype_context: PhenotypeContext
    evidence_sections: list[EvidenceSection]
    conflict_summary: ConflictSummary
    variant_interpretation: InterpretationSection
    reviewer_summary: str | None
    reviewer_notes: list[str]
    literature_references: list[CanonicalReference]
    data_sources: list[DataSourceRecord]
    provenance: ReportProvenance
    limitations: list[str]


class ReportEditRecord(TypedDict):
    """One bounded append-only change to an explicitly editable field."""

    sequence: int
    field_path: str
    change_type: Literal["modified"]
    old_value: str | list[str] | None
    new_value: str | list[str] | None
    timestamp: str
    reviewer_context: str | None


class SelectionRecord(TypedDict):
    """One append-only human final-report inclusion decision."""

    sequence: int
    old_value: bool
    new_value: bool
    timestamp: str
    reviewer_context: str | None


class DraftVariantReport(TypedDict):
    """Immutable machine original plus a traceable reviewer-edited layer."""

    schema_version: str
    report_id: str
    variant_index: int
    machine_original_report: VariantReportContent
    reviewed_report: VariantReportContent
    edit_history: list[ReportEditRecord]
    include_in_final_report: bool
    selection_history: list[SelectionRecord]
    review_status: str
    created_at: str
    updated_at: str


_REPORT_FIELDS = frozenset(DraftVariantReport.__required_keys__)
_CONTENT_FIELDS = frozenset(VariantReportContent.__required_keys__)
_VARIANT_FIELDS = frozenset(VariantSummary.__required_keys__)
_PHENOTYPE_FIELDS = frozenset(PhenotypeContext.__required_keys__)
_EVIDENCE_SECTION_FIELDS = frozenset(EvidenceSection.__required_keys__)
_EVIDENCE_ITEM_FIELDS = frozenset(EvidenceItem.__required_keys__)
_CONFLICT_FIELDS = frozenset(ConflictSummary.__required_keys__)
_INTERPRETATION_FIELDS = frozenset(InterpretationSection.__required_keys__)
_PROVENANCE_FIELDS = frozenset(ReportProvenance.__required_keys__)
_EDIT_RECORD_FIELDS = frozenset(ReportEditRecord.__required_keys__)
_SELECTION_RECORD_FIELDS = frozenset(SelectionRecord.__required_keys__)
_EDITABLE_PATHS = (
    "/reviewer_summary",
    "/variant_interpretation/narrative",
    "/variant_interpretation/conflict_assessment",
    "/reviewer_notes",
)


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _display(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list):
        parts = [_display(item) for item in value[:MAX_REPORT_LIST_ITEMS]]
        return ", ".join(part for part in parts if part) or None
    return None


def _item(label: str, value: object) -> EvidenceItem | None:
    displayed = _display(value)
    if displayed is None:
        return None
    return {"label": label, "value": displayed[:MAX_EVIDENCE_VALUE_CHARS]}


def _items(*candidates: EvidenceItem | None) -> list[EvidenceItem]:
    return [item for item in candidates if item is not None]


def _status(value: object, fallback: str = "not_found") -> str:
    return _text(value) or fallback


def _provider_status(evidence: EvidenceObject, source: str) -> str:
    return _status(evidence["source_statuses"].get(source), "unavailable")


def _capability_status(evidence: EvidenceObject, capability: str) -> str:
    return capability_availability(
        evidence["capability_results"][capability]
    )


def _provider_record(
    evidence: EvidenceObject,
    source: str,
) -> dict[str, object]:
    providers = evidence["provenance"].get("providers")
    if not isinstance(providers, list):
        return {}
    return next(
        (
            item
            for item in providers
            if isinstance(item, dict) and item.get("source") == source
        ),
        {},
    )


def _disease_context(evidence: EvidenceObject) -> list[str]:
    relationship = evidence["phenotype_relationship"]
    mydisease = _mapping(relationship.get("mydisease"))
    diseases = mydisease.get("diseases")
    summaries: list[str] = []
    if isinstance(diseases, list):
        for disease in diseases[:MAX_REPORT_LIST_ITEMS]:
            record = _mapping(disease)
            name = _text(record.get("name")) or _text(
                record.get("disease_name")
            )
            identifier = _text(record.get("mondo_id")) or _text(
                record.get("disease_id")
            )
            if name and identifier:
                summaries.append(f"{name} ({identifier})")
            elif name or identifier:
                summaries.append(cast(str, name or identifier))
    return summaries


def _phen2gene_summary(evidence: EvidenceObject) -> list[str]:
    phen2gene = _mapping(
        evidence["phenotype_relationship"].get("phen2gene")
    )
    capability = evidence["capability_results"]["phenotype_gene"]
    return [
        f"{item['label']}: {item['value']}"
        for item in _items(
            _item(
                "Phenotype-gene availability",
                capability_availability(capability),
            ),
            _item("Phenotype-gene provider", capability["provider"]),
            _item("Phenotype-gene rank", phen2gene.get("rank")),
            _item("Phenotype-gene score", phen2gene.get("score")),
            _item("Phenotype-gene method", capability["method"]),
            _item(
                "Primary provider failure",
                capability["primary_failure"],
            ),
            _item(
                "Local dataset version",
                phen2gene.get("dataset_version"),
            ),
            _item(
                "Local dataset date",
                phen2gene.get("dataset_date"),
            ),
        )
    ]


def _evidence_sections(evidence: EvidenceObject) -> list[EvidenceSection]:
    context = evidence["variant_context"]
    annotations = evidence["annotations"]
    pathogenicity = evidence["pathogenicity"]
    enrichment = evidence["conditional_enrichment"]
    genebe = _mapping(annotations.get("genebe"))
    population = _mapping(enrichment.get("population_frequency"))
    literature = _mapping(enrichment.get("literature"))
    cspec = pathogenicity["cspec_context"]
    cspec_metadata = _mapping(cspec[0]) if cspec else {}
    cspec_capability = evidence["capability_results"]["cspec_context"]
    cspec_provenance = _mapping(cspec_capability.get("provenance"))
    cspec_query_scope = _mapping(cspec_provenance.get("query_scope"))
    cspec_status = _capability_status(evidence, "cspec_context")
    if (
        cspec_status == "no_match"
        and cspec_provenance.get("applicability_status")
        == "no_applicable_specification"
    ):
        cspec_status = (
            "No applicable specification for current gene/disease scope"
        )
    clingen = pathogenicity["clingen_context"]
    clinvar_provider = _provider_record(evidence, "clinvar")
    cspec_provider = _provider_record(evidence, "cspec")
    fallback_notices = {
        notice["capability"]: notice
        for notice in build_fallback_notices(
            evidence["capability_results"]
        )
    }

    def source_label(primary: str, capability: str) -> str:
        notice = fallback_notices.get(capability)
        if notice is None:
            return primary
        return (
            f"{notice['capability_label']} — fallback: "
            f"{notice['fallback_provider_label']}"
        )

    sections: list[EvidenceSection] = [
        {
            "source": source_label("Ensembl VEP", "variant_annotation"),
            "status": _capability_status(evidence, "variant_annotation"),
            "items": _items(
                _item("Consequence", context["consequence"]),
                _item("Transcript", context["transcript"]),
                _item("Coding HGVS", context["hgvs_c"]),
                _item("Protein HGVS", context["hgvs_p"]),
                _item("Impact", evidence["impact"]),
            ),
        },
        {
            "source": "GeneBe",
            "status": _status(genebe.get("status")),
            "items": _items(
                _item(
                    "Automated ACMG classification",
                    pathogenicity["automated_acmg_classification"],
                ),
                _item("ACMG criteria", pathogenicity["acmg_criteria"]),
            ),
        },
        {
            "source": source_label("MyVariant.info", "variant_context"),
            "status": _capability_status(evidence, "variant_context"),
            "items": _items(
                _item(
                    "Population frequency",
                    evidence["population_frequency"],
                ),
                _item(
                    "Variant identifier",
                    _mapping(annotations["population"]).get("variant_id"),
                ),
            ),
        },
        {
            "source": source_label("NCBI ClinVar", "clinvar_evidence"),
            "status": _capability_status(evidence, "clinvar_evidence"),
            "items": _items(
                _item("Accession", evidence["clinvar_accession"]),
                _item("Significance", pathogenicity["clinvar_classification"]),
                _item("Review status", pathogenicity["clinvar_review_status"]),
                _item("Conditions", pathogenicity["clinvar_conditions"]),
                _item(
                    "Primary failure",
                    clinvar_provider.get("primary_failure"),
                ),
            ),
        },
        {
            "source": "ClinGen / GenCC",
            "status": _provider_status(evidence, "clingen"),
            "items": _items(
                _item("Matching curations", len(clingen)),
                _item(
                    "Classifications",
                    [record.get("classification") for record in clingen],
                ),
                _item(
                    "Diseases",
                    [record.get("disease") for record in clingen],
                ),
            ),
        },
        {
            "source": source_label("ClinGen CSpec", "cspec_context"),
            "status": cspec_status,
            "items": _items(
                _item("Matching specifications", len(cspec)),
                _item(
                    "Specification titles",
                    [record.get("title") for record in cspec],
                ),
                _item("Provider", cspec_provider.get("provider")),
                _item(
                    "Scope explanation",
                    cspec_provenance.get("applicability_message"),
                ),
                _item(
                    "Query gene",
                    cspec_query_scope.get("gene_symbol"),
                ),
                _item(
                    "Query MONDO scope",
                    cspec_query_scope.get("mondo_ids"),
                ),
                _item(
                    "Primary provider failure",
                    cspec_provider.get("primary_failure"),
                ),
                _item(
                    "Original live retrieval",
                    cspec_metadata.get("source_retrieved_at"),
                ),
                _item(
                    "Cache stored",
                    cspec_metadata.get("cache_stored_at"),
                ),
                _item(
                    "Cache fallback used",
                    cspec_metadata.get("fallback_used_at"),
                ),
                _item(
                    "Freshness",
                    cspec_metadata.get("freshness_status"),
                ),
            ),
        },
        {
            "source": source_label(
                "Population evidence — gnomAD",
                "population_frequency",
            ),
            "status": _capability_status(evidence, "population_frequency"),
            "items": _items(
                _item("Provider", population.get("provider")),
                _item(
                    "Allele frequency",
                    population.get("population_frequency")
                    if population.get("population_frequency") is not None
                    else population.get("global_maf"),
                ),
                _item("Dataset", population.get("dataset")),
                _item("Fallback used", population.get("fallback_used")),
                _item("Primary failure", population.get("primary_failure")),
            ),
        },
        {
            "source": source_label("Literature enrichment", "literature"),
            "status": _capability_status(evidence, "literature"),
            "items": _items(
                _item(
                    "Article count",
                    len(literature.get("articles", []))
                    if isinstance(literature.get("articles"), list)
                    else 0,
                ),
                _item("Triggers", enrichment["triggers"]),
            ),
        },
    ]
    return sections


def _rsid(evidence: EvidenceObject) -> str | None:
    population = _mapping(evidence["annotations"].get("population"))
    for key in ("rsid", "dbsnp_id"):
        candidate = _text(population.get(key))
        if candidate:
            return candidate
    return None


def _provenance(
    evidence: EvidenceObject,
    interpretation: VariantInterpretationResult,
) -> ReportProvenance:
    providers: list[str] = []
    for provider in evidence["provenance"]["providers"]:
        name = _text(provider.get("provider")) or _text(
            provider.get("source")
        )
        status = _text(provider.get("status"))
        if name:
            providers.append(f"{name}: {status}" if status else name)
    providers.extend(
        f"Fallback: {notice['message']}"
        for notice in build_fallback_notices(
            evidence["capability_results"]
        )
    )
    return {
        "evidence_schema_version": evidence["schema_version"],
        "interpretation_schema_version": interpretation["schema_version"],
        "providers": providers[:MAX_REPORT_LIST_ITEMS],
        "upstream_sources": evidence["provenance"]["upstream_sources"][
            :MAX_REPORT_LIST_ITEMS
        ],
        "interpretation_model": interpretation["configured_model"],
        "interpretation_prompt_version": interpretation["prompt_version"],
    }


def _content(
    evidence: EvidenceObject,
    interpretation: VariantInterpretationResult,
) -> VariantReportContent:
    context = evidence["variant_context"]
    variant = evidence["variant"]
    audit = evidence["conflict_audit"]["pre_review"]
    reference_model = build_reference_model_v2(evidence)
    gene = context["gene"] or evidence["gene"]
    coordinate = (
        f"{evidence['assembly']} {variant['chrom']}:{variant['pos']} "
        f"{variant['ref']}>{variant['alt']}"
    )
    display_label = f"{gene} — {coordinate}" if gene else coordinate
    finding_messages = [
        cast(str, finding["message"])
        for finding in audit["findings"][:MAX_REPORT_LIST_ITEMS]
        if _text(finding.get("message"))
    ]
    limitations = list(evidence["warnings"])
    limitations.extend(evidence["conditional_enrichment"]["warnings"])
    if interpretation["status"] == "failed":
        limitations.append(
            "Variant interpretation is unavailable because the model task "
            "failed; collected evidence remains reviewable."
        )
    limitations.append(
        "Decision-support report only; it is not a diagnosis or treatment "
        "recommendation and requires qualified human review."
    )
    content: VariantReportContent = {
        "variant_summary": {
            "display_label": display_label,
            "assembly": evidence["assembly"],
            "chrom": variant["chrom"],
            "pos": variant["pos"],
            "ref": variant["ref"],
            "alt": variant["alt"],
            "gene": gene,
            "gene_id": context["gene_id"],
            "transcript": context["transcript"],
            "hgvs_c": context["hgvs_c"],
            "hgvs_p": context["hgvs_p"] or evidence["protein_change"],
            "consequence": context["consequence"],
            "rsid": _rsid(evidence),
        },
        "phenotype_context": {
            "accepted_hpo_terms": list(evidence["hpo_terms"]),
            "matched_hpo_terms": list(evidence["matched_hpo_terms"]),
            "phenotype_score": evidence["phenotype_score"],
            "phenotype_status": evidence["phenotype_relationship"]["phenotype_status"],
            "phenotype_to_gene_summary": _phen2gene_summary(evidence),
            "disease_context": _disease_context(evidence),
        },
        "evidence_sections": _evidence_sections(evidence),
        "conflict_summary": {
            "detected": audit["status"] == "conflict",
            "status": audit["status"],
            "severity": audit["routing_severity"],
            "findings": finding_messages,
        },
        "variant_interpretation": {
            "status": interpretation["status"],
            "narrative": interpretation["interpretation"],
            "conflict_assessment": interpretation["conflict_assessment"],
            "warnings": list(interpretation["warnings"]),
            "model": interpretation["configured_model"],
            "prompt_version": interpretation["prompt_version"],
            "generated_at": interpretation["generated_at"],
            "failure_type": interpretation["error_type"],
        },
        "reviewer_summary": None,
        "reviewer_notes": [],
        "literature_references": reference_model["literature_references"][
            :MAX_REPORT_REFERENCES
        ],
        "data_sources": reference_model["data_sources"][:MAX_REPORT_REFERENCES],
        "provenance": _provenance(evidence, interpretation),
        "limitations": limitations[:MAX_REPORT_LIST_ITEMS],
    }
    return content


def build_draft_variant_report(
    evidence_object: object,
    interpretation_result: object,
    *,
    variant_index: int,
) -> DraftVariantReport:
    """Compose one immutable machine report and separate draft copy."""

    if (
        isinstance(variant_index, bool)
        or not isinstance(variant_index, int)
        or variant_index < 0
    ):
        raise DraftVariantReportError(
            "variant_index must be a non-negative integer."
        )
    try:
        evidence = validate_evidence_object(deepcopy(evidence_object))
        interpretation = validate_variant_interpretation_result(
            deepcopy(interpretation_result),
            evidence=evidence,
        )
    except (ValueError, TypeError) as exc:
        raise DraftVariantReportError(
            "Draft report inputs are invalid."
        ) from exc
    if interpretation["variant_index"] != variant_index:
        raise DraftVariantReportError(
            "Interpretation order does not match the report variant index."
        )
    original = _content(evidence, interpretation)
    digest = hashlib.sha256(
        json.dumps(
            original,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    report: DraftVariantReport = {
        "schema_version": DRAFT_VARIANT_REPORT_SCHEMA_VERSION,
        "report_id": f"dvr-{variant_index}-{digest}",
        "variant_index": variant_index,
        "machine_original_report": deepcopy(original),
        "reviewed_report": deepcopy(original),
        "edit_history": [],
        "include_in_final_report": True,
        "selection_history": [],
        "review_status": "draft",
        "created_at": interpretation["generated_at"],
        "updated_at": interpretation["generated_at"],
    }
    return validate_draft_variant_report(
        report,
        evidence=evidence,
        interpretation=interpretation,
    )


def build_draft_variant_reports(
    evidence_objects: list[object],
    interpretation_results: list[object],
) -> list[DraftVariantReport]:
    """Compose ordered reports with one-to-one evidence interpretation pairing."""

    if len(evidence_objects) != len(interpretation_results):
        raise DraftVariantReportError(
            "Evidence and interpretation counts must match."
        )
    return [
        build_draft_variant_report(
            evidence,
            interpretation_results[index],
            variant_index=index,
        )
        for index, evidence in enumerate(evidence_objects)
    ]


def save_draft_variant_report(
    report: object,
    *,
    reviewer_summary: object,
    interpretation_narrative: object,
    conflict_assessment: object,
    reviewer_notes: object,
    timestamp: str | None = None,
    reviewer_context: str | None = None,
) -> DraftVariantReport:
    """Save allowed report edits with append-only field-level history."""

    working = validate_draft_variant_report(deepcopy(report))
    desired: dict[str, str | list[str] | None] = {
        "/reviewer_summary": _normalize_optional_review_text(
            reviewer_summary,
            field="reviewer_summary",
        ),
        "/variant_interpretation/narrative": (
            _normalize_optional_review_text(
                interpretation_narrative,
                field="interpretation_narrative",
            )
        ),
        "/variant_interpretation/conflict_assessment": (
            _normalize_optional_review_text(
                conflict_assessment,
                field="conflict_assessment",
            )
        ),
        "/reviewer_notes": _normalize_reviewer_notes(reviewer_notes),
    }
    if (
        working["reviewed_report"]["variant_interpretation"]["status"]
        == "success"
        and desired["/variant_interpretation/narrative"] is None
    ):
        raise DraftVariantReportError(
            "A successful interpretation narrative cannot be empty."
        )
    normalized_context = _normalize_optional_review_text(
        reviewer_context,
        field="reviewer_context",
    )
    if (
        normalized_context is not None
        and len(normalized_context) > MAX_REVIEWER_CONTEXT_CHARS
    ):
        raise DraftVariantReportError(
            "reviewer_context exceeds the supported length."
        )
    changed_paths = [
        field_path
        for field_path in _EDITABLE_PATHS
        if _editable_value(
            working["reviewed_report"],
            field_path,
        )
        != desired[field_path]
    ]
    if not changed_paths:
        return working
    if (
        len(working["edit_history"]) + len(changed_paths)
        > MAX_REPORT_EDIT_HISTORY
    ):
        raise DraftVariantReportError(
            "Draft report edit history limit would be exceeded."
        )
    normalized_timestamp = _timestamp(timestamp)
    if _timestamp_value(normalized_timestamp) < _timestamp_value(
        working["updated_at"]
    ):
        raise DraftVariantReportError(
            "Draft report edit timestamp predates the latest report decision."
        )
    for field_path in changed_paths:
        old_value = _editable_value(
            working["reviewed_report"],
            field_path,
        )
        new_value = desired[field_path]
        working["edit_history"].append(
            {
                "sequence": len(working["edit_history"]) + 1,
                "field_path": field_path,
                "change_type": "modified",
                "old_value": deepcopy(old_value),
                "new_value": deepcopy(new_value),
                "timestamp": normalized_timestamp,
                "reviewer_context": normalized_context,
            }
        )
        _set_editable_value(
            working["reviewed_report"],
            field_path,
            new_value,
        )
    working["review_status"] = "reviewed"
    working["updated_at"] = normalized_timestamp
    return validate_draft_variant_report(working)


def set_draft_variant_report_inclusion(
    report: object,
    include_in_final_report: bool,
    *,
    timestamp: str | None = None,
    reviewer_context: str | None = None,
) -> DraftVariantReport:
    """Record a human reporting decision without ranking the variant."""

    working = validate_draft_variant_report(deepcopy(report))
    if not isinstance(include_in_final_report, bool):
        raise DraftVariantReportError(
            "include_in_final_report must be boolean."
        )
    if working["include_in_final_report"] == include_in_final_report:
        return working
    if len(working["selection_history"]) >= MAX_SELECTION_HISTORY:
        raise DraftVariantReportError(
            "Draft report selection history limit would be exceeded."
        )
    normalized_context = _normalize_optional_review_text(
        reviewer_context,
        field="reviewer_context",
    )
    if (
        normalized_context is not None
        and len(normalized_context) > MAX_REVIEWER_CONTEXT_CHARS
    ):
        raise DraftVariantReportError(
            "reviewer_context exceeds the supported length."
        )
    normalized_timestamp = _timestamp(timestamp)
    if _timestamp_value(normalized_timestamp) < _timestamp_value(
        working["updated_at"]
    ):
        raise DraftVariantReportError(
            "Draft report selection timestamp predates the latest report decision."
        )
    old_value = working["include_in_final_report"]
    working["selection_history"].append(
        {
            "sequence": len(working["selection_history"]) + 1,
            "old_value": old_value,
            "new_value": include_in_final_report,
            "timestamp": normalized_timestamp,
            "reviewer_context": normalized_context,
        }
    )
    working["include_in_final_report"] = include_in_final_report
    working["review_status"] = "reviewed"
    working["updated_at"] = normalized_timestamp
    return validate_draft_variant_report(working)


def select_included_draft_variant_reports(
    reports: list[object],
) -> list[DraftVariantReport]:
    """Return included reports in their original input order."""

    validated = [validate_draft_variant_report(report) for report in reports]
    indexes = [report["variant_index"] for report in validated]
    if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
        raise DraftVariantReportError(
            "Draft reports must retain unique original input order."
        )
    return [
        report
        for report in validated
        if report["include_in_final_report"]
    ]


def _require_fields(
    value: object,
    fields: frozenset[str],
    path: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DraftVariantReportError(f"{path} has invalid fields.")
    return value


def _require_text(value: object, path: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_REPORT_TEXT_CHARS
    ):
        raise DraftVariantReportError(f"{path} must be bounded text.")


def _require_text_list(value: object, path: str) -> None:
    if not isinstance(value, list) or len(value) > MAX_REPORT_LIST_ITEMS:
        raise DraftVariantReportError(f"{path} must be a bounded list.")
    for index, item in enumerate(value):
        _require_text(item, f"{path}[{index}]")


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if not isinstance(value, str) or not value.strip():
        raise DraftVariantReportError(
            "Report edit timestamp must be non-empty."
        )
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(
            normalized.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise DraftVariantReportError(
            "Report edit timestamp must use ISO 8601."
        ) from exc
    if parsed.tzinfo is None:
        raise DraftVariantReportError(
            "Report edit timestamp must include a timezone."
        )
    return parsed.astimezone(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_optional_review_text(
    value: object,
    *,
    field: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DraftVariantReportError(f"{field} must be text or null.")
    normalized = value.strip()
    if not normalized:
        return None
    _require_text(normalized, field)
    return normalized


def _normalize_reviewer_notes(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_REVIEWER_NOTES:
        raise DraftVariantReportError(
            "reviewer_notes must be a bounded list."
        )
    notes: list[str] = []
    for index, note in enumerate(value):
        normalized = _normalize_optional_review_text(
            note,
            field=f"reviewer_notes[{index}]",
        )
        if normalized is None:
            raise DraftVariantReportError(
                "reviewer_notes cannot contain empty entries."
            )
        notes.append(normalized)
    return notes


def _editable_value(
    content: VariantReportContent,
    field_path: str,
) -> str | list[str] | None:
    if field_path == "/reviewer_summary":
        return content["reviewer_summary"]
    if field_path == "/variant_interpretation/narrative":
        return content["variant_interpretation"]["narrative"]
    if field_path == "/variant_interpretation/conflict_assessment":
        return content["variant_interpretation"]["conflict_assessment"]
    if field_path == "/reviewer_notes":
        return list(content["reviewer_notes"])
    raise DraftVariantReportError(
        "Report edit history contains a non-editable field."
    )


def _set_editable_value(
    content: VariantReportContent,
    field_path: str,
    value: str | list[str] | None,
) -> None:
    if field_path == "/reviewer_summary":
        content["reviewer_summary"] = cast(str | None, deepcopy(value))
        return
    if field_path == "/variant_interpretation/narrative":
        content["variant_interpretation"]["narrative"] = cast(
            str | None,
            deepcopy(value),
        )
        return
    if field_path == "/variant_interpretation/conflict_assessment":
        content["variant_interpretation"]["conflict_assessment"] = cast(
            str | None,
            deepcopy(value),
        )
        return
    if field_path == "/reviewer_notes":
        content["reviewer_notes"] = cast(
            list[str],
            deepcopy(value),
        )
        return
    raise DraftVariantReportError(
        "Report edit history contains a non-editable field."
    )


def _validate_edit_value(
    field_path: str,
    value: object,
) -> str | list[str] | None:
    if field_path == "/reviewer_notes":
        return _normalize_reviewer_notes(value)
    return _normalize_optional_review_text(
        value,
        field=field_path,
    )


def _validate_content(value: object, path: str) -> VariantReportContent:
    content = _require_fields(value, _CONTENT_FIELDS, path)
    summary = _require_fields(
        content["variant_summary"],
        _VARIANT_FIELDS,
        f"{path}.variant_summary",
    )
    for field in ("display_label", "assembly", "chrom", "ref", "alt"):
        _require_text(summary[field], f"{path}.variant_summary.{field}")
    pos = summary["pos"]
    if isinstance(pos, bool) or not isinstance(pos, int) or pos <= 0:
        raise DraftVariantReportError(f"{path}.variant_summary.pos is invalid.")
    for field in (
        "gene",
        "gene_id",
        "transcript",
        "hgvs_c",
        "hgvs_p",
        "consequence",
        "rsid",
    ):
        _require_text(
            summary[field],
            f"{path}.variant_summary.{field}",
            optional=True,
        )

    phenotype = _require_fields(
        content["phenotype_context"],
        _PHENOTYPE_FIELDS,
        f"{path}.phenotype_context",
    )
    _require_text_list(
        phenotype["accepted_hpo_terms"],
        f"{path}.phenotype_context.accepted_hpo_terms",
    )
    _require_text_list(
        phenotype["matched_hpo_terms"],
        f"{path}.phenotype_context.matched_hpo_terms",
    )
    _require_text(
        phenotype["phenotype_status"],
        f"{path}.phenotype_context.phenotype_status",
    )
    _require_text_list(
        phenotype["phenotype_to_gene_summary"],
        f"{path}.phenotype_context.phenotype_to_gene_summary",
    )
    _require_text_list(
        phenotype["disease_context"],
        f"{path}.phenotype_context.disease_context",
    )
    score = phenotype["phenotype_score"]
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
    ):
        raise DraftVariantReportError(
            f"{path}.phenotype_context.phenotype_score is invalid."
        )

    sections = content["evidence_sections"]
    if (
        not isinstance(sections, list)
        or not sections
        or len(sections) > MAX_EVIDENCE_SECTIONS
    ):
        raise DraftVariantReportError(f"{path}.evidence_sections is invalid.")
    for section_index, section_value in enumerate(sections):
        section_path = f"{path}.evidence_sections[{section_index}]"
        section = _require_fields(section_value, _EVIDENCE_SECTION_FIELDS, section_path)
        _require_text(section["source"], f"{section_path}.source")
        _require_text(section["status"], f"{section_path}.status")
        items = section["items"]
        if not isinstance(items, list) or len(items) > MAX_EVIDENCE_ITEMS_PER_SECTION:
            raise DraftVariantReportError(f"{section_path}.items is invalid.")
        for item_index, item_value in enumerate(items):
            item_path = f"{section_path}.items[{item_index}]"
            item = _require_fields(item_value, _EVIDENCE_ITEM_FIELDS, item_path)
            _require_text(item["label"], f"{item_path}.label")
            _require_text(item["value"], f"{item_path}.value")

    conflict = _require_fields(
        content["conflict_summary"],
        _CONFLICT_FIELDS,
        f"{path}.conflict_summary",
    )
    if not isinstance(conflict["detected"], bool):
        raise DraftVariantReportError(f"{path}.conflict_summary.detected is invalid.")
    _require_text(conflict["status"], f"{path}.conflict_summary.status")
    _require_text(conflict["severity"], f"{path}.conflict_summary.severity")
    _require_text_list(conflict["findings"], f"{path}.conflict_summary.findings")

    interpretation = _require_fields(
        content["variant_interpretation"],
        _INTERPRETATION_FIELDS,
        f"{path}.variant_interpretation",
    )
    for field in ("status", "model", "prompt_version", "generated_at"):
        _require_text(interpretation[field], f"{path}.variant_interpretation.{field}")
    for field in ("narrative", "conflict_assessment", "failure_type"):
        _require_text(
            interpretation[field],
            f"{path}.variant_interpretation.{field}",
            optional=True,
        )
    _require_text_list(
        interpretation["warnings"],
        f"{path}.variant_interpretation.warnings",
    )
    if interpretation["status"] == "success" and interpretation["narrative"] is None:
        raise DraftVariantReportError(
            f"{path}.variant_interpretation requires narrative text."
        )
    if interpretation["status"] == "failed" and interpretation["failure_type"] is None:
        raise DraftVariantReportError(
            f"{path}.variant_interpretation requires failure provenance."
        )

    reviewer_summary = _normalize_optional_review_text(
        content["reviewer_summary"],
        field=f"{path}.reviewer_summary",
    )
    if reviewer_summary != content["reviewer_summary"]:
        raise DraftVariantReportError(
            f"{path}.reviewer_summary is not normalized."
        )
    reviewer_notes = _normalize_reviewer_notes(content["reviewer_notes"])
    if reviewer_notes != content["reviewer_notes"]:
        raise DraftVariantReportError(
            f"{path}.reviewer_notes is not normalized."
        )

    references = content["literature_references"]
    if not isinstance(references, list) or len(references) > MAX_REPORT_REFERENCES:
        raise DraftVariantReportError(
            f"{path}.literature_references is invalid."
        )
    for index, reference_value in enumerate(references):
        try:
            reference = validate_canonical_reference(reference_value)
        except CanonicalReferenceError as exc:
            raise DraftVariantReportError(
                f"{path}.literature_references[{index}] is not canonical."
            ) from exc
        if reference["reference_id"] != f"R{index + 1}":
            raise DraftVariantReportError(
                f"{path}.literature_references must preserve citation order."
            )
    citation_texts: list[object] = [
        interpretation["narrative"],
        interpretation["conflict_assessment"],
        *interpretation["warnings"],
        content["reviewer_summary"],
        *content["reviewer_notes"],
    ]
    citation_tokens = re.findall(
        r"\[(R[^\]]*)\]",
        "\n".join(item for item in citation_texts if isinstance(item, str)),
    )
    allowed_reference_ids = {
        reference["reference_id"]
        for reference in references
        if isinstance(reference, dict)
    }
    if any(
        re.fullmatch(r"R[1-9][0-9]*", item) is None
        for item in citation_tokens
    ) or any(
        item not in allowed_reference_ids
        for item in cited_reference_ids(*citation_texts)
    ):
        raise DraftVariantReportError(
            f"{path} contains a citation absent from canonical references."
        )

    data_sources = content["data_sources"]
    if not isinstance(data_sources, list) or len(data_sources) > MAX_REPORT_REFERENCES:
        raise DraftVariantReportError(f"{path}.data_sources is invalid.")
    try:
        for source in data_sources:
            validate_data_source_record(source)
    except ReferenceModelError as exc:
        raise DraftVariantReportError(
            f"{path}.data_sources contains invalid provenance."
        ) from exc
    try:
        validate_reference_model_v2(
            {
                "schema_version": REFERENCE_MODEL_SCHEMA_VERSION,
                "literature_references": references,
                "data_sources": data_sources,
            }
        )
    except ReferenceModelError as exc:
        raise DraftVariantReportError(
            f"{path} mixes literature and data-source references."
        ) from exc

    provenance = _require_fields(
        content["provenance"],
        _PROVENANCE_FIELDS,
        f"{path}.provenance",
    )
    for field in (
        "evidence_schema_version",
        "interpretation_schema_version",
        "interpretation_model",
        "interpretation_prompt_version",
    ):
        _require_text(provenance[field], f"{path}.provenance.{field}")
    _require_text_list(provenance["providers"], f"{path}.provenance.providers")
    _require_text_list(
        provenance["upstream_sources"],
        f"{path}.provenance.upstream_sources",
    )
    _require_text_list(content["limitations"], f"{path}.limitations")
    return cast(VariantReportContent, deepcopy(content))


def validate_draft_variant_report(
    value: object,
    *,
    evidence: object | None = None,
    interpretation: object | None = None,
) -> DraftVariantReport:
    """Validate persisted or UI-bound Draft Variant Report V2 state."""

    report = _require_fields(value, _REPORT_FIELDS, "draft_variant_report")
    if report["schema_version"] != DRAFT_VARIANT_REPORT_SCHEMA_VERSION:
        raise DraftVariantReportError("Draft report schema version is unsupported.")
    _require_text(report["report_id"], "draft_variant_report.report_id")
    index = report["variant_index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise DraftVariantReportError("Draft report variant_index is invalid.")
    original = _validate_content(
        report["machine_original_report"],
        "draft_variant_report.machine_original_report",
    )
    reviewed = _validate_content(
        report["reviewed_report"],
        "draft_variant_report.reviewed_report",
    )
    original_digest = hashlib.sha256(
        json.dumps(
            original,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    if report["report_id"] != f"dvr-{index}-{original_digest}":
        raise DraftVariantReportError(
            "Draft report machine original failed its integrity check."
        )
    history = report["edit_history"]
    if (
        not isinstance(history, list)
        or len(history) > MAX_REPORT_EDIT_HISTORY
    ):
        raise DraftVariantReportError(
            "Draft report edit_history must be a bounded list."
        )
    created_at = _timestamp(cast(str, report["created_at"]))
    if created_at != report["created_at"]:
        raise DraftVariantReportError(
            "Draft report created_at is not normalized."
        )
    replayed = deepcopy(original)
    previous_timestamp = _timestamp_value(created_at)
    for history_index, record_value in enumerate(history):
        path = f"draft_variant_report.edit_history[{history_index}]"
        record = _require_fields(
            record_value,
            _EDIT_RECORD_FIELDS,
            path,
        )
        if record["sequence"] != history_index + 1:
            raise DraftVariantReportError(
                "Draft report edit sequence is invalid."
            )
        field_path = record["field_path"]
        if field_path not in _EDITABLE_PATHS:
            raise DraftVariantReportError(
                "Draft report edit history contains a non-editable field."
            )
        if record["change_type"] != "modified":
            raise DraftVariantReportError(
                "Draft report edit change_type is invalid."
            )
        old_value = _validate_edit_value(
            cast(str, field_path),
            record["old_value"],
        )
        new_value = _validate_edit_value(
            cast(str, field_path),
            record["new_value"],
        )
        if old_value != record["old_value"] or new_value != record[
            "new_value"
        ]:
            raise DraftVariantReportError(
                "Draft report edit values are not normalized."
            )
        if old_value != _editable_value(
            replayed,
            cast(str, field_path),
        ):
            raise DraftVariantReportError(
                "Draft report edit history is not append-only."
            )
        if old_value == new_value:
            raise DraftVariantReportError(
                "Draft report edit history contains a no-op change."
            )
        timestamp = _timestamp(cast(str, record["timestamp"]))
        if timestamp != record["timestamp"]:
            raise DraftVariantReportError(
                "Draft report edit timestamp is not normalized."
            )
        timestamp_value = _timestamp_value(timestamp)
        if timestamp_value < previous_timestamp:
            raise DraftVariantReportError(
                "Draft report edit timestamps are out of order."
            )
        previous_timestamp = timestamp_value
        context = record["reviewer_context"]
        if context is not None:
            normalized_context = _normalize_optional_review_text(
                context,
                field=f"{path}.reviewer_context",
            )
            if (
                normalized_context != context
                or len(cast(str, context)) > MAX_REVIEWER_CONTEXT_CHARS
            ):
                raise DraftVariantReportError(
                    "Draft report reviewer context is invalid."
                )
        _set_editable_value(
            replayed,
            cast(str, field_path),
            new_value,
        )
    if replayed != reviewed:
        raise DraftVariantReportError(
            "Draft report reviewed content is not explained by edit history."
        )
    include_in_final_report = report["include_in_final_report"]
    if not isinstance(include_in_final_report, bool):
        raise DraftVariantReportError(
            "Draft report include_in_final_report must be boolean."
        )
    selection_history = report["selection_history"]
    if (
        not isinstance(selection_history, list)
        or len(selection_history) > MAX_SELECTION_HISTORY
    ):
        raise DraftVariantReportError(
            "Draft report selection_history must be a bounded list."
        )
    replayed_selection = True
    selection_timestamp = _timestamp_value(created_at)
    for selection_index, record_value in enumerate(selection_history):
        path = f"draft_variant_report.selection_history[{selection_index}]"
        record = _require_fields(
            record_value,
            _SELECTION_RECORD_FIELDS,
            path,
        )
        if record["sequence"] != selection_index + 1:
            raise DraftVariantReportError(
                "Draft report selection sequence is invalid."
            )
        old_value = record["old_value"]
        new_value = record["new_value"]
        if not isinstance(old_value, bool) or not isinstance(new_value, bool):
            raise DraftVariantReportError(
                "Draft report selection values must be boolean."
            )
        if old_value != replayed_selection or old_value == new_value:
            raise DraftVariantReportError(
                "Draft report selection history is not append-only."
            )
        timestamp = _timestamp(cast(str, record["timestamp"]))
        if timestamp != record["timestamp"]:
            raise DraftVariantReportError(
                "Draft report selection timestamp is not normalized."
            )
        timestamp_value = _timestamp_value(timestamp)
        if timestamp_value < selection_timestamp:
            raise DraftVariantReportError(
                "Draft report selection timestamps are out of order."
            )
        selection_timestamp = timestamp_value
        context = record["reviewer_context"]
        if context is not None:
            normalized_context = _normalize_optional_review_text(
                context,
                field=f"{path}.reviewer_context",
            )
            if (
                normalized_context != context
                or len(cast(str, context)) > MAX_REVIEWER_CONTEXT_CHARS
            ):
                raise DraftVariantReportError(
                    "Draft report selection reviewer context is invalid."
                )
        replayed_selection = new_value
    if replayed_selection != include_in_final_report:
        raise DraftVariantReportError(
            "Draft report inclusion is not explained by selection history."
        )
    expected_status = "reviewed" if history or selection_history else "draft"
    if report["review_status"] != expected_status:
        raise DraftVariantReportError(
            "Draft report review_status does not match its edit history."
        )
    updated_at = _timestamp(cast(str, report["updated_at"]))
    latest_timestamp = max(previous_timestamp, selection_timestamp)
    expected_updated_at = latest_timestamp.isoformat().replace("+00:00", "Z")
    if (
        updated_at != report["updated_at"]
        or updated_at != expected_updated_at
    ):
        raise DraftVariantReportError(
            "Draft report updated_at does not match its review history."
        )
    if evidence is not None:
        try:
            validated_evidence = validate_evidence_object(deepcopy(evidence))
        except (ValueError, TypeError) as exc:
            raise DraftVariantReportError(
                "Draft report evidence is invalid."
            ) from exc
        variant = validated_evidence["variant"]
        summary = original["variant_summary"]
        if (
            summary["assembly"] != validated_evidence["assembly"]
            or summary["chrom"] != variant["chrom"]
            or summary["pos"] != variant["pos"]
            or summary["ref"] != variant["ref"]
            or summary["alt"] != variant["alt"]
        ):
            raise DraftVariantReportError(
                "Draft report identity does not match its Evidence Object."
            )
        if interpretation is not None:
            try:
                validated_interpretation = (
                    validate_variant_interpretation_result(
                        deepcopy(interpretation),
                        evidence=validated_evidence,
                    )
                )
            except (ValueError, TypeError) as exc:
                raise DraftVariantReportError(
                    "Draft report interpretation is invalid."
                ) from exc
            if validated_interpretation["variant_index"] != index:
                raise DraftVariantReportError(
                    "Draft report interpretation order is invalid."
                )
            if original != _content(
                validated_evidence,
                validated_interpretation,
            ):
                raise DraftVariantReportError(
                    "Draft report content does not match its machine inputs."
                )
    try:
        validate_human_review_content(
            report["reviewed_report"],
            reviewed["reviewer_notes"],
            history,
        )
        validate_no_prohibited_fields(
            report,
            context="Draft Variant Report V2",
        )
        serialized = json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ClinicalDataPrivacyError, TypeError, ValueError) as exc:
        raise DraftVariantReportError(
            "Draft Variant Report contains unsafe data."
        ) from exc
    if len(serialized) > MAX_DRAFT_VARIANT_REPORT_BYTES:
        raise DraftVariantReportError("Draft Variant Report is too large.")
    return cast(DraftVariantReport, deepcopy(report))


__all__ = [
    "DRAFT_VARIANT_REPORT_SCHEMA_VERSION",
    "DraftVariantReport",
    "DraftVariantReportError",
    "ReportEditRecord",
    "SelectionRecord",
    "VariantReportContent",
    "build_draft_variant_report",
    "build_draft_variant_reports",
    "save_draft_variant_report",
    "select_included_draft_variant_reports",
    "set_draft_variant_report_inclusion",
    "validate_draft_variant_report",
]
