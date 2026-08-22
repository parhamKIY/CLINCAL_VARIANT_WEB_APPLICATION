# Stage 4.3 Audit Trail, Performance Baseline, and Acceptance

## Scope and verdict

Stage 4.3 audited the existing XLSX-to-draft-report workflow without changing
clinical interpretation, evidence semantics, provider selection, prompts, or
schemas.

The persisted production path is traceable from normalized allele identity to
the per-variant report lifecycle record. A performance-observability gap was
confirmed after the report stage: terminal database persistence and report
record regeneration consumed substantial time but had no separate duration
event. The implementation now emits bounded persistence start/finish events
with run correlation, assignment status, variant count, analysis identifier,
and duration.

## Audit trail map

| Boundary | Preserved metadata | Current location |
| --- | --- | --- |
| Selected XLSX input | worksheet/row provenance, source representation, preprocessing status, canonical index and identity | `PipelineResult.input_preprocessing_results` |
| Canonical variant | assembly plus chromosome, position, REF, ALT; stable allele digest; immutable input order | `PipelineResult.variants` and `variant_integrity_records` |
| Annotation | normalized provider records, provider status, primary/fallback role, upstream source and warnings | `annotations[].sources` |
| Evidence | exact allele identity, provider lineage, availability, shared-upstream groups, capability status, fallback provenance and limitations | `EvidenceObject.provenance`, `capability_results`, `warnings` |
| LLM interpretation | exact variant index/identity, result schema, model, response model, prompt version, generated time, success/failure and bounded error type | `variant_interpretation_results[]` |
| Draft report | variant identity/context, evidence sections, limitations, interpretation status/model/prompt version, provider and upstream summaries | `draft_variant_reports[]` |
| Persisted report | report ID, originating analysis ID, lifecycle state, renderer-neutral `ReportData`, deterministic DOCX metadata and timestamps | `variant_report_records[]` |
| Final clinical report | report ID, originating analysis ID, generated time and source schema versions | `final_clinical_report` |
| Operational correlation | one safe run ID on all log events plus terminal analysis ID | structured application log |

The Stage 4.3 acceptance test verifies the same canonical identity across input
preprocessing, integrity records, EvidenceObjects, interpretations, draft
reports, and persisted report records. It also verifies that VariantValidator
fallback attribution and the Ensembl VEP primary timeout remain visible in the
EvidenceObject and report provenance.

## Safety boundary

The audited path does not store or log raw provider payloads, prompts, API keys,
tokens, PHI, or model response bodies. Performance events contain only bounded
operational metadata. The acceptance test injects a private sentinel into an
LLM timeout and proves it is absent from logs, pipeline output, and the partial
results presentation.

## Gaps and decisions

1. **Resolved in Stage 4.3:** post-report persistence time was included only in
   total analysis duration. `analysis_persistence_started` and
   `analysis_persistence_finished` now make that interval measurable without a
   schema change.
2. **Accepted existing contract:** draft reports are constructed before an
   analysis ID exists. Once persistence succeeds, `VariantReportRecord` and
   `ReportData` are rebuilt with the assigned analysis ID.
3. **Residual limitation:** explicit non-persistent runs and database-assignment
   failure retain `analysis_id=None`; their only unique correlation is the
   runtime log ID. Closing this gap inside exported reports would require an
   approved report/run-identity contract change and was not performed.
4. **Residual granularity:** batch provider-call events are provider/operation
   scoped. Per-variant attribution is carried by normalized annotation and
   evidence provenance rather than duplicated in every provider telemetry
   event.

## Performance baseline

Measurements were collected on Windows with local deterministic provider and
LLM seams. They are an offline software baseline, not a live-provider latency
claim. No optimization was performed.

### Nominal one-variant workflow

| Metric | Observed value |
| --- | ---: |
| Input processing | 0 ms |
| Variant processing | 1 ms |
| Annotation | 20 ms |
| Phenotype context | 6 ms |
| Evidence construction | 17 ms |
| LLM interpretation | 35 ms |
| Report generation | 914 ms |
| Terminal persistence | 1,135 ms |
| Total analysis | 2,317 ms |
| Provider call events | 3 |
| LLM call events | 1 |
| Variants / draft reports | 1 / 1 |

