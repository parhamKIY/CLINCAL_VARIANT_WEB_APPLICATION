# Stage 103 - Reference Acceptance Suite

## Outcome

The user-facing literature and provenance architecture now has one deterministic
acceptance gate spanning canonical reference construction, Draft Variant Report
content, ReportData V4 projection, the HTML report preview, and the authoritative
DOCX output.

## Required checks

| Check | Accepted behavior |
|---|---|
| Literature links | Every retained PMID, PMCID, and DOI resolves to its exact canonical human record and remains clickable in HTML and DOCX. |
| Category separation | Numbered literature contains only PMID, PMCID, and DOI records; database and tool provenance remains unnumbered Data Sources. |
| Raw JSON | A MyVariant.info API endpoint cannot become a normal reference or hyperlink and is labelled as a programmatic annotation source. |
| Fabricated links | Invalid PubMed, PMC, DOI, and ClinVar identifiers remain explicitly unlinked. |
| Human provider pages | Available ClinVar, Ensembl, and GeneBe identities resolve only to validated human-readable record pages. |
| Missing stable link | A source without the required stable identifier remains visible as clean unlinked provenance without `None`, raw URLs, or invented targets. |

The deterministic gate validates exact link targets and their presence in generated
HTML/DOCX artifacts without making network calls. The existing optional Stage 94 live
check remains the bounded mechanism for point-in-time HTTP reachability and content-
type verification; this stage does not claim a new live-provider result.

## Run the gate

```powershell
.\.venv\Scripts\python.exe -m pytest `
  -m stage103_reference_acceptance -q
```

## Validation

Validated on 2026-08-11:

- Stage 103 gate: `9 passed`;
- complete repository suite: `1138 passed, 6 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 308 deselected`, `85.20%`
  coverage;
- all Stage 103 checks were deterministic and offline;
- no production reference or report behavior was changed by this acceptance-only
  stage.

Stage 104 status and warning UX acceptance work is not implemented here.
