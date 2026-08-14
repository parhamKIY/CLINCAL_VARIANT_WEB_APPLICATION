"""High-fidelity, privacy-safe HTML preview for Draft Variant Report V2."""

from __future__ import annotations

from html import escape
from typing import Iterable

from backend.conflict_auditor import normalize_classification_label
from backend.variant_report import (
    DraftVariantReport,
    validate_draft_variant_report,
)
from frontend.source_status import (
    build_reviewer_section_status,
    build_reviewer_source_status,
)


_PAGE_STYLE = """
<style>
.cv-report-page {
  box-sizing: border-box;
  width: min(100%, 816px);
  min-height: 980px;
  margin: 0 auto;
  padding: 72px 72px 64px;
  background: #fff;
  color: #111827;
  border: 1px solid #d9dee7;
  box-shadow: 0 8px 24px rgba(15, 23, 42, .10);
  font-family: "Times New Roman", Times, serif;
  font-size: 15px;
  line-height: 1.48;
}
.cv-report-page * { box-sizing: border-box; }
.cv-report-page h1 {
  margin: 0 0 22px;
  text-align: center;
  font-size: 25px;
  font-style: italic;
}
.cv-report-page h2 {
  margin: 24px 0 10px;
  color: #17365d;
  font-size: 19px;
  border-bottom: 1px solid #9eabc0;
  padding-bottom: 4px;
}
.cv-report-page h3 { margin: 18px 0 7px; font-size: 16px; }
.cv-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px 24px;
  margin-bottom: 20px;
  padding: 9px 12px;
  background: #f3f5f8;
  border-top: 1px solid #9eabc0;
  border-bottom: 1px solid #9eabc0;
  font-size: 12px;
}
.cv-result {
  margin: 24px auto 18px;
  padding: 13px 16px;
  border: 3px solid #8b1a1a;
  text-align: center;
}
.cv-result-allele { color: #17365d; font-size: 20px; font-weight: 700; }
.cv-result-class { color: #a56a00; font-size: 18px; font-weight: 700; }
.cv-quality { margin: 10px 0; padding: 9px 11px; border-left: 5px solid #17365d; background: #eef3f8; }
.cv-quality-action { border-left-color: #8b1a1a; background: #fff1f2; font-weight: 700; }
.cv-quality-partial { border-left-color: #a56a00; background: #fff8e6; font-weight: 700; }
.cv-notice { color: #4b5563; font-size: 12px; }
.cv-report-page table { width: 100%; border-collapse: collapse; margin: 10px 0 14px; }
.cv-report-page th, .cv-report-page td {
  border: 1px solid #7f8998;
  padding: 7px 8px;
  overflow-wrap: anywhere;
  vertical-align: top;
}
.cv-report-page th { background: #fff; color: #17365d; text-align: left; }
.cv-report-page td { background: #e2e4e8; }
.cv-report-page .cv-source { width: 25%; font-weight: 700; }
.cv-prose { white-space: pre-line; }
.cv-list { margin: 7px 0 12px; padding-left: 22px; }
.cv-ref { margin: 0 0 7px; overflow-wrap: anywhere; }
.cv-ref a { color: #17365d; }
@media (max-width: 700px) {
  .cv-report-page { min-height: 0; padding: 30px 22px; }
  .cv-meta { grid-template-columns: 1fr; }
}
</style>
"""


def _text(value: object, fallback: str = "Not available") -> str:
    if value is None or value == "":
        return fallback
    return escape(str(value), quote=True)


def _items(values: Iterable[str]) -> str:
    retained = [value for value in values if value]
    if not retained:
        return "<li>Not available</li>"
    return "".join(f"<li>{_text(value)}</li>" for value in retained)


def _status_html(category: str, message: str) -> str:
    return f"<b>{_text(category)}</b><br>{_text(message)}"


def _provider_names(values: Iterable[str]) -> str:
    names: list[str] = []
    for value in values:
        if value.startswith("Fallback: "):
            names.append(value.removeprefix("Fallback: "))
        else:
            names.append(value.split(": ", 1)[0])
    return ", ".join(names) or "None recorded"


def stable_allele_identity(value: object) -> str:
    """Return the assembly-qualified identity used for report navigation."""

    report = validate_draft_variant_report(value)
    variant = report["reviewed_report"]["variant_summary"]
    return (
        f"{variant['assembly']} chr{variant['chrom']}:"
        f"{variant['pos']} {variant['ref']}>{variant['alt']}"
    )


