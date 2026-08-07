# Stage 34 — Human Confirmation Gate and Reviewed Evidence Package

Status: complete and verified

Date: 2026-08-07

## Scope

Stage 34 prevents final LLM interpretation before explicit confirmation of a
Stage 33 draft. It builds one immutable Reviewed Evidence Package per
confirmed variant without calling an LLM.

## Package contents

- Original machine-generated evidence
- User-reviewed report
- User-added evidence
- Reviewer notes
- Append-only edit history
- Provider provenance
- Pre-review conflict audit
- Recomputed post-review conflict audit
- Time-zone-aware confirmation timestamp

## Integrity rules

- The package identifier covers every confirmed package field except itself.
- Reload validation rejects changes to original evidence, reviewed evidence,
  edit history, user-added evidence, notes, provenance, conflict results, or
  confirmation metadata.
- User-added evidence must exactly match the added records in edit history.
- Confirmation cannot precede any saved edit.
- Post-review conflict results are recomputed and compared during reload.

## Full-access review behavior

- Clinically material edits are detected in both legacy flat fields and
  Evidence Object V2 nested sections.
- Classification, assembly, transcript, gene, and condition edits trigger a
  deterministic user-override conflict when they differ from preserved source
  evidence.
- UI confirmation is written into the pipeline result for later Stage 35
  routing.
- Saving a newer draft invalidates both the UI confirmation and the matching
  pipeline package, requiring explicit reconfirmation.

## Verification

- Nested classification overrides route as conflicts.
- Tampering with reviewed evidence, post-review audit, or edit history is
  rejected.
- Pipeline packages preserve variant order and do not call the LLM.
- Saving after confirmation invalidates confirmed state.
- Full regression suite: passed.

## Completion condition

Every confirmed variant has a validated Reviewed Evidence Package tied to the
current draft, original evidence remains preserved, the post-review audit is
deterministic, and no final LLM interpretation runs before confirmation.
