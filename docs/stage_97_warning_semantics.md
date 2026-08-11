# Stage 97 - Warning Semantics V2

## Outcome

Variant cards now classify reviewer-facing notices by consequence instead of
displaying raw provider or exception text.

## Severity contract

`frontend/warning_semantics.py` defines four stable severities:

- `INFO`: expected evidence absence, including no exact record, no literature,
  and no supported phenotype association;
- `PARTIAL`: analysis succeeded, but optional evidence was unavailable,
  unsupported, or otherwise limited;
- `ACTION REQUIRED`: interpretation could not be produced after recovery attempts;
- `BLOCKING`: a minimum normalized-input or report invariant is unavailable.

A `no_match` source state is always expected absence and is never converted into a
provider failure. A successful fallback suppresses a primary-source availability
notice because the capability was ultimately supplied.

## Presentation

`frontend/evidence_review.py` renders compact native Streamlit badges and
consequence-oriented copy inside the owning variant card. Expected missingness uses an
informational badge instead of a crash-like warning. Raw exception names, endpoints,
and implementation details remain outside the primary reviewer message. Technical
provider state remains available in the existing collapsed details expander.

## Validation

Validated on 2026-08-11:

- Stage 97 gate: `14 passed`;
- combined Stage 96/97 gate: `22 passed`;
- complete repository suite: `1089 passed, 5 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 258 deselected`, `85.26%`
  coverage.

Stage 98 subsequently added a collapsed technical diagnostics drawer without exposing
its implementation-oriented fields in the primary reviewer workflow.
