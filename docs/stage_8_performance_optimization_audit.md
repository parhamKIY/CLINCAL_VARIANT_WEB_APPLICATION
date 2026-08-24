# Stage 8 Performance Optimization Audit

## Scope and method

This audit used deterministic, non-PHI, offline fixtures on Windows. Provider and
LLM network time was replaced with the existing validated fakes so application
work could be measured independently of external latency. The same commands and
fixtures were run before and after the changes.

Measurements used pipeline stage observability, `cProfile`, `perf_counter`,
`tracemalloc`, SQLite `EXPLAIN QUERY PLAN`, and synthetic repository records.
Wall-clock values are a local baseline, not a service-level guarantee.

## Proven bottlenecks

1. DOCX placeholder replacement repeatedly evaluated `run.text` and
   `paragraph.text` once per placeholder. The three-variant profile recorded
   130,488 XML XPath calls; `_replace_paragraph_text` consumed 2.621 seconds of
   3.006 seconds spent rendering six lifecycle DOCX artifacts.
2. Every repository operation opened one connection to initialize/validate the
   repository and a second connection for the operation. Three hundred exact
   one-record lookups took 688.91 ms (2.296 ms per lookup).
3. The repository index did not include `query_identity_hash`, `stored_at`, or
   `record_id`. Exact provider/query lookups scanned the matching provider
   history and used a temporary B-tree for ordering. With 1,000 snapshots for
   one allele/provider and distinct query identities, 300 lookups took 864.9 ms
   (2.883 ms per lookup).
4. Input validation, variant processing, annotation orchestration, evidence
   construction, and LLM boundary processing were small in the deterministic
   profile. Provider network latency and model latency remain externally
   dominated and were not candidates for semantic changes.

## Implemented optimizations

- DOCX replacement now reads each XML run once per paragraph scan and reuses one
  paragraph text projection. Split-run fallback behavior and rendered output are
  unchanged.
- Repository initialization is serialized once per repository instance.
  Subsequent operations use one connection while still checking the schema
  version, complete table shape, and exact index shape before every operation.
- Repository schema 2 replaces the original lookup index with
  `evidence_repository_lookup_v2`:

  ```text
  canonical_digest,
  semantic_node,
  provider,
  query_identity_hash,
  retrieved_at,
  stored_at,
  record_id
  ```

- Schema 1 repositories migrate transactionally to schema 2. Repository record
  schema `1.0`, payload integrity hashes, immutable history, provenance, and the
  clinical database schema are unchanged.

## Before/after benchmark

| Scenario | Before | After | Change |
|---|---:|---:|---:|
| Three-variant report stage | 1,231 ms | 199 ms | 83.8% faster |
| Three-variant persistence phase | 2,761 ms | 304 ms | 89.0% faster |
| Exact repository hit, 300 calls | 688.91 ms | 406.84 ms | 40.9% faster |
| Hot-history exact query, per lookup | 2.883 ms | 1.477 ms | 48.8% faster |
| One-variant pipeline with memory tracing | 6,351.5 ms | 4,265.5 ms | 32.8% faster |
| Five-variant pipeline with memory tracing | 14,973.6 ms | 8,149.5 ms | 45.6% faster |

The three-variant result retained the same 3 EvidenceObjects, 3 LLM calls, 3
draft reports, 38,454 total serialized EvidenceObject bytes, and 12,818 maximum
serialized bytes.

The optimized query plan uses
`evidence_repository_lookup_v2 (canonical_digest, semantic_node, provider,
query_identity_hash)` and no longer creates a temporary sort B-tree for the exact
provider/query lookup.

## Repository scale probe

A one-million-row synthetic, non-PHI SQLite probe inserted decoy index rows in a
bounded local transaction and retained one fully valid target record created and
verified through `EvidenceRepository`.

- rows: 1,000,001
- database size: 853,344,256 bytes
- 1,000 exact verified target lookups: 1,643.1 ms total
- exact lookup mean: 1.643 ms
- synthetic bulk population time: 8.714 seconds

The direct synthetic population step intentionally measured SQLite index scale;
it did not represent or bypass the production repository write boundary. Normal
provider snapshots still use bounded transactions and full validation.

SQLite remains appropriate for the current single-user/research deployment and
the measured exact-identity lookup pattern. A storage-engine migration is not
justified by this result.

## Memory and resource stability

Peak traced Python memory remained effectively unchanged:

| Scenario | Before | After |
|---|---:|---:|
| One variant | 3,184,747 bytes | 3,187,683 bytes |
| Five variants | 6,151,539 bytes | 6,151,424 bytes |

Focused acceptance covers same-process analysis isolation, bounded one-to-five
variant memory, thread-safe concurrent idempotent repository writes, one
connection per hot lookup, and fail-closed schema/integrity checks. No shared
clinical or variant state was observed across repeated analyses.

## Safety verification

- EvidenceObject and interpretation schemas are unchanged.
- Provider routing, fallback order, timeout semantics, validation, and provenance
  are unchanged.
- Repository hits still undergo payload SHA-256 verification, schema validation,
  freshness evaluation, and provider-result semantic validation.
- Operational failures remain uncacheable and valid `no_match` remains distinct.
- DOCX golden/fidelity tests verify deterministic rendering and artifact hashes.
- No PHI, prompts, provider payloads, or clinical narratives are emitted by the
  benchmark tests.

## Remaining limits

- Live provider and LLM latency varies by network, quota, and upstream service and
  was intentionally not optimized in this stage.
- Repository storage grows with immutable provider history; maintenance reports
  stale/invalid state but does not delete valid historical provenance.
- The scale probe validates exact indexed lookup, not simultaneous multi-process
  write saturation. SQLite's bounded busy timeout and WAL mode remain the current
  concurrency boundary.
- Normal provider snapshot writes remain approximately milliseconds per record;
  batching was not introduced because provider results arrive incrementally and
  bounded immutable writes preserve the existing failure-isolation contract.
