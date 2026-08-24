# Stage 11.3 Variant-Level Failure Isolation Audit

## Scope and current state

This audit was completed before Stage 11.3 implementation. It preserves the
Stage 11.1 field-level interpretation contract and the Stage 11.2 bounded
analysis lifecycle. It does not change EvidenceObject, provider, AI
classification, interpretation, or clinical-report schemas.

## Current execution path

```text
frontend.execution.AnalysisJob
  -> backend.pipeline.run_analysis
  -> canonical variant processing and annotation
  -> backend.report.build_evidence_objects_isolated
  -> backend.pipeline._build_evidence_and_report
  -> evidence readiness
  -> backend.variant_interpretation.interpret_variants
  -> backend.variant_report.build_draft_variant_reports
  -> evidence review and report lifecycle projections
  -> PipelineResult validation and persistence
  -> frontend partial-result and variant-status projections
```

`evidence_construction_outcomes` is already the per-input cardinality ledger.
It preserves every original variant index, maps successful variants to compact
EvidenceObject indexes, and records bounded failure metadata for unsuccessful
variants. The UI already consumes this ledger to show evidence-construction
failures without exposing internal exceptions.

## Failure boundaries

### P0 - Evidence construction is isolated locally but aborted globally

- Location: `backend.pipeline._build_evidence_and_report`.
- Current behavior: `build_evidence_objects_isolated` retains successful
  siblings, but the pipeline raises on the first preliminary or final failed
  outcome. Readiness, interpretation, and draft-report generation never run.
- Risk: one invalid EvidenceObject prevents every valid sibling from reaching
  the LLM and reviewer workflow.
- Required change: continue with the successful EvidenceObjects, preserve the
  complete construction-outcome ledger, and finish with aggregate `partial`
  status. If no EvidenceObject survives, retain the existing analysis-level
  failure.

### P0 - PipelineResult rejects every sparse downstream collection

- Location: `backend.pipeline.validate_pipeline_result`.
- Current behavior: partial construction is required to use failed workflow
  state and is forbidden from containing readiness, interpretation, draft,
  review, lifecycle, confirmation, or routing records. Downstream validators
  also assume `input variant index == EvidenceObject list index`.
- Risk: removing the pipeline raise alone would create an invalid, unrecoverable
  result and could associate a surviving variant with the wrong evidence.
- Required change: use construction outcomes as the authoritative mapping from
  original input index to compact EvidenceObject index. Downstream records stay
  sorted by original input index and must cover exactly the successful
  construction outcomes.

### P0 - Integrity and report lifecycle gates assume full cardinality

- Locations: `backend.variant_integrity`, `backend.report_lifecycle`, evidence
  review construction, and final-review validation.
- Current behavior: partial evidence explicitly cannot produce report records;
  builders enumerate compact collections and therefore renumber later siblings.
- Risk: either all reports are suppressed or original variant identity is lost.
- Required change: allow sparse report/review records keyed by original input
  index. Keep failed input records in the integrity ledger with null evidence
  and report identities. Never fabricate an EvidenceObject or clinical report
  for a failed construction.

### P1 - Unexpected interpretation exception escapes the per-variant loop

- Location: `backend.variant_interpretation.interpret_variants`.
- Current behavior: expected LLM and interpretation errors become failed
  per-variant results; another ordinary `Exception` terminates the batch.
- Risk: a variant-local implementation error discards later valid LLM work and
  leaves the pipeline to the analysis-level exception handler.
- Required change: convert ordinary variant-local exceptions into the existing
  bounded failed interpretation result and execution-trace event. Process-level
  exceptions remain outside `Exception` and are not swallowed.

### P1 - Editing and retry APIs index sparse lists by original index

- Locations: draft update, interpretation retry, confirmation, and selected
  report projection in `backend.pipeline`.
- Current behavior: direct list indexing is correct only for fully successful
  contiguous batches.
- Risk: after a middle EvidenceObject failure, an action for variant 2 can
  mutate variant 3 or raise an unrelated index error.
- Required change: resolve artifacts through explicit `variant_index` mappings.

### P1 - Normalized persistence projection assumes contiguous report indexes

- Location: `backend.database._v3_projection`.
- Current behavior: normalized rows are emitted only when draft count equals
  total input count, and selected report indexes directly index the compact
  EvidenceObject list.
- Risk: sparse results either lose normalized report rows or fail persistence
  when a later sibling retains its original input index.
- Required change: derive evidence and interpretation mappings from the
  construction ledger and persist successful sparse records under their
  original input indexes. Existing tables and recovery schema remain unchanged.

### P2 - Aggregate and trace projections undercount construction failures

- Locations: analysis completion trace and frontend status projections.
- Current behavior: the terminal trace compensates for missing interpretation
  records, while some status projections still pair compact evidence and
  readiness lists positionally.
- Risk: successful siblings can be displayed under the wrong input index or
  aggregate counts can be unclear.
- Required change: preserve original indexes in readiness, interpretation,
  draft, review, and lifecycle records; derive aggregate failure counts from
  the construction ledger plus interpretation results.

## Chosen architecture

The implementation remains additive and mapping-based:

```text
input variants (complete, original order)
  -> evidence_construction_outcomes (complete, original order)
     -> success: evidence_object_index
     -> failed: bounded failure metadata, no fabricated evidence
  -> EvidenceObjects (successful subset, compact storage order)
  -> readiness / interpretation / draft / review records
     (successful subset, original variant_index)
```

Successful siblings may proceed to reviewer drafts. Failed construction
variants remain explicit through the input list, construction ledger,
execution trace, integrity record, partial-analysis UI, and deterministic final
report limitation. A clinical draft is not fabricated when the minimum
EvidenceObject contract failed.

## Safety invariants

- Exact canonical identity remains mandatory.
- Successful outcome indexes remain contiguous and integrity checked.
- Every downstream record maps to one successful construction outcome.
- Failed variants are never silently dropped from analysis-level status.
- Provider and LLM semantics are unchanged.
- Existing validation remains strict; only full-cardinality assumptions are
  replaced with explicit mapping validation.
- If every EvidenceObject fails, the analysis remains a terminal failure.
