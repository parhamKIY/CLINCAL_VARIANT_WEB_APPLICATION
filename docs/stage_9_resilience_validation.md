# Stage 9 Resilience Validation

## Scope and frozen contracts

Starting checkpoint: `a5939f4a93c00d8c5b25db80bcdb5c8de8773439`.

This is a validation-only stage. No production module changed. EvidenceObject remains `2.5`; SQLite schema remains `4`; pipeline schema remains `3.2`; Stage 7 coverage and Stage 8 final-disposition projections remain runtime-only schema `1.0`.

Internal readiness remains `READY`, `READY_WITH_LIMITATIONS`, `RESCUE_REQUIRED`, and `MINIMUM_IDENTITY_FAILURE`. Final disposition remains `READY`, `READY_WITH_LIMITATIONS`, and `BLOCKED`. Human review remains mandatory; this stage adds no ACMG automation, CSpec execution, diagnosis, treatment recommendation, provider, or LLM prompt/model behavior.

## Deterministic correctness evidence

| Area | Existing deterministic coverage exercised |
|---|---|
| Shared provider contract | `tests/test_provider_resilience.py`: success, valid `no_match`, timeout, connection failure, 403, 429, 5xx, malformed response, bounded retries/circuits, fallback provenance. |
| ERepo exact context | `tests/test_erepo.py`, `tests/test_erepo_orchestration.py`, and `tests/test_erepo_stage1_contract.py`: exact/equivalent identity, structured no-match, rejected candidate, UUID/version, assembly/CPRA mismatch, timeout/403/429, malformed/schema drift, source attribution. |
| MedGen roles | `tests/test_medgen.py`, `tests/test_medgen_gene_disease.py`, and `tests/test_medgen_phenotype_gene.py`: all three semantic roles, zero-UID no-match, unverified candidate diagnostics, exact gene/HPO acceptance, XML/schema/partial failure, disabled/unattempted states, and per-query deduplication. |
| Composition | `tests/test_stage6a_shadow_composition.py` and `tests/test_stage6b_active_promotion.py`: VEP state separation, exact CPRA, assembly/transcript/HGVS conflict rejection, safe promotion, provenance, unpromoted consequence/impact/MANE/canonical, and LLM isolation. |
| Coverage/disposition | `tests/test_stage7_evidence_coverage.py` and `tests/test_stage8_final_disposition.py`: all coverage states, retrieval-state preservation, fallback/no-match semantics, correlation, per-variant order, readiness-to-disposition mapping, sparse-valid and blocked identity cases. |
| Population/literature | `tests/test_failure_injection.py` and `tests/test_stage7_evidence_coverage.py`: failure-only fallback, direct/fallback/no-match/failure coverage, and correlation semantics. |
| Persistence and golden | `tests/test_stage99_persistence_recovery_v4.py` and `tests/test_stage105_professor_testcase.py`: schema-3-to-4 recovery, no regeneration, fail-closed tamper handling, ERC-01 through ERC-07 normalized golden projection. |
| Live-runner contract | `tests/test_stage9_resilience_validation.py`: isolated class execution, atomic checkpoint after each result, normalized failure redaction, resume, unknown-class rejection, and configuration-disabled `NOT_RUN`. |

