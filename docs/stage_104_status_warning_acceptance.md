# Stage 104 - Status and Warning UX Acceptance Suite

## Outcome

The completed-analysis surface now answers every required reviewer question without
opening developer logs or technical provider drawers. Stage 104 adds an exact
`reports_ready` count to the product summary and validates the existing variant-first
status and consequence-oriented warning architecture as one coherent primary UX.

## Reviewer questions

| Question | Primary UI answer |
|---|---|
| How many variants were analyzed? | The analysis summary states the exact retained Evidence Object count. |
| How many reports are ready? | The analysis summary counts cards in `Report ready` or `Report ready with partial evidence` state. |
| Which variant needs attention? | The affected card retains its input position, allele label, and `Interpretation requires attention` status. |
| Which source is missing? | The same card names the capability and source outcome, such as `ClinVar: unavailable`. |
| Missing evidence or application failure? | `PARTIAL` explains retained-evidence limitations; `ACTION REQUIRED` identifies exhausted interpretation and provides a retry/review action. |

`draft_reports_prepared` remains available and distinct from `reports_ready`; merely
having a draft record does not imply that its interpretation or minimum report state
is ready. Technical diagnostics remain available in `Show technical details`
expanders, collapsed by default and rendered after the primary status answer.

## Run the gate

```powershell
.\.venv\Scripts\python.exe -m pytest `
  -m stage104_status_warning_acceptance -q
```

## Validation

Validated on 2026-08-11:

- Stage 104 gate: `4 passed`;
- combined Stage 95 and Stage 104 gate: `14 passed`;
- complete repository suite: `1142 passed, 6 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 312 deselected`, `85.20%`
  coverage;
- the primary Streamlit surface was exercised with technical drawers closed;
- no external provider or live-model call was made.

Stage 105 professor-testcase end-to-end acceptance is not implemented here.
