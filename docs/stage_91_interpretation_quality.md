# Stage 91 - Interpretation Quality Upgrade

## Outcome

Stage 91 adds an evidence-based benchmark and selection gate for the Variant
Interpretation Model. It does not alter request construction, schema handling,
retry behavior, runtime routing, or configured model defaults.

The configured provider model catalog timed out during a bounded read-only check on
2026-08-11. No model completions were requested and no provider cost was incurred.
Consequently, no live candidate comparison or model promotion is claimed. The
configured `gpt-5.4-mini` Variant Interpretation Model remains unchanged; its current
configuration is not treated as benchmark proof.

## Fixed evaluation set

`data/benchmarks/stage91_cases.json` defines exactly seven review cases:

1. strong phenotype match;
2. partial phenotype match;
3. irrelevant phenotype;
4. conflicting ClinVar evidence;
5. sparse evidence;
6. rich literature;
7. no literature.

Each candidate must contain all seven unique case identifiers. Conflict handling is
scored on the ClinVar-conflict case. Phenotype restraint is scored on the three
phenotype cases. Every case also records groundedness, hallucinations,
clinical-style coherence, structured-output reliability, latency, token use, cost,
status, and reviewer notes.

## Recommendation gate

`backend/interpretation_quality.py` validates candidate records, normalizes quality
scores, and produces a deterministic decision. A candidate is eligible only when:

- it was executed against a live provider and human reviewed;
- groundedness, clinical style, conflict handling, and phenotype restraint are each
  at least `0.80`;
- no evaluated case contains a hallucination;
- all seven structured outputs are reliable and successful;
- latency, token use, and estimated cost are recorded.

At least two unique candidates are required, including the configured default. If
none qualifies, the result is `no_recommendation` and the default stays unchanged.
The evaluator never mutates `.env` or application settings. Offline fixtures verify
the scoring implementation but cannot establish model quality or select a default.

The command-line reviewer gate accepts completed JSON evidence:

```powershell
.\.venv\Scripts\python.exe tools\evaluate_stage91_models.py --input <review.json> --output <decision.json>
```

## Task isolation

The decision task is fixed to `variant_interpretation`. Phenotype Extraction Model
selection remains independently configured and is neither evaluated nor changed by
this gate.

## Acceptance status

The benchmark definition, strict evidence validator, deterministic comparison,
offline acceptance coverage, and documentation are implemented. A live default
recommendation remains intentionally pending until two or more candidates have
complete human-reviewed provider evidence. Stage 92 subsequently added Reference
Model V2 separation.

Validated on 2026-08-11:

- Stage 91 gate: `9 passed`;
- complete repository suite: `1030 passed, 4 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 198 deselected`, `85.11%` coverage.
