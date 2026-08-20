# Live-Run Stage 1 Evidence Construction Audit

## Scope and baseline

This audit is limited to Stage 1 of `COMPLETE_CODEX_EXECUTION_ROADMAP.md`. It adds bounded diagnostic metadata and a deterministic reproduction; it does not change EvidenceObject construction semantics, batch isolation, provider orchestration, schemas, readiness, persistence, LLM input, or reporting.

Audit baseline:

- Branch: `master`
- Pre-audit HEAD: `9c5ade6`
- PipelineResult schema: `3.3`
- SQLite schema: `4`
- Recovery request schema: `3`
- EvidenceObject schema: `2.5`
- Full regression: `1476 passed, 6 skipped`
- Testing V3: `1038 passed, 4 skipped, 440 deselected`; coverage `83.47%`

The 2026-08-17 failure was replayed read-only from the persisted pipeline snapshot. No raw provider payload, prompt, model output, sample/genotype field, or patient identifier was copied into this artifact or the reproduction.

## Production construction path

The exact production path is:

1. `backend.pipeline._build_evidence_and_report()` receives normalized annotation and phenotype candidates.
2. `backend.report.build_evidence_objects()` performs the preliminary per-candidate loop.
3. `backend.report.build_evidence_object()` applies controlled active annotation projection, source mapping, phenotype mapping, normalized V2 section construction, capability construction, shadow composition/promotion, conflict audit, and `sanitize_evidence_object()`.
4. `sanitize_evidence_object()` validates the EvidenceObject, sanitizes bounded fields/context trees, validates it again, serializes it, and enforces the `65536`-byte total limit.
5. Conditional population/literature enrichment runs after the preliminary objects are buildable.
6. `build_evidence_objects()` repeats over the final enriched candidates.
7. Only a completely returned final list is assigned to `PipelineResult.evidence_objects`.
8. Evidence readiness and variant-cardinality/integrity synchronization run only after that assignment.
9. `run_analysis()` maps `EvidenceObjectError` to the safe analysis-level `evidence_object_failed` result and skips LLM/report stages.

Current source anchors after the Stage 1 instrumentation:

- Builder: `backend/report.py:5585`
- Batch loop: `backend/report.py:5702`
- Sanitizer/validator boundary: `backend/report.py:2512`
- Serialized-size gate: `backend/report.py:2803`
- Preliminary build: `backend/pipeline.py:2602`
- Conditional enrichment: `backend/pipeline.py:2633`
- Final build: `backend/pipeline.py:2646`
- PipelineResult assignment: `backend/pipeline.py:2653`
- Integrity synchronization: `backend/pipeline.py:2676`
- Analysis-level exception mapping: `backend/pipeline.py:3190`

## Exact live root cause

All four preliminary EvidenceObjects were buildable. After conditional enrichment, variants 0 through 2 remained buildable. Variant 3 produced a normalized, schema-valid EvidenceObject whose final sanitized serialization was `73063` bytes. The total EvidenceObject safety bound is `65536` bytes, so the failure occurred at the aggregate serialized-envelope gate.

The exact safe diagnostic is:

```text
evidence_step=serialization_bounds
evidence_field=evidence
failure_code=serialized_size_exceeded
failure_scope=per_variant
```

The largest normalized section on the failing shape was `evidence.phenotype_relationship`, dominated by retained `mydisease` context. GenCC/pathogenicity context and conditional enrichment also contributed. No individual missing optional field caused the exception. Missing GeneBe context, VEP operational failure, VariantValidator fallback without consequence, valid provider no-match, gnomAD 403, and UCSC fallback were all representable and are not the proven exception cause.

The failing requirement is **STRUCTURAL / SAFETY-CRITICAL** because it is the bounded total EvidenceObject serialization contract. The contributing content is valid optional evidence context, so Stage 2 must compact it deterministically without weakening the bound or reclassifying provider missingness.

## Live per-variant audit

| Index | Stable canonical allele digest | Preliminary bytes | Final bytes | Builder entered | Furthest step | Builder result | Safe failure code | EvidenceObject emitted individually |
|---:|---|---:|---:|---|---|---|---|---|
| 0 | `allele-sha256:a575c05d77b326c2f8b75c1587fc6982aa0ad7effd48c32cab821f021895bb17` | 52746 | 58754 | yes | serialization bounds passed | success | none | yes |
| 1 | `allele-sha256:9d081f5b5ca4e7c6c752f2090d585dd714eb0259c8cd9ad1795631c7fe31c73b` | 38110 | 44166 | yes | serialization bounds passed | success | none | yes |
| 2 | `allele-sha256:471c582909cc44175bdc6ba95c98465db9578e65cf2b3af4caeaeb6e8c36137e` | 42567 | 47529 | yes | serialization bounds passed | success | none | yes |
| 3 | `allele-sha256:acbb0b1c10badfa4f6cb3e7b5dceb08a1ad052c4c13acb547eafefe12ef16a75` | 65289 | 73063 | yes | `serialization_bounds` | failure | `serialized_size_exceeded` | no |

