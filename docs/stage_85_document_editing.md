# Stage 85 — Document-Like Editing Workflow

## Implemented boundary

The report remains the default review state. Selecting `Edit` opens an explicit
`Edit clinical report` mode directly below the high-fidelity preview. The previous
generic `Edit report` technical tab is removed.

The editor is organized by the document regions the reviewer is changing:

1. page 1 — `Brief Interpretation(s)`;
2. page 2 — `Variant interpretation`;
3. page 3 — `Variant(s) classification`;
4. page 3 — `Reviewer notes` under comments and scope.

Each region is visually grouped and explains which generated content remains
read-only. `Save report edits`, `Reset editable fields`, and `Cancel` are batched in
one form. Cancel discards unsaved widget state; reset uses the machine-original values.

## Audit and confirmation behavior

Stage 85 reuses the validated `DraftVariantReport V2` edit contract. Only reviewer
summary, interpretation narrative, conflict/classification-summary wording, and
reviewer notes can change. Allele identity, provider evidence, citations, provenance,
and the machine original are immutable.

Every material save appends field-level old/new values, timestamp, and reviewer
context. Existing confirmation packages and finalization state are invalidated before
the edited report can be reconfirmed.

## ReportData and Word regeneration

`backend/report_data_projection.py` deterministically projects the current reviewed
Draft Variant Report into a validated transient `ReportData V4` record. It preserves
stable allele identity, HPO concordance, source-attributed classifications and
population evidence, literature-only references, provider provenance, warnings,
review history, and Final Report selection history.

The current projection is rendered through the Stage 83 professor-template DOCX path.
`Download editable Word report` is therefore rebuilt from the latest saved reviewer
fields on every review rerun. Non-literature source references stay in Data Sources
instead of being promoted into the numbered bibliography.

Stage 85 does not persist a second report lifecycle. Stage 86 owns the planned change
that makes `ReportData V4` the primary stored per-variant artifact.

## Acceptance evidence

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -m stage85_document_editor -q
```

The gate verifies explicit closed/open/cancel editor state, document-region grouping,
the four-field mutation boundary, append-only audit records, immutable generated
evidence, confirmation invalidation, transient ReportData projection, and regenerated
DOCX content.

Validated on 2026-08-11:

- Stage 85 gate: `6 passed`;
- full repository suite: `982 passed, 4 skipped`;
- Stage 60 release gate: passed, including compilation, dependency consistency,
  secrets audit, ten-variant acceptance, and the complete Testing V3 suite at `85.83%`
  coverage.

At Stage 85 completion, Stage 86 lifecycle work was not yet implemented. It is now
implemented separately in `docs/stage_86_report_lifecycle.md`.
