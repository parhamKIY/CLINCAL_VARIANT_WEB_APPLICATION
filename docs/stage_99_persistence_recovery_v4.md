# Stage 99 - Persistence and Recovery V4

## Outcome

SQLite schema `4` adds a normalized, fail-closed report recovery projection while the
validated canonical pipeline snapshot remains the recovery source of truth.

## Persisted report state

The `report_recovery_states` table stores one row per input variant with:

- complete `ReportData V4`;
- deterministic DOCX artifact metadata and template version;
- interpretation prompt version;
- resolved literature and data-source references;
- report edits, reviewer summary/notes, and selection history;
- include/exclude state;
- draft, confirmed, or finalized state;
- report warning state.

Artifact identity is the tuple `analysis_id`, assembly-qualified `variant_id`, and
`report_version`. The report version is a stable prefix of the deterministic DOCX
content digest, so material report changes produce a new version without relying on a
wall-clock value.

## Recovery behavior

Saving replaces the ordered normalized recovery rows atomically with the canonical
pipeline snapshot. Loading revalidates both projections and fails closed if either is
missing or altered. Report preview data, reviewer edits, selection, confirmation,
references, warnings, and artifact identity therefore return after refresh/restart.

Recovery reuses the persisted interpretation result and performs no LLM call. DOCX
bytes remain regenerable from the approved `ReportData`; only bounded artifact metadata
is stored. Schema `3` databases migrate to schema `4` and backfill valid recovery rows.

## Validation

Validated on 2026-08-11:

- Stage 99 gate: `8 passed`;
- persistence/database regression selection: `83 passed, 1 skipped`;
- complete repository suite: `1106 passed, 5 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 275 deselected`, `85.22%`
  coverage.

Stage 100 subsequently added approved per-variant DOCX export and the selected final
Word package without changing the Stage 99 recovery model.