This proves roadmap mechanism **B: one variant fails and aborts the batch**. It disproves A because three variants build successfully, C because the later integrity gate is never reached, and D because no other mechanism is required to explain the four-to-zero transition.

## Deterministic non-PHI reproduction

`tests/test_live_run_stage1_evidence_audit.py` constructs four exact-identity synthetic candidates using existing EvidenceObject fixtures. The shape includes:

- VEP operational failure with successful VariantValidator exact-identity fallback;
- GeneBe unavailable;
- mixed MyVariant/ClinVar usable and valid no-match states;
- GenCC, Phen2Gene, MyDisease, and MedGen support;
- gnomAD forbidden with UCSC gnomAD fallback;
- a schema-valid optional-context volume just below the preliminary EvidenceObject bound;
- final conditional enrichment that reaches the same `serialization_bounds` failure.

The focused TDD sequence was:

- RED after the valid reproduction reached the expected boundary: `1 failed, 2 passed`; failure was the absence of bounded diagnostic attributes/logging.
- GREEN after minimal instrumentation: `3 passed`.

The diagnostic log contains only variant index, stable digest, bounded step/path/code, and scope. The test explicitly proves that the genotype value retained upstream by the fixture does not enter the log.

Final verification:

- Focused Stage 1, EvidenceObject, Stage 7, Stage 8, Stage 10, integrity, persistence, and recovery set: `179 passed, 1 skipped`.
- Full offline regression: `1479 passed, 6 skipped`.
- Testing V3: `1038 passed, 4 skipped, 443 deselected`; coverage `83.45%`.
- Stage 78: compilation passed; secrets audit passed; resilience scenario `1 passed, 1484 deselected`; full regression `1479 passed, 6 skipped`; Testing V3 passed at `83.45%`; gate passed.
- Standalone secrets audit: passed.
- PipelineResult, SQLite, recovery request, and EvidenceObject schema versions remain unchanged.

## Current batch-abort behavior

The deterministic A/B/C case proves:

- A is buildable and entered.
- B triggers `serialized_size_exceeded` and raises from the per-variant loop.
- C is independently buildable but is never entered by the batch loop.
- The list containing A exists only inside `build_evidence_objects()` and is lost when the exception propagates.
- The caller assignment occurs only after a full list returns, so `PipelineResult.evidence_objects` remains its initialized empty list.
- The final readiness/cardinality/integrity gate does not discard the siblings; it is unreachable.
- Analysis-level error handling then marks evidence as failed and skips LLM/report, producing the observed zero EvidenceObjects.

Instrumentation does not alter this behavior.

## Evidence-stage event map

The affected live run emitted `25` instances of:

```text
event=pipeline_stage_started stage=evidence
```

| Emission source | Count | Meaning | Classification |
|---|---:|---|---|
| `backend/pipeline.py:2594` through `_set_stage()` | 1 | Actual top-level evidence-stage entry | Correct top-level event |
| `backend/pipeline.py:2621` via `notify_enrichment_progress()` | 16 | Four terminal provider-status callbacks for each of four variants: population, LitVar, Europe PMC, PubMed | Repeated/misnamed sub-operation event |
| Same callback, initiated at `backend/conditional_enrichment.py:2965` | 4 | Population lookup entered `running` for each variant | Repeated/misnamed sub-operation event |
| Same callback, initiated by the LitVar `running` notification at `backend/conditional_enrichment.py:2259` and bridged at `backend/conditional_enrichment.py:3007` | 4 | LitVar lookup entered `running` for each variant | Repeated/misnamed sub-operation event |

There is no existing `pipeline_stage_started` emission for individual EvidenceObject construction. The Stage 1 addition emits a separate warning only on a construction failure. Event cleanup is deferred.

## Recommended Stage 2 change — not implemented

Stage 2 should preserve the global `65536`-byte safety bound while introducing deterministic, provenance-preserving per-section compaction/truncation budgets and explicit truncation warnings for large optional context, especially MyDisease/phenotype relationship context. It should then isolate EvidenceObject construction per variant so a proven per-variant failure becomes an explicit per-variant disposition while buildable siblings continue in order. Readiness, cardinality, persistence, and LLM sanitation must consume only successfully validated EvidenceObjects and retain the failed variant's bounded technical disposition outside the LLM payload.

## Explicit non-goals

This stage did not implement:

- any EvidenceObject compaction, truncation, size-limit, or batch-isolation fix;
- provider failover/retry/readiness orchestration changes;
- evidence-stage event cleanup;
- UI or PyArrow changes;
- Persian disease/syndrome extraction;
- persistent evidence caching or storage-engine migration;
- Franklin integration;
- schema changes, LLM changes, or unrelated refactoring.
