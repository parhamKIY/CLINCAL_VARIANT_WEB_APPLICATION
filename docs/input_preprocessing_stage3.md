# Selected-Variant Input Stabilization — Stage 3

## Explicit XLSX selection

For an `.xlsx` upload, the Streamlit workflow shows the actual workbook worksheet
names. The user selects one worksheet, reviews its source rows, and explicitly
chooses one to ten rows for analysis. Worksheet order is retained only for display;
the application does not rank, prioritize, choose Top-N, choose a first ten, or use
source `FILTER`, QUAL, DP, AD, GQ, phenotype score, or pathogenicity to select rows.

The selected worksheet and source row numbers are retained as input provenance.
Changing worksheets clears the row selection. A selection of more than ten rows is
blocked without truncation; an empty selection cannot start analysis.

## Identity preprocessing review

Only after the user starts analysis, the selected source rows enter the Stage-2
adapter. The UI does not convert zero alleles or call reference services while a
workbook is being browsed. Source quality values remain caller metadata and are
shown separately from the input identity status.

Every selected row has one existing `InputPreprocessingResult`:

- `ACCEPTED_DIRECT` is shown as **Accepted directly**.
- `NORMALIZED_AND_ACCEPTED` is shown as **Normalized safely**, with the existing
  canonical identity and bounded reference-verification provenance.
- `IDENTITY_UNRESOLVED` is shown as **Requires attention**, with its precise
  existing reason and no canonical identity.

The review reports selected-source, canonical/analyzable, and unresolved counts
separately. Therefore unresolved source rows remain visible and accounted for but
never enter annotations, Evidence Objects, providers, or the sanitized LLM payload.

PipelineResult remains `3.3`, SQLite remains `4`, recovery requests remain `3`, and
EvidenceObject remains `2.5`.