Command:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q tests\test_stage9_resilience_validation.py tests\test_provider_resilience.py tests\test_failure_injection.py tests\test_erepo.py tests\test_erepo_orchestration.py tests\test_erepo_stage1_contract.py tests\test_medgen.py tests\test_medgen_gene_disease.py tests\test_medgen_phenotype_gene.py tests\test_stage6a_shadow_composition.py tests\test_stage6b_active_promotion.py tests\test_stage7_evidence_coverage.py tests\test_stage8_final_disposition.py tests\test_stage98_technical_diagnostics.py tests\test_stage99_persistence_recovery_v4.py tests\test_stage105_professor_testcase.py
```

Result: `264 passed in 26.10s`.

The tests prove same-gene query reuse without variant merging, per-variant failure isolation, input-order/cardinality retention, and prevention of stronger sibling evidence inflating another variant. They also prove direct ClinVar/MyVariant-derived ClinVar and population/MedGen shared-upstream correlation remains non-independent through Stage 7 and Stage 8 reviewer projections.

LLM-boundary assertions confirm raw provider payloads, `shadow_composition`, Stage 7 coverage records, and Stage 8 technical diagnostics are not wholesale prompt input. Stage 6 active canonical fields retain permitted sanitized provenance only. No prompt change was made.

## Full resilience gate

Command:

```powershell
& .\.venv\Scripts\python.exe tests\run_stage78_resilience_acceptance.py
```

Compilation, secrets audit, and the Stage 78 multi-variant scenario passed (`1 passed, 1432 deselected in 3.27s`). The complete offline suite produced `1425 passed, 2 failed, 6 skipped in 144.35s`.

The two failures are the frozen historical baseline only:

- `tests/test_pipeline.py::TestFrontendFoundation::test_manual_table_executes_pipeline` expects six Streamlit dataframes and observes eight.
- `tests/test_stage79_acceptance_assets.py::test_acceptance_registry_freezes_all_stage79_defects` expects missing `docs/acceptance_failures_v1.md`.

No other deterministic failure occurred. The runner stops at the permitted historical full-suite result, so its later Testing V3 step is intentionally not run.

## Bounded live operational observations

The new `tests/run_stage9_live_validation.py` reuses production clients but is an operational test harness, not a correctness gate. Each invocation accepts one class, has existing provider timeouts/retries, writes a normalized checkpoint immediately after that class, keeps previous checkpoints with `--resume`, continues after an operational failure, and stores no raw response, raw VCF row, genotype, sample identifier, secret, or exception text.

All probes used the public non-PHI GRCh38 `1:941284:G>A` context, `SCN1A` where a gene was necessary, and `HP:0001250` where an HPO term was necessary. Observations were made on 2026-08-16 UTC; they are point-in-time reachability only.

| Query class | Operational outcome | Retrieval state | Accepted type/count | Attempts | Latency | Note |
|---|---|---|---|---:|---:|---|
| ERepo exact curated context | `no_match` | `no_match` | expert-curated context / 0 | 1 | 1378.7 ms | Valid missingness, not an operational failure. |
| MedGen gene-disease support | `success` | `accepted_records` | gene-disease support / 1 | 2 | 1687.4 ms | Exact verified support only. |
| MedGen phenotype-gene support | `success` | `accepted_records` | phenotype-gene support / 1 | 2 | 1063.0 ms | Does not create a Phen2Gene rank or score. |
| MedGen disease/HPO context | `success` | `accepted_records` | disease/HPO context / 1 | 2 | 1298.5 ms | Does not create GenCC validity. |
| VEP annotation | `success` | `accepted_records` | annotation / 1 | 1 | 1444.5 ms | Isolated VEP request only. |
| Population retrieval | `available` | `accepted_records` | population evidence / 1 | 1 | 911.7 ms | Existing bounded fallback chain remained available. |
| Literature retrieval | `no_match` | `no_match` | literature evidence / 0 | n/a | 1098.3 ms | Valid missingness, not an operational failure. |

The command shape for each observation was:

```powershell
& .\.venv\Scripts\python.exe tests\run_stage9_live_validation.py --classes <one-query-class> --output $env:TEMP\stage9-live-<class>.json
```

## Defects and remaining risks

One Stage 9 harness defect was found before live observation: the public VEP probe omitted required manual-row `qual` and `filter` fields. It was localized to the new validation harness, reproduced as `VCFProcessingError`, corrected in that harness only, and then the isolated VEP observation succeeded. No production behavior changed.

Remaining risk: live observations can change with provider availability, rate limits, and external payloads. They are intentionally not treated as proof of deterministic correctness or as input to coverage/readiness/disposition. The two historical baseline failures remain out of scope.
