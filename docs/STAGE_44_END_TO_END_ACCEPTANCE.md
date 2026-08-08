# Stage 44 — End-to-End Acceptance

Status: complete and offline verified

Date: 2026-08-08

## Final MVP case

The deterministic case in
`data/samples/stage44_acceptance_case.json` contains five ordered GRCh38
variants and three HPO terms. It contains no patient or sample identifiers.

The acceptance test exercises the complete application workflow:

1. manual variants and normalization;
2. VEP, GeneBe, direct ClinVar, ClinGen/CSpec, Phen2Gene, and Monarch/MyDisease
   evidence contracts;
3. Evidence Object V2, lineage, pre-review conflict audit, and the conditional
   enrichment decision;
4. one editable Output A report per variant;
5. a user evidence addition, Draft save, classification edit, and immutable
   original comparison;
6. confirmation, Reviewed Evidence Packages, and post-review conflict audit;
7. LLM-1 for no-conflict packages and LLM-2 for the edited conflict;
8. an allowed unresolved LLM-2 result and one isolated LLM timeout;
9. ordered Output B entries for all five variants, with interpretation or an
   explicit failure only.

The final case uses mocked provider responses and provider-neutral LLM
adapters. Unmocked HTTP remains blocked. Provider-specific success, failure,
retry, no-match, validation, and privacy behavior is covered by the complete
Stage 43 suite included in the final gate.

## Acceptance guarantees

- All five submitted variants remain present and ordered through both outputs.
- Output A preserves immutable machine evidence and distinguishable user edits.
- No LLM request occurs before confirmation.
- Post-review conflict auditing controls the LLM-1/LLM-2 route.
- An unresolved conflict is retained explicitly.
- One interpretation failure does not remove evidence or other results.
- Output B excludes raw evidence, review objects, ranking, and Top-N fields.
- Evidence lineage and source provenance remain available in Output A.

## Final command

```powershell
.\.venv\Scripts\python.exe tests\run_stage44_acceptance.py
```

The command gates Python compilation, installed dependency consistency, the
repository secrets audit, the five-variant Stage 44 workflow, the complete
Stage 43 regression suite, and a minimum 80% backend/frontend/config coverage
threshold.

## Completion condition

Stage 44 passes only when every final command succeeds offline and the
five-variant case satisfies every acceptance guarantee above.
