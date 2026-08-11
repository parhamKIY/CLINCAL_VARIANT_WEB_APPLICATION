# Stage 95 - User-Facing Analysis Summary Redesign

## Outcome

Completed results now begin with a concise product-level summary before the clinical
report and technical views. A reviewer can immediately see how many variants were
analyzed, how many draft reports were prepared, how many variants have partial source
coverage, and how many interpretations require attention.

## Count contract

`frontend/analysis_summary.py` derives every count from per-variant pipeline state:

- variants analyzed: the number of retained Evidence Objects;
- draft reports prepared: the number of Draft Variant Reports;
- partial source coverage: the number of Evidence Objects with a non-available,
  attempted core source/capability status;
- interpretations requiring attention: the number of failed per-variant
  interpretations.

Global provider-status record counts are never used as variant totals. Optional
capabilities recorded as `not_assessed`, `not_applicable`, `not_triggered`, or
`skipped` do not create a false partial-coverage count.

## Presentation

- `frontend/ui.py` renders the bordered `Analysis complete` summary before the report
  and technical tabs using native Streamlit elements.
- Input copy follows `analysis_context.input_type`: Excel, manual variant, VCF, and
  compressed VCF inputs each receive accurate validation wording.
- The old `VCF processing` label is replaced by input-aware stage terminology.
- Per-provider statuses remain available under `Technical provider details`, collapsed
  by default, and the report remains the first tab.

Stage 96 subsequently added variant-first status cards without changing these
analysis-level count semantics.

Stage 104 subsequently added the explicit `reports_ready` count derived from those
stable card states while retaining the distinct draft-report count.

## Validation

Validated on 2026-08-11:

- Stage 95 gate: `10 passed`.
- complete repository suite: `1067 passed, 5 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 236 deselected`, `85.19%` coverage.