def _result_classification(report: DraftVariantReport) -> tuple[str, str]:
    def sentence_case(value: str) -> str:
        cleaned = value.replace("_", " ").strip()
        if normalize_classification_label(cleaned) == "VUS":
            return "Uncertain significance (VUS)"
        return cleaned[:1].upper() + cleaned[1:]

    clinvar: tuple[str, str] | None = None
    genebe: tuple[str, str] | None = None
    myvariant_derived: tuple[str, str] | None = None
    audit_state: str | None = None
    for section in report["reviewed_report"]["evidence_sections"]:
        values = {item["label"]: item["value"] for item in section["items"]}
        if section["source"] == "Classification evidence audit":
            audit_state = values.get("State")
            if values.get("Direct ClinVar exact classification"):
                clinvar = (
                    values["Direct ClinVar exact classification"],
                    "NCBI ClinVar",
                )
            if values.get("GeneBe automated classification"):
                genebe = (values["GeneBe automated classification"], "GeneBe")
            if values.get("MyVariant ClinVar-derived classification"):
                myvariant_derived = (
                    values["MyVariant ClinVar-derived classification"],
                    "MyVariant.info",
                )
            continue
        if "ClinVar" in section["source"] and values.get("Significance"):
            clinvar = (values["Significance"], section["source"])
        if "GeneBe" in section["source"] and values.get(
            "Automated ACMG classification"
        ):
            genebe = (values["Automated ACMG classification"], section["source"])

    retained = [item for item in (clinvar, genebe, myvariant_derived) if item]
    normalized = {
        normalized
        for value, _source in retained
        if (normalized := normalize_classification_label(value)) is not None
    }
    if audit_state == "CONFLICTING_CLASSIFICATIONS" or len(normalized) > 1:
        labels: list[str] = []
        if clinvar:
            labels.append(f"ClinVar: {sentence_case(clinvar[0])}")
        if genebe:
            labels.append(f"GeneBe automated: {sentence_case(genebe[0])}")
        if myvariant_derived:
            labels.append(
                "MyVariant ClinVar-derived: "
                f"{sentence_case(myvariant_derived[0])}"
            )
        return "Classification conflict", "; ".join(labels)
    if clinvar:
        return (
            f"ClinVar exact classification: {sentence_case(clinvar[0])}",
            clinvar[1],
        )
    if genebe:
        return (
            "GeneBe automated classification: "
            f"{sentence_case(genebe[0])}",
            "Direct ClinVar exact classification: not available",
        )
    if myvariant_derived:
        return (
            "MyVariant ClinVar-derived classification: "
            f"{sentence_case(myvariant_derived[0])}",
            "Non-independent rescue evidence; direct ClinVar exact classification: not available",
        )
    if audit_state == "NO_CLASSIFICATION_AFTER_RESCUE":
        return (
            "Classification not available",
            "Configured classification retrieval and rescue routes exhausted",
        )
    return "Classification not assessed", "Classification retrieval is incomplete"


def _preliminary_classification(
    report: DraftVariantReport,
) -> tuple[str, str | None, list[str]]:
    interpretation = report["reviewed_report"]["variant_interpretation"]
    if interpretation["status"] != "success":
        return "Not available", None, []
    status = interpretation.get("preliminary_classification_status")
    if status == "classified":
        return (
            str(interpretation["preliminary_classification"]),
            str(interpretation["classification_rationale"]),
            list(interpretation.get("limitations", [])),
        )
    if status == "ambiguous":
        return (
            "Ambiguous — user review required",
            str(interpretation["classification_rationale"]),
            list(interpretation.get("limitations", [])),
        )
    return "Not available for this pre-Stage-124 analysis", None, []


def _brief_interpretation(report: DraftVariantReport) -> str:
    content = report["reviewed_report"]
    if content["reviewer_summary"]:
        return content["reviewer_summary"]
    narrative = content["variant_interpretation"]["narrative"]
    if not narrative:
        return "Interpretation is not available and requires human review."
    first_paragraph = narrative.strip().split("\n\n", 1)[0]
    return first_paragraph[:1200]


