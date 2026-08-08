# Stage 43 — Testing V2

Status: complete and offline verified

Date: 2026-08-08

## Acceptance coverage

The `stage43_testing_v2` marker selects the complete offline test suite so
Stage 43 remains an expansion of the established suite instead of a parallel
set of duplicated fixtures.

| Contract | Covered behavior |
|---|---|
| Existing regression | VCF parsing, normalization, VEP, GeneBe, ClinVar, ClinGen/CSpec, report generation, and the provider-neutral LLM client |
| Phenotype APIs | Phen2Gene and Monarch/MyDisease success, failure isolation, retry, and explicit no-match/missingness semantics |
| Evidence | Evidence Object V2 validation, bounded lineage, provenance, upstream-source collapse, and explicit missingness |
| Conflict | No conflict, classification, condition, transcript, source-quality, user-override, and pre/post-review audits |
| Human review | Output A creation, edits, additions, immutable originals, reset, Draft save, confirmation, and the pre-confirmation LLM gate |
| LLM routing | Confirmed no-conflict packages use LLM-1; meaningful conflict uses LLM-2; unresolved is valid; human and source evidence remain distinguishable |
| Output | Every submitted variant remains ordered in Output A and Output B; Output B contains interpretation/failure only and excludes ranking, Top-N, and raw evidence |

The Stage 43 cross-stage acceptance test exercises three variants through
Draft editing, confirmation, both LLM routes, an unresolved conflict, and final
Output B generation. All provider and LLM calls are mocked; unmocked HTTP is
blocked by the suite-wide fixture.

## Commands

Run the Stage 43 acceptance gate:

```powershell
.\.venv\Scripts\python.exe -m pytest -m stage43_testing_v2
```

Run only the new cross-stage contract test:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_pipeline.py::TestStage43TestingV2
```

## Completion condition

Stage 43 passes only when the complete offline regression, API, evidence,
review, routing, and output suite passes with live external services blocked.
