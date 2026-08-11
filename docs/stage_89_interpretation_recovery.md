# Stage 89 - Interpretation Recovery Policy

## Primary-model invariant

Every variant first uses the user-selected Variant Interpretation Model. Conflict
status changes only prompt context; it never selects a different model. A configured
fallback is eligible only after an operational failure and is never used for variant
difficulty, evidence strength, conflict, or phenotype content.

## Bounded recovery sequence

The policy is finite and deterministic:

```text
selected model attempt 1
-> one retry for timeout, connection, HTTP 429, or HTTP 5xx
-> one structured-output repair after parse/schema failure
-> optional operational fallback model
```

`VARIANT_INTERPRETATION_MAX_RETRIES` defaults to `1` and cannot exceed `1`.
Authentication failures, invalid configuration/request failures, other HTTP 4xx
responses, and internal conversion failures are not retried or fallback-routed.

Structured repair sends the same sanitized Evidence Object and strict response schema
to the same selected model with one constrained recovery instruction. Raw invalid
output is not echoed into a new prompt. If repair is exhausted, an optional fallback
may receive the original normalized evidence prompt.

## Operational fallback

`VARIANT_INTERPRETATION_FALLBACK_MODEL` is optional and disabled when empty. It must
differ from the configured primary model. If a user selects that same model as the
primary at runtime, fallback is disabled for that analysis instead of changing the
primary selection.

A successful fallback result records the actual fallback model in interpretation
provenance, adds a concise operational-recovery warning, and emits a structured
`variant_interpretation_recovery` event with `fallback_used=true`. A failed fallback
retains Stage 88 failure diagnostics with the fallback model and fallback-use state.
No prompt, response body, exception message, or secret is logged.

## Acceptance evidence

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m stage89_interpretation_recovery
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\run_stage60_acceptance.py
```

Validated on 2026-08-11:

- Stage 89 recovery gate: `9 passed`;
- transient retry, schema repair, and operational fallback covered;
- identical normalized evidence verified across primary and fallback calls;
- conflict/no-conflict primary-model invariance verified;
- non-transient request and authentication failures verified as ineligible;
- full repository suite: `1014 passed, 4 skipped`;
- Stage 60 release gate: passed at `85.69%` coverage.

Stage 90 subsequently added the phenotype non-concordance response and prompt
contract while retaining this recovery sequence.
