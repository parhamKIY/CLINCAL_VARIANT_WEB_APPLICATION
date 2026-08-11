# Stage 84 — In-App High-Fidelity Report Preview

## Implemented boundary

Stage 84 makes the clinical report the first completed-analysis review surface. The
default application tab is `Clinical report review`; pipeline progress, API status,
provider detail, and evidence dashboards are retained in the secondary
`Analysis and provider details` tab.

The active workflow still persists `DraftVariantReport V2`. Stage 84 therefore renders
that validated, privacy-checked record directly to deterministic HTML. It does not
introduce a second persisted report state or perform the Stage 86 lifecycle refactor.
The `ReportData V4` to DOCX path from Stage 83 remains authoritative for future final
artifact generation.

## Review surface

Each report has three explicit document pages:

1. title, privacy-safe metadata, clinical features, method, result emphasis, and brief
   interpretation;
2. source-attributed main findings and long-form interpretation;
3. classification context, comments/scope, literature references, and data sources.

The preview uses a white Letter-like page, Times New Roman typography, restrained blue
headings, the professor-family red result border, gold classification emphasis, gray
finding cells, and responsive page padding. All dynamic strings are HTML escaped.
Only already-validated canonical reference URLs become links, and executable JavaScript
is not enabled.

## Navigation and controls

- The heading shows `Variant X of N`.
- The navigation identity always includes assembly, chromosome, position, reference,
  and alternate allele; gene and HGVS are secondary annotations.
- `Previous` and `Next` move through reports in original input order.
- The page control shows the current page and total page count.
- `Edit` points to the existing audited report editor under technical details; Stage 85
  owns the future document-like editing redesign.
- `Include this variant in Final Report` keeps the existing reporting-only, auditable
  selection behavior.
- Existing evidence editing, comparison, confirmation, and provenance remain available
  after the preview as technical details.

## Acceptance evidence

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -m stage84_report_preview -q
```

The gate checks three-page document structure, professor-family presentation markers,
stable allele identity, HTML escaping and link safety, source-attributed findings, and
report-first application hierarchy.

Stage 85 document-like editing and Stage 86 ReportData lifecycle integration are not
implemented here.