def _call_quality_text(report: DraftVariantReport) -> tuple[str, str]:
    quality = report["reviewed_report"]["call_quality"]
    qual = "Not available" if quality["qual"] is None else f"{quality['qual']:g}"
    raw_filter = quality["filter"] or "Not evaluated"
    state = quality["status"].replace("_", " ")
    override = quality["override"]
    if quality["status"] == "not_evaluated" and quality["acknowledged_at"] is None:
        return (
            f"QUAL: {qual}; FILTER: {raw_filter}; State: {state}. ACTION REQUIRED: reviewer acknowledgement is missing.",
            "cv-quality-action",
        )
    if quality["status"] == "failed" and override is None:
        return (
            f"QUAL: {qual}; FILTER: {raw_filter}; State: {state}. ACTION REQUIRED: documented reviewer override is missing.",
            "cv-quality-action",
        )
    if quality["status"] == "failed" and override is not None:
        return (
            f"QUAL: {qual}; FILTER: {raw_filter}; State: {state}. PARTIAL: overridden by reviewer — {override['reason']} ({override['timestamp']}).",
            "cv-quality-partial",
        )
    if quality["status"] == "not_evaluated":
        return (
            f"QUAL: {qual}; FILTER: {raw_filter}; State: {state}; reviewer acknowledgement: {quality['acknowledged_at']}.",
            "cv-quality",
        )
    return f"QUAL: {qual}; FILTER: {raw_filter}; State: {state}.", "cv-quality"


def _page(content: str, *, page: int) -> str:
    return (
        _PAGE_STYLE
        + f'<article class="cv-report-page" aria-label="Clinical report page {page}">'
        + content
        + "</article>"
    )


