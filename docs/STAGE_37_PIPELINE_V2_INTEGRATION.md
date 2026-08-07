# Stage 37 — Pipeline V2 Integration

Status: complete and offline verified

Date: 2026-08-08

## Two-phase workflow

Phase A is executed by `run_analysis`. It performs input validation,
normalization, annotation, phenotype processing, Evidence Object V2 creation,
conflict auditing, conditional enrichment, and editable Output A generation.
It never calls an LLM and pauses with `workflow_state` set to
`awaiting_confirmation`.

Phase B is executed by `resume_confirmed_analysis`. It resumes a validated copy
of the same pipeline result, optionally confirms supplied reviewed drafts,
requires a confirmed package for every submitted variant, performs Stage 35
two-layer routing, and builds Stage 36 Output B.

## State contract

Pipeline schema `2.3` records these explicit workflow states:

- `phase_a_running`
- `awaiting_confirmation`
- `phase_b_running`
- `completed`
- `failed`

The analysis identifier and all Phase A evidence remain unchanged across the
Phase B handoff. Progress callbacks receive isolated validated snapshots at the
start of Phase B, after routing, and after Output B.

## Resilience and warnings

- Existing annotation and enrichment provider isolation remains active.
- Stage 35 retains one result per variant, so one LLM timeout or invalid model
  response does not remove successful interpretations for other variants.
- Output B represents each failed interpretation explicitly.
- Annotation, conditional-enrichment, structured pipeline, and model warnings
  are aggregated, bounded, and deduplicated in the pipeline result.
- A degraded Phase A remains `partial` after an otherwise successful Phase B.
- Invalid or incomplete confirmation blocks Phase B before any LLM call.

## Verification

- Phase A pauses after editable Output A without calling an LLM.
- Phase B resumes from the paused result and preserves `analysis_id`.
- User edits are included in confirmed packages before routing.
- All variants must be confirmed before Phase B starts.
- A strong-model timeout affects only its routed variant.
- Phase A and model warnings survive through final Output B.
- Phase B progress transitions through 50%, 85%, and 100%.
- The full regression suite passes with mocked providers.

Live-provider generation was not run because it would consume configured
provider quota; the complete orchestration is verified offline.

## Completion condition

The API-first workflow can pause after evidence collection and editable Output
A, then resume the same analysis after confirmation to produce per-variant
interpretations and Output B without losing unaffected evidence or warnings.