### Three-variant degraded/partial acceptance workflow

Median of three consecutive passing runs:

| Metric | Median | Observed range |
| --- | ---: | ---: |
| Input processing | 0 ms | 0-0 ms |
| Variant processing | 6 ms | 4-7 ms |
| Annotation projection | 2 ms | 2-2 ms |
| Phenotype context | 0 ms | 0-0 ms |
| Evidence construction | 41 ms | 36-45 ms |
| LLM interpretation | 73 ms | 69-85 ms |
| Report generation | 1,843 ms | 1,801-2,093 ms |
| Terminal persistence | 2,336 ms | 2,183-2,529 ms |
| Total analysis | 4,579 ms | 4,329-5,113 ms |
| LLM calls | 3 | 3-3 |
| Serialized EvidenceObjects | 38,454 bytes total | stable |
| Largest serialized EvidenceObject | 12,818 bytes | stable |

The degraded acceptance seam supplies six deterministic provider outcome
summaries but does not perform provider network calls; provider request counts
for that scenario are therefore intentionally not claimed.

### Bottlenecks

Report rendering and post-report persistence/report-record regeneration dominate
the offline profile. In the three-variant median run they account for about
91% of total wall time. This is a baseline finding only. No caching, rendering,
persistence, or pipeline optimization was introduced in Stage 4.3.

## End-to-end acceptance scenario

The deterministic scenario covers:

```text
XLSX workbook
-> explicit row selection
-> canonical preprocessing
-> annotation with VEP timeout / VariantValidator exact fallback
-> GeneBe unavailable and ClinVar no-match semantics
-> three validated EvidenceObjects
-> two successful LLM interpretations plus one bounded timeout
-> three Draft Variant Reports
-> three persisted report lifecycle records
-> partial-results presentation with 2 successful and 1 failed variant
```

Fallback provenance, limitations, the failed variant, and the available retry
action remain explicit. Valid sibling interpretations and reports are retained.

## Contract status

- PipelineResult schema: `3.4` (unchanged)
- EvidenceObject schema: `2.5` (unchanged)
- Variant interpretation schema: `1.1` (unchanged)
- Draft Variant Report schema: `2.2` (unchanged)
- ReportData schema: `4.0` (unchanged)
- Report lifecycle schema: `1.0` (unchanged)
- SQLite schema: `4` (unchanged)

## Verification record

- Stage 4.3 acceptance and persistence-failure telemetry: `2 passed`.
- Stage 4.2 UI, XLSX selection, report lifecycle, persistence recovery, and
  lifecycle observability group: `29 passed`.
- EvidenceObject, compaction, LLM preflight/boundary/diagnostics/recovery, and
  interpretation acceptance group: `92 passed`.
- Pipeline suite: `856 passed`, `4 skipped`, `1 failed`. The only failure was
  the pre-existing nested secrets-audit subprocess exceeding its fixed
  30-second timeout; the same secrets audit passed standalone.
- Full repository suite: `1548 passed`, `6 skipped`, `11 failed`. Ten failures
  are missing-file assertions caused by the 19 pre-existing user-owned deleted
  documentation paths. The remaining failure is the same nested secrets-audit
  timeout.
- Stage 60: compilation, dependency consistency, standalone secrets audit, and
  the ten-variant workflow passed. Testing V3 reached `1036 passed`, `4 skipped`,
  `2 failed`, `523 deselected`, and `81.74%` coverage; its failures were the
  nested secrets timeout and deleted `docs/stage_6a_counterfactual_audit.md`.
- Stage 78 multi-variant scenario: `1 passed`, `1564 deselected`; its subsequent
  full-suite gate reported the same `1548 passed`, `6 skipped`, `11 failed`
  baseline.

No deleted documentation file or out-of-scope test harness was restored or
modified in Stage 4.3.
