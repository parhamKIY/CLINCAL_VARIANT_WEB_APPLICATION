"""High-fidelity, privacy-safe HTML preview for Draft Variant Report V2."""

from __future__ import annotations

from html import escape
from typing import Iterable

from backend.variant_report import (
    DraftVariantReport,
    validate_draft_variant_report,
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


def stable_allele_identity(value: object) -> str:
    """Return the assembly-qualified identity used for report navigation."""

    report = validate_draft_variant_report(value)
    variant = report["reviewed_report"]["variant_summary"]
    return (
        f"{variant['assembly']} chr{variant['chrom']}:"
        f"{variant['pos']} {variant['ref']}>{variant['alt']}"
    )


def _result_classification(report: DraftVariantReport) -> tuple[str, str]:
    for section in report["reviewed_report"]["evidence_sections"]:
        if "ClinVar" not in section["source"]:
            continue
        values = {item["label"]: item["value"] for item in section["items"]}
        classification = values.get("Significance")
        if classification:
            return classification, section["source"]
    return "Classification not available", "No exact classification source"


def _brief_interpretation(report: DraftVariantReport) -> str:
    content = report["reviewed_report"]
    if content["reviewer_summary"]:
        return content["reviewer_summary"]
    narrative = content["variant_interpretation"]["narrative"]
    if not narrative:
        return "Interpretation is not available and requires human review."
    first_paragraph = narrative.strip().split("\n\n", 1)[0]
    return first_paragraph[:1200]


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
    classification, classification_source = _result_classification(report)
    allele = stable_allele_identity(report)
    hgvs = " / ".join(
        item for item in (variant["hgvs_c"], variant["hgvs_p"]) if item
    ) or allele
    gene_hgvs = f"{variant['gene'] or 'Gene not available'}: {hgvs}"
    accepted_hpo = ", ".join(phenotype["accepted_hpo_terms"]) or "None recorded"
    matched_hpo = ", ".join(phenotype["matched_hpo_terms"]) or "None recorded"

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
<p><b>Phenotype evidence:</b> {_text(phenotype['phenotype_status'].replace('_', ' '))}</p>
<h2>Method</h2>
<p>Allele-level evidence synthesis for an already filtered variant. The application did not perform sequencing or genome-wide prioritization.</p>
<div class="cv-result">
  <div class="cv-result-allele">{_text(gene_hgvs)}</div>
  <div>{_text(allele)}</div>
  <div class="cv-result-class">{_text(classification)}</div>
  <div>Source: {_text(classification_source)}</div>
</div>
<h2>Brief Interpretation(s)</h2>
<p class="cv-prose">{_text(_brief_interpretation(report))}</p>
<p class="cv-notice">Decision-support output for educational and research use; qualified human review is required.</p>
""",
        page=1,
    )

    evidence_rows = "".join(
        "<tr>"
        f'<td class="cv-source">{_text(section["source"])}</td>'
        f"<td>{_text(section['status'].replace('_', ' '))}</td>"
        "<td><ul class=\"cv-list\">"
        + _items(
            f"{item['label']}: {item['value']}" for item in section["items"]
        )
        + "</ul></td></tr>"
        for section in content["evidence_sections"]
    )
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
            f"<td>{_text(source['status'].replace('_', ' '))}</td></tr>"
        )
    sources = "".join(source_rows)
    page_three = _page(
        f"""
<h1>Variant(s) classification</h1>
<p><b>Source-attributed result:</b> {_text(classification)} ({_text(classification_source)})</p>
<p><b>Conflict status:</b> {_text(conflict['status'].replace('_', ' '))}; severity: {_text(conflict['severity'])}.</p>
<ul class="cv-list">{_items(conflict['findings'])}</ul>
<p>The application has not independently adjudicated an ACMG/AMP classification.</p>
<h2>Comments and scope</h2>
<ul class="cv-list">{_items([*interpretation['warnings'], *content['limitations'], *content['reviewer_notes']])}</ul>
<h2>Literature References</h2>
{''.join(reference_rows) or '<p>No trusted literature reference is available.</p>'}
<h2>Data Sources</h2>
<table aria-label="Data sources"><thead><tr><th>Source</th><th>Status</th></tr></thead><tbody>{sources}</tbody></table>
<p class="cv-notice">Providers: {_text(', '.join(content['provenance']['providers']) or 'None recorded')}</p>
""",
        page=3,
    )
    return page_one, page_two, page_three


__all__ = [
    "render_draft_report_preview_pages",
    "stable_allele_identity",
]
