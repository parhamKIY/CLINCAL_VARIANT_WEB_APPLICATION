"""Draft Variant Report V2 contracts and deterministic composition."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import TypedDict, cast

from backend.privacy import (
    ClinicalDataPrivacyError,
    validate_no_prohibited_fields,
)
from backend.report import EvidenceObject, validate_evidence_object
from backend.variant_interpretation import (
    VariantInterpretationResult,
    validate_variant_interpretation_result,
)


DRAFT_VARIANT_REPORT_SCHEMA_VERSION = "2.0"
MAX_DRAFT_VARIANT_REPORT_BYTES = 128 * 1024
MAX_REPORT_TEXT_CHARS = 20_000
MAX_EVIDENCE_VALUE_CHARS = 4_000
MAX_REPORT_LIST_ITEMS = 50
MAX_EVIDENCE_SECTIONS = 16
MAX_EVIDENCE_ITEMS_PER_SECTION = 20
MAX_REPORT_REFERENCES = 50


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


class ReportReference(TypedDict):
    """Trusted source reference awaiting Stage 55 canonicalization."""

    source: str
    identifier: str | None
    url: str | None


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
    references: list[ReportReference]
    provenance: ReportProvenance
    limitations: list[str]


class DraftVariantReport(TypedDict):
    """Immutable machine original plus separate future-editable draft layer."""

    schema_version: str
    report_id: str
    variant_index: int
    machine_original_report: VariantReportContent
    reviewed_report: VariantReportContent
    edit_history: list[dict[str, object]]
    review_status: str
    created_at: str


_REPORT_FIELDS = frozenset(DraftVariantReport.__required_keys__)
_CONTENT_FIELDS = frozenset(VariantReportContent.__required_keys__)
_VARIANT_FIELDS = frozenset(VariantSummary.__required_keys__)
_PHENOTYPE_FIELDS = frozenset(PhenotypeContext.__required_keys__)
_EVIDENCE_SECTION_FIELDS = frozenset(EvidenceSection.__required_keys__)
_EVIDENCE_ITEM_FIELDS = frozenset(EvidenceItem.__required_keys__)
_CONFLICT_FIELDS = frozenset(ConflictSummary.__required_keys__)
_INTERPRETATION_FIELDS = frozenset(InterpretationSection.__required_keys__)
_REFERENCE_FIELDS = frozenset(ReportReference.__required_keys__)
_PROVENANCE_FIELDS = frozenset(ReportProvenance.__required_keys__)


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
    return [
        f"{item['label']}: {item['value']}"
        for item in _items(
            _item("Phen2Gene status", phen2gene.get("status")),
            _item("Phen2Gene rank", phen2gene.get("rank")),
            _item("Phen2Gene score", phen2gene.get("score")),
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
    clingen = pathogenicity["clingen_context"]
    sections: list[EvidenceSection] = [
        {
            "source": "Ensembl VEP",
            "status": _provider_status(evidence, "vep"),
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
            "source": "MyVariant.info",
            "status": _provider_status(evidence, "myvariant"),
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
            "source": "NCBI ClinVar",
            "status": _provider_status(evidence, "clinvar"),
            "items": _items(
                _item("Accession", evidence["clinvar_accession"]),
                _item("Significance", pathogenicity["clinvar_classification"]),
                _item("Review status", pathogenicity["clinvar_review_status"]),
                _item("Conditions", pathogenicity["clinvar_conditions"]),
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
            "source": "ClinGen CSpec",
            "status": "success" if cspec else "not_found",
            "items": _items(
                _item("Matching specifications", len(cspec)),
                _item(
                    "Specification titles",
                    [record.get("title") for record in cspec],
                ),
            ),
        },
        {
            "source": "gnomAD / population enrichment",
            "status": _status(population.get("status"), "not_triggered"),
            "items": _items(
                _item("Provider", population.get("provider")),
                _item("Allele frequency", population.get("allele_frequency")),
                _item("Dataset", population.get("dataset")),
            ),
        },
        {
            "source": "Literature enrichment",
            "status": _status(literature.get("status"), "not_triggered"),
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


def _references(evidence: EvidenceObject) -> list[ReportReference]:
    references: list[ReportReference] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    def add(source: object, identifier: object, url: object) -> None:
        normalized_source = _text(source)
        normalized_identifier = _text(identifier)
        normalized_url = _text(url)
        if normalized_source is None:
            return
        key = (normalized_source, normalized_identifier, normalized_url)
        if key in seen or len(references) >= MAX_REPORT_REFERENCES:
            return
        seen.add(key)
        references.append(
            {
                "source": normalized_source,
                "identifier": normalized_identifier,
                "url": normalized_url,
            }
        )

    for reference in evidence["references"]:
        add(reference["source"], None, reference["url"])
    for curation in evidence["clingen_curations"]:
        add("ClinGen / GenCC", curation.get("disease_id"), curation.get("report_url"))
        for pmid in curation.get("pmids", []):
            add("PubMed", f"PMID:{pmid}", None)
    literature = _mapping(
        evidence["conditional_enrichment"].get("literature")
    )
    articles = literature.get("articles")
    if isinstance(articles, list):
        for article in articles:
            record = _mapping(article)
            source = _text(record.get("provider")) or _text(
                record.get("source")
            ) or "Literature"
            identifier = (
                _text(record.get("pmid"))
                or _text(record.get("pmcid"))
                or _text(record.get("doi"))
                or _text(record.get("id"))
            )
            add(source, identifier, None)
    return references


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
        "references": _references(evidence),
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
        "review_status": "draft",
        "created_at": interpretation["generated_at"],
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

    references = content["references"]
    if not isinstance(references, list) or len(references) > MAX_REPORT_REFERENCES:
        raise DraftVariantReportError(f"{path}.references is invalid.")
    for index, reference_value in enumerate(references):
        ref_path = f"{path}.references[{index}]"
        reference = _require_fields(reference_value, _REFERENCE_FIELDS, ref_path)
        _require_text(reference["source"], f"{ref_path}.source")
        _require_text(reference["identifier"], f"{ref_path}.identifier", optional=True)
        _require_text(reference["url"], f"{ref_path}.url", optional=True)
        if reference["url"] is not None and not cast(
            str,
            reference["url"],
        ).startswith("https://"):
            raise DraftVariantReportError(
                f"{ref_path}.url must be a trusted HTTPS reference."
            )

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
    if not isinstance(history, list) or history:
        raise DraftVariantReportError(
            "Stage 52 draft reports require an empty edit history."
        )
    if report["review_status"] != "draft":
        raise DraftVariantReportError(
            "Stage 52 draft report review_status must be draft."
        )
    if reviewed != original:
        raise DraftVariantReportError(
            "Stage 52 reviewed report must match its immutable original."
        )
    _require_text(report["created_at"], "draft_variant_report.created_at")
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
    "VariantReportContent",
    "build_draft_variant_report",
    "build_draft_variant_reports",
    "validate_draft_variant_report",
]
