# Stage 36 — Final Interpretation Only Report

Status: complete and offline verified

Date: 2026-08-08

## Scope

Stage 36 builds Output B only from validated Stage 35 routing results. It is
independent of editable Output A and does not make another LLM request.

## Contract

- One entry is emitted for every submitted variant in original input order.
- Each entry contains final interpretation text or an explicit failure status.
- No ranking, score, model prompt, provenance payload, or raw evidence appears
  in Output B.
- An `unresolved` Stage 35 result is prefixed with the stable statement
  `Conflict remains unresolved.`
- Reconfirmation, review edits, or rerunning Stage 35 invalidates stale Output
  B.
- The report schema, interpretation length, control characters, fields, order,
  and rendered UTF-8 size are bounded and validated.

## Viewing and download

The Streamlit result view displays each variant in a separate native container
and provides a text download named `final-interpretations.txt`. Failed variants
remain visible with a stable failure status; private provider error messages are
not exposed.

## Verification

- Original input order is preserved even when routing results arrive unordered.
- Every input variant receives an interpretation or failure status.
- Missing and failed Stage 35 results remain distinct.
- Unresolved conflict is explicit in the final text.
- Extra evidence fields are rejected.
- Output B is visible and downloadable in the Streamlit result view.
- Stage 35 and Stage 36 focused tests and the full regression suite pass with
  mocked providers.

Live-provider generation was not run because Stage 36 is deterministic and
requires no provider call.

## Completion condition

Output B is generated separately from Output A, covers every variant without
ranking or raw-evidence repetition, and is available for viewing and download.
