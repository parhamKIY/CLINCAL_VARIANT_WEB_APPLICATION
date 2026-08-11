# Stage 98 - Technical Diagnostics Drawer

## Outcome

Each variant status card now has a collapsed `Show technical details` drawer with one
safe diagnostic row per retained provider/capability record.

## Diagnostic contract

`frontend/technical_diagnostics.py` projects these bounded fields:

- provider;
- assembly-qualified variant identity;
- operational status;
- attempt count;
- latency in milliseconds;
- fallback-used flag;
- failure category;
- provider-specific consequence note.

Attempt and latency telemetry are matched from retained per-variant evidence when the
provider path persisted them. Missing telemetry is displayed as `Not recorded`; the UI
does not invent attempt or latency values. No raw provider response, endpoint payload,
exception text, credentials, or patient data enters the diagnostic projection.

No-match remains a valid missing-evidence result with failure category `none`.
Successful fallback rows retain the primary failure category and identify the provider
that supplied the evidence.

## Presentation

`frontend/evidence_review.py` uses a native collapsed Streamlit expander and a bounded
read-only dataframe. The main reviewer card remains focused on report state and
consequence-oriented notices. Diagnostic computation is an in-memory projection with
no provider calls and no custom CSS.

## Validation

Validated on 2026-08-11:

- Stage 98 gate: `9 passed`;
- combined Stage 96-98 gate: `31 passed`;
- complete repository suite: `1098 passed, 5 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 267 deselected`, `85.20%`
  coverage.

Stage 99 subsequently added normalized report-first persistence and restart recovery
without changing the Stage 98 diagnostics presentation.
