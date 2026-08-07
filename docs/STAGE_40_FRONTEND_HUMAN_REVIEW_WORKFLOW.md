# Stage 40 — Frontend human-review workflow

Status: complete and offline verified

Date: 2026-08-08

## Review workflow

The existing searchable HPO picker remains unchanged. After Phase A, Output A
shows one detailed evidence report per retained variant with four views:

- full-access reviewed JSON;
- immutable original machine evidence;
- append-only edit history;
- human confirmation and conflict status.

The JSON editor permits nested edits, additions, and deletions. Reviewer notes
are stored separately. `Save draft` validates the complete report, invalidates
any stale confirmation and interpretation, updates the active pipeline result,
and persists the Stage 39 Draft when an analysis ID exists. `Reset to original`
restores the reviewed copy and notes while retaining the audit history. The
original machine report is never modified.

## Two model roles

The UI now makes both Stage 35 roles explicit:

1. `Strong conflict model` is selected first from the full curated catalog.
2. `Low-cost no-conflict model` is selected from a focused list of inexpensive
   models plus the configured fallback.

The deterministic conflict audit controls routing. The low-cost model handles
confirmed evidence with no meaningful conflict. Moderate, major, or critical
conflicts use the strong model. Changing either selector does not discard the
collected Phase A evidence.

## Confirmation and Output B

Each variant must be explicitly confirmed. Confirmation builds the immutable
Reviewed Evidence Package, reruns the post-review conflict audit, and persists
the updated review state. The UI reports:

- no meaningful conflict;
- conflict detected before review;
- conflict after user edits;
- unresolved conflict after interpretation.

`Generate interpretation` remains disabled until every variant is confirmed.
Once enabled, it resumes Pipeline Phase B with the selected low-cost and strong
models, persists the completed state, and displays the existing final-
interpretation-only Output B view and download control.

## Verification

- Searchable HPO selection remains covered by the frontend regression tests.
- Both model selectors expose their intended catalogs and routing roles.
- Full-access edits preserve immutable originals and persist the Draft.
- Reset restores the reviewed report while retaining edit history.
- Output B is absent and generation is disabled before confirmation.
- Confirmation unlocks generation, passes both selected model roles to Phase B,
  persists the result, and displays Output B.
- The complete offline suite passes with mocked LLM/provider boundaries.

## Completion condition

A reviewer can edit and confirm Output A and then generate Output B entirely
through the Streamlit interface without modifying code or bypassing the human
confirmation gate.