def render_draft_report_preview_pages(value: object) -> tuple[str, str, str]:
    """Render three deterministic document pages without executable content."""

    report = validate_draft_variant_report(value)
    content = report["reviewed_report"]
    variant = content["variant_summary"]
    phenotype = content["phenotype_context"]
    interpretation = content["variant_interpretation"]
    source_classification, source_classification_detail = (
        _result_classification(report)
    )
    preliminary_classification, classification_rationale, classification_limitations = (
        _preliminary_classification(report)
    )
    allele = stable_allele_identity(report)
    hgvs = " / ".join(
        item for item in (variant["hgvs_c"], variant["hgvs_p"]) if item
    ) or allele
    gene_hgvs = f"{variant['gene'] or 'Gene not available'}: {hgvs}"
    accepted_hpo = ", ".join(phenotype["accepted_hpo_terms"]) or "None recorded"
    matched_hpo = ", ".join(phenotype["matched_hpo_terms"]) or "None recorded"
    phenotype_status = build_reviewer_source_status(
        {
            "source": "Phenotype evidence",
            "capability": "phenotype_gene",
            "status": phenotype["phenotype_status"],
            "operational_status": phenotype["phenotype_status"],
            "provider_role": "primary",
            "fallback_used": False,
        }
    )
    quality_text, quality_class = _call_quality_text(report)

    page_one = _page(
        f"""
<h1>NGS Result Report</h1>
<div class="cv-meta">
  <span><b>Report ID:</b> {_text(report['report_id'])}</span>
  <span><b>Variant:</b> {report['variant_index'] + 1}</span>
  <span><b>Genome build:</b> {_text(variant['assembly'])}</span>
  <span><b>Review state:</b> {_text(report['review_status'].capitalize())}</span>
</div>
<h2>Clinical Features</h2>
<p><b>Accepted HPO terms:</b> {_text(accepted_hpo)}</p>
<p><b>Matched HPO terms:</b> {_text(matched_hpo)}</p>
<p><b>Phenotype evidence:</b> {_text(phenotype_status['message'])}</p>
<h2>Method</h2>
<p>Allele-level evidence synthesis for an already filtered variant. The application did not perform sequencing or genome-wide prioritization.</p>
<div class="{quality_class}"><b>Call quality:</b> {_text(quality_text)}</div>
<h2>Variant and source classification context</h2>
<div class="cv-result">
  <div class="cv-result-allele">{_text(gene_hgvs)}</div>
  <div>{_text(allele)}</div>
  <div class="cv-result-class">Preliminary evidence-based classification: {_text(preliminary_classification)}</div>
  {f'<div><b>Classification rationale:</b> {_text(classification_rationale)}</div>' if classification_rationale else ''}
  {f'<div><b>Classification limitations:</b> {_text("; ".join(classification_limitations))}</div>' if classification_limitations else ''}
  <div class="cv-result-class">System classification: Not independently determined</div>
  <div>Source classification context: {_text(source_classification)}</div>
  <div>{_text(source_classification_detail)}</div>
  <div class="{quality_class}"><b>Call quality:</b> {_text(quality_text)}</div>
</div>
<h2>Brief Interpretation(s)</h2>
<p class="cv-prose">{_text(_brief_interpretation(report))}</p>
<p class="cv-notice">Decision-support output for educational and research use; qualified human review is required.</p>
""",
        page=1,
    )

    evidence_rows_list: list[str] = []
    for section in content["evidence_sections"]:
        presented = build_reviewer_section_status(
            source=section["source"],
            status=section["status"],
            data_sources=content["data_sources"],
        )
        evidence_rows_list.append(
            "<tr>"
            f'<td class="cv-source">{_text(section["source"])}</td>'
            f"<td>{_status_html(presented['category'], presented['message'])}</td>"
            "<td><ul class=\"cv-list\">"
            + _items(
                f"{item['label']}: {item['value']}" for item in section["items"]
            )
            + "</ul></td></tr>"
        )
    evidence_rows = "".join(evidence_rows_list)
    narrative = (
        interpretation["narrative"]
        or "Interpretation is not available and requires human review."
    )
    page_two = _page(
        f"""
<h1>Main Finding(s) in Detail</h1>
<table aria-label="Source-attributed main findings">
  <thead><tr><th>Evidence source</th><th>Status</th><th>Exact-allele finding</th></tr></thead>
  <tbody>{evidence_rows}</tbody>
</table>
<p class="cv-notice">Missingness is explicit. Computational evidence and provider classifications remain source-attributed and are not independent application classifications.</p>
<h2>Variant interpretation</h2>
<p class="cv-prose">{_text(narrative)}</p>
""",
        page=2,
    )

    conflict = content["conflict_summary"]
    reference_rows = []
    for reference in content["literature_references"]:
        label = " — ".join(
            str(item)
            for item in (
                reference["reference_id"],
                reference["source"],
                reference["identifier"],
                reference["title"],
            )
            if item
        )
        if reference["url_status"] == "validated" and reference["canonical_url"]:
            reference_rows.append(
                f'<p class="cv-ref"><a href="{escape(reference["canonical_url"], quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{_text(label)}</a></p>'
            )
        else:
            reference_rows.append(f'<p class="cv-ref">{_text(label)}</p>')
    source_rows: list[str] = []
    for source in content["data_sources"]:
        presented = build_reviewer_source_status(source)
        source_label = source["source"]
        if source_label == "MyVariant.info":
            source_label += " (Programmatic annotation source)"
        if source["human_url"] and source["link_status"] == "validated":
            rendered_source = (
                f'<a href="{escape(source["human_url"], quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">'
                f'{_text(source_label)}</a>'
            )
        else:
            rendered_source = _text(source_label)
        source_rows.append(
            f"<tr><td>{rendered_source}</td>"
            f"<td>{_status_html(presented['category'], presented['message'])}</td></tr>"
        )
    sources = "".join(source_rows)
    page_three = _page(
        f"""
<h1>Variant(s) classification</h1>
<p><b>Preliminary evidence-based classification:</b> {_text(preliminary_classification)}</p>
{f'<p><b>Classification rationale:</b> {_text(classification_rationale)}</p>' if classification_rationale else ''}
{f'<p><b>Classification limitations:</b> {_text("; ".join(classification_limitations))}</p>' if classification_limitations else ''}
<p><b>System classification:</b> Not independently determined</p>
<p><b>Source-attributed result:</b> {_text(source_classification)} ({_text(source_classification_detail)})</p>
<p><b>Conflict status:</b> {_text(conflict['status'].replace('_', ' '))}; severity: {_text(conflict['severity'])}.</p>
<ul class="cv-list">{_items(conflict['findings'])}</ul>
<p>The application has not independently adjudicated an ACMG/AMP classification.</p>
<h2>Comments and scope</h2>
<ul class="cv-list">{_items([*interpretation['warnings'], *content['limitations'], *content['reviewer_notes']])}</ul>
<h2>Literature References</h2>
{''.join(reference_rows) or '<p>No trusted literature reference is available.</p>'}
<h2>Data Sources</h2>
<table aria-label="Data sources"><thead><tr><th>Source</th><th>Status</th></tr></thead><tbody>{sources}</tbody></table>
<p class="cv-notice">Providers: {_text(_provider_names(content['provenance']['providers']))}</p>
""",
        page=3,
    )
    return page_one, page_two, page_three


__all__ = [
    "render_draft_report_preview_pages",
    "stable_allele_identity",
]
