# Stage 86 - Per-Variant Report Lifecycle Refactor

## Implemented boundary

Stage 86 makes the per-variant report a persisted primary workflow artifact. Each
accepted input variant now receives exactly one ordered `VariantReportRecord V1`
when its Draft Variant Report and interpretation are ready.

The record contains:

- the validated `ReportData V4` source of truth;
- deterministic editable DOCX metadata: filename, SHA-256, byte size, and template
  version;
- stable report, analysis, and original input-index identity;
- draft, confirmation, and finalization timestamps;
- one lifecycle state: `draft`, `confirmed`, or `finalized`.

DOCX bytes are regenerated deterministically from stored `ReportData`; binary files
are not embedded in pipeline JSON or SQLite state.

## Lifecycle behavior

The implemented lifecycle is:

```text
evidence package -> interpretation -> ReportData -> DOCX draft
-> review/edit -> selection -> confirmation -> final artifact
```

Reviewer edits continue through the append-only `DraftVariantReport V2` mutation
boundary. Every accepted edit immediately resynchronizes the same lifecycle record,
including its ReportData content and DOCX digest. Material included-report edits
invalidate prior confirmation before the record returns to `draft`.

Confirmation advances the same record to `confirmed` and stores the package's exact
confirmation timestamp in both the lifecycle record and ReportData review state.
Finalization advances every confirmed record to `finalized`; excluded records are not
deleted.

## Selection and order contract

`include_in_final_report` is a reporting decision only. Excluded variants retain
their evidence, interpretation, Draft Variant Report, ReportData, DOCX metadata,
selection history, confirmation, and lifecycle record.

Final Clinical Report selection is derived from the ordered lifecycle records. The
selected subset therefore remains in original zero-based input order without sorting
by gene, classification, reviewer action time, or inclusion time.

Persisted pipeline snapshots use schema `3.0`. The bounded database loader upgrades
schemas `2.8` and `2.9` by constructing ordered lifecycle records without rerunning
annotation or interpretation. Current production analyses always populate the new
records; empty records remain accepted only for older in-memory test/compatibility
fixtures that did not execute report construction.

## Acceptance evidence

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m stage86_report_lifecycle
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\run_stage60_acceptance.py
```

Validated on 2026-08-11:

- Stage 86 gate: `4 passed`;
- full repository suite: `986 passed, 4 skipped`;
- Stage 60 release gate: passed, including compilation, dependency consistency,
  secrets audit, ten-variant acceptance, and the complete Testing V3 suite at
  `85.88%` coverage.

At Stage 86 completion, Stage 87 cardinality-gate work was not yet implemented. It is
now implemented separately in `docs/stage_87_variant_integrity.md`.
