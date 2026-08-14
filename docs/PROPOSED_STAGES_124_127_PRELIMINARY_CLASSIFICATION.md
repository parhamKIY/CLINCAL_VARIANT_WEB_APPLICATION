# Proposed Stages 124–127 — Preliminary Classification and Review Workflow

**Status:** Stages 124–125 implemented and verified; Stages 126–127 proposed

## Approved product behavior

The initial result must use the gathered, source-attributed evidence to produce:

- an LLM-generated **preliminary classification**;
- a brief, rational, evidence-grounded interpretation; and
- visible source details, limitations, and conflicts.

When the available evidence is clear, sufficient, and non-conflicting, the LLM may
produce one preliminary classification from the approved controlled vocabulary.
When evidence is materially ambiguous, conflicting, or insufficient, the initial
result must state `Ambiguous — user review required` and must not force a
classification. It must instead show the conflict or missing evidence for user
review.

The user may review and edit the evidence/context and request a revised
interpretation. Any resulting final report remains review-required and must retain
its source provenance and version history. It is not a laboratory sign-out,
diagnosis, or autonomous clinical adjudication.

## Stage 124 — Preliminary-classification contract

**Objective:** Extend the interpretation contract so the LLM can return an
evidence-grounded preliminary classification when the evidence permits one.

### Required behavior

- Add a controlled `preliminary_classification` result field.
- Add an explicit outcome for ambiguous, conflicting, or insufficient evidence.
- Require a concise rationale, cited evidence identifiers, limitations, and conflict
  status.
- Continue to validate that the response uses only the supplied Evidence Object.
- Do not permit a forced classification when material conflict remains unresolved.

### Acceptance gate

- Clear synthetic evidence produces a validated preliminary classification.
- Conflicting or insufficient synthetic evidence produces the ambiguous-review
  outcome without a forced label.
- Invalid model output is repaired or retained as an explicit recoverable failure.

### Implemented behavior and verification

`backend/variant_interpretation.py` now uses schema `1.2` and prompt
`variant-interpretation-v1.4`. Successful model results contain a controlled
`preliminary_classification_status`, an optional controlled
`preliminary_classification`, a source-grounded classification rationale, and
limitations. A material pre-review conflict requires the `ambiguous` outcome with
no label; a forced label is rejected and follows the existing bounded repair path.

Retained schema `1.1` interpretations remain reviewable without inventing a label.
They normalize to an ambiguous preliminary outcome only for validation, while
retaining their original stored schema/provenance.

`tests/test_stage124_preliminary_classification.py` covers clear classification,
ambiguous conflict, forced-label rejection, repair, and legacy compatibility. The
full offline suite passed `1281 passed, 6 skipped` on 2026-08-14.

**Suggested commit:** `feat(interpretation): add preliminary classification contract`

## Stage 125 — Initial result presentation

**Objective:** Present the first LLM classification and interpretation in the
existing report template without hiding source provenance.

### Required behavior

- Display the preliminary classification, concise interpretation, rationale, and
  limitations when available.
- Display `Ambiguous — user review required` with the relevant conflicting or
  missing evidence when no classification is justified.
- Keep ClinVar, GeneBe, population, phenotype, and other source observations
  separately attributed and visible.
- Update HTML, DOCX, and persisted report data consistently.

### Acceptance gate

- Initial HTML and DOCX output show a preliminary classification only for clear
  evidence.
- Ambiguous output visibly identifies the unresolved evidence and never presents a
  false consensus.
- Existing source-attribution and call-quality disclosures remain intact.

### Implemented behavior and verification

The initial HTML preview, review screen, and professor-template DOCX now display
the initial LLM evidence synthesis separately from the final system result. Clear
evidence shows the controlled preliminary label, its rationale, and limitations.
Materially ambiguous evidence shows `Ambiguous — user review required` with no
forced label. `System classification: Not independently determined` remains
visible, while provider classifications, call quality, and source provenance stay
separate and attributed.

New Draft Variant Reports use schema `2.3`; persisted schema `2.2` reports remain
readable as pre-Stage-124 output. Newly persisted renderer data uses `ReportData`
schema `5.0`, while retained `4.0` records remain readable.

**Suggested commit:** `feat(report): show preliminary classification in initial output`

## Stage 126 — Reviewer editing and revised interpretation

**Objective:** Enable a user to amend the reviewable context and intentionally
request a revised LLM interpretation.

### Required behavior

- Provide an auditable review input for user edits and conflict-resolution context.
- Preserve the original evidence package and initial interpretation unchanged.
- Send only the approved revised context to the LLM for a new preliminary
  classification and interpretation.
- Persist result versions, timestamps, user-supplied rationale, and model
  provenance.
- Never rerun the model merely because a user confirms a report.

### Acceptance gate

- A reviewer edit can produce a distinct, traceable revised interpretation.
- The initial and revised versions remain recoverable and distinguishable.
- Confirmation without an explicit reinterpretation request does not call the LLM.

**Suggested commit:** `feat(review): support revised evidence interpretation`

## Stage 127 — Final-report workflow and release verification

**Objective:** Make the reviewed result suitable for final report generation while
preserving preliminary status, provenance, and unresolved ambiguity.

### Required behavior

- Let the user select an initial or revised reviewed interpretation for final-report
  generation.
- Preserve preliminary-classification wording and all source attribution in final
  output.
- Keep ambiguous results visibly review-required rather than silently upgrading them
  to a classification.
- Add unit, pipeline, persistence, HTML, DOCX, and Streamlit acceptance coverage.

### Acceptance gate

- Clear, ambiguous, failed-model, revised, and final-report scenarios pass targeted
  and full regression suites.
- The final output retains the selected version, evidence provenance, limitations,
  and user-review history.

**Suggested commit:** `test(workflow): verify preliminary classification finalization`

## Execution order

Implement and validate one stage at a time in the order `124 → 125 → 126 → 127`.
This document records planned work only; `docs/PROJECT_DECLARATION.md` must be
updated later with implemented facts and verification results.
