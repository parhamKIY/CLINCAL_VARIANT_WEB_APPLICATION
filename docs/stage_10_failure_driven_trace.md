# Stage 10 Failure-Driven Input-to-LLM Audit

## Frozen boundary and trace model

Starting approved checkpoint: `3fdb81bce14359e9b65fc0ee97d5f40d2f04a8db`.

The test-only `tests/stage10_trace.py` emits trace schema `1.0`: redacted identity, first-loss stage, cause, expectedness, EvidenceObject record paths, Stage 7 capability states, persisted readiness, Stage 8 disposition, LLM inclusion/exclusion manifest, hashed LLM projection, interpretation status, and optional recovery summary. It retains no provider payload, raw prompt, VCF row, genotype, sample identifier, PHI, or secret.

Confirmed versions: EvidenceObject `2.5`; SQLite `4`; pipeline `3.2`; Stage 7 coverage `1.0` runtime-only; Stage 8 disposition `1.0` runtime-only.

## Validated defect correction

| Item | Result |
|---|---|
| Defect | Rejected MedGen candidate diagnostics reached the Variant Interpretation prompt. |
| Severity | `CRITICAL` — non-evidentiary rejected material reached the LLM. |
| First breach | `LLM_SANITIZATION`. |
| RED | `python -m pytest -q tests/test_stage10_validated_defect.py` → `1 failed in 0.16s`. |
| Root cause | `sanitize_evidence_object()` correctly retained diagnostics, then `shadow_free_evidence_for_llm()` removed only composition metadata. |
| Correction | The canonical LLM projection now recursively excludes `candidate_diagnostics`; it does not modify normalized MedGen contexts, EvidenceObject persistence, coverage diagnostic paths, or reviewer diagnostics. |
| GREEN | Focused boundary/MedGen/Stage 6–8/persistence command → `150 passed in 6.34s`. |
| Corrective commit | `d9d912e8bc402ecfbdbdd67f438eebb22ed5ca2a`. |

Accepted MedGen `records` remain in the LLM projection where previously allowed. `candidate_diagnostics` remains upstream for provenance, reviewer traceability, and Stage 7 `diagnostic_paths`, but is `EXCLUDED_BY_DESIGN` from the LLM projection for gene-disease, phenotype-gene, and disease/HPO contexts.

## Deterministic trace scenarios

| ID | First loss | Cause | Expected | Result |
|---|---|---|---:|---|
| A | `RETRIEVAL` | `PROVIDER_OPERATIONAL_FAILURE` | yes | Safe Stage 6 fallback survives; annotation is `DEGRADED`; disposition remains `READY_WITH_LIMITATIONS`. |
| B | `RETRIEVAL` | `VALID_NO_MATCH` | yes | ERepo no-match yields no invented assertion and remains an informational limitation. |
| C | `SEMANTIC_VERIFICATION` | `UNVERIFIED_CANDIDATE` | yes | MedGen candidate diagnostics remain upstream; support is `UNAVAILABLE` with `no_verified_gene_association`; rejected candidates are absent from the LLM projection. |
| D | `COMPOSITION` | `TRANSCRIPT_CONFLICT` | yes | Unsafe transcript/HGVS fields remain unpromoted; active annotation is coherent and `DEGRADED`. |
| E | `IDENTITY` | `IDENTITY_FAILURE` | yes | `MINIMUM_IDENTITY_FAILURE` derives `BLOCKED`; the LLM adapter receives zero requests. |
| F | `LLM_INVOCATION` | `LLM_INVOCATION_FAILURE` | yes | Existing per-variant failure handling retains EvidenceObjects and produces explicit retryable failure state. |

`tests/test_stage10_failure_driven_trace.py` proves trace schema, fallback/no-match distinction, transcript conflict, coverage-to-disposition behavior, manifest exclusions, and structural block/no-LLM behavior. Existing `tests/test_defect08_interpretation_recovery.py`, `tests/test_stage89_interpretation_recovery.py`, and `tests/test_stage99_persistence_recovery_v4.py` prove failure isolation, retry using retained evidence, and recovery without interpretation regeneration.

## Provenance, coverage, disposition, and LLM manifest

Surviving accepted evidence remains source-attributed in EvidenceObject and its normalized evidence path reaches Stage 7 coverage and Stage 8 reviewer notices. Shared-upstream groups remain correlation context only and do not create independent evidence. GenCC validity remains distinct from MedGen support; Phen2Gene ranking remains distinct from phenotype support; automated ACMG context remains distinct from ERepo context.

| LLM path/category | Manifest state |
|---|---|
| Accepted normalized MedGen records | `INCLUDED` where already allowed |
| MedGen `candidate_diagnostics` | `EXCLUDED_BY_DESIGN` |
| `shadow_composition` / `annotation_promotion` metadata | `EXCLUDED_BY_DESIGN` |
| Runtime Stage 7 coverage | `EXCLUDED_BY_DESIGN` |
| Runtime Stage 8 technical diagnostics | `EXCLUDED_BY_DESIGN` |
| Raw provider payload / raw VCF / genotype / sample identifier | `EXCLUDED_BY_DESIGN` |

## Bounded real-pipeline trace and regression

The bounded Stage 8/9 public non-PHI pipeline fixture remains the real-pipeline trace boundary: it exercises normalized EvidenceObject, readiness, runtime coverage/disposition, and reviewer projection without retaining raw provider content; final interpretation remains deterministic/mock to avoid live-model cost and nondeterminism. Stage 9 live observations remain operational-only and are not deterministic proof.

Focused command:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q tests\test_stage10_validated_defect.py tests\test_stage10_failure_driven_trace.py tests\test_stage9_resilience_validation.py tests\test_stage6a_shadow_composition.py tests\test_stage6b_active_promotion.py tests\test_stage7_evidence_coverage.py tests\test_stage8_final_disposition.py tests\test_stage98_technical_diagnostics.py tests\test_medgen.py tests\test_medgen_gene_disease.py tests\test_medgen_phenotype_gene.py tests\test_defect08_interpretation_recovery.py tests\test_stage89_interpretation_recovery.py tests\test_stage99_persistence_recovery_v4.py tests\test_stage105_professor_testcase.py
```

Frozen historical full-suite failures remain out of scope: manual-table dataframe count and missing `docs/acceptance_failures_v1.md`.

Result: `180 passed in 27.30s`.

`python tests/run_stage78_resilience_acceptance.py` passed compilation, secrets audit, and Stage 78 (`1 passed, 1438 deselected in 3.18s`), then reached `1431 passed, 2 failed, 6 skipped in 147.35s`. The only failures were the two frozen historical baselines.
