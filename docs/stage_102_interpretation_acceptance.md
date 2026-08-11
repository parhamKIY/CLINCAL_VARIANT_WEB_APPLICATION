# Stage 102 - Interpretation Acceptance Suite

## Outcome

The corrected interpretation architecture now has one deterministic acceptance gate
covering every scenario required by the post-acceptance roadmap. The suite exercises
the production interpretation, recovery, Draft Variant Report, warning-semantics, and
variant-status boundaries without network access or live model credentials.

## Acceptance scenarios

| Scenario | Accepted behavior |
|---|---|
| Normal evidence | Produces a successful, evidence-grounded interpretation. |
| Irrelevant phenotype | Succeeds, states `no supported association found`, and continues from remaining evidence. |
| Partial provider coverage | Succeeds with evidence-limited language and treats unavailable evidence as unknown. |
| Conflicting evidence | Uses conflict-aware context and explicitly leaves the disagreement unresolved. |
| Transient model failure | Performs the one permitted transient retry and recovers successfully. |
| Structured-output failure | Performs the one constrained repair request and accepts the corrected structure. |
| Total interpretation failure | Retains the allele and Draft Variant Report, exposes `Interpretation requires attention`, and emits an `ACTION REQUIRED` notice with a retry/review action. |

The failure scenario verifies that collected evidence remains reviewable after the
model task is exhausted. Internal exception names do not become the primary user
status, and the test does not fabricate an interpretation to fill the gap.

## Run the gate

```powershell
.\.venv\Scripts\python.exe -m pytest `
  -m stage102_interpretation_acceptance -q
```

## Validation

Validated on 2026-08-11:

- Stage 102 gate: `7 passed`;
- complete repository suite: `1129 passed, 6 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 299 deselected`, `85.20%`
  coverage;
- all acceptance fixtures were deterministic and offline;
- no production interpretation or report behavior was changed by this test-only
  stage.

Stage 103 reference acceptance-suite work is not implemented here.
