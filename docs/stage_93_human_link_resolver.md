# Stage 93 - Canonical Human-Link Resolver

## Outcome

Every normal-looking reviewer link now resolves to a deterministic human-readable
record whenever a stable mapping exists. Provider APIs and raw JSON targets remain
provenance only and cannot be promoted to ordinary report links.

## Resolver policy

- PubMed PMID values resolve to canonical PubMed article pages.
- PMCID values resolve to canonical PubMed Central article pages.
- DOI values resolve through `https://doi.org/`.
- ClinVar VCV accessions resolve to human variation pages; RCV and SCV accessions
  resolve through the human ClinVar search page.
- GeneBe receives a human variant page only for a validated GRCh37/GRCh38 primary
  chromosome, positive coordinate, and exact REF/ALT allele.
- Ensembl receives a human variation page only when a valid rsID and supported
  assembly are present. Transient VEP REST endpoints are never clinical references.
- MyVariant.info remains explicitly labelled `Programmatic annotation source`.
  Its record identifier may be shown as provenance, but its raw JSON/API endpoint is
  never the primary reviewer-facing link.
- When no validated human record can be resolved, the data source remains visible
  with `link_status=unavailable`; no URL is fabricated.

## Integration

- `backend/human_links.py` owns deterministic provider-specific resolution and
  machine-endpoint rejection.
- `backend/references.py` applies the policy while canonicalizing evidence references.
- `backend/reference_model.py` resolves source records from exact evidence identity;
  it and `backend/report_data.py` reject machine-readable URLs during validation.
- Streamlit review, HTML preview, Final Clinical Report Markdown, and professor-family
  DOCX output expose only validated human links and label MyVariant.info as
  programmatic provenance.

Stage 94 subsequently added the dedicated deterministic regression matrix and a
separately gated optional live reachability check.

## Validation

Validated on 2026-08-11:

- Stage 93 gate: `8 passed`.
- complete repository suite: `1045 passed, 4 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 213 deselected`, `85.15%` coverage.
