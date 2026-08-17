# Selected-Variant Input Stabilization — Stage 2C

## Reference identity resilience

The input adapter uses one internal reference request contract:
`(GRCh38, chromosome, start_1based_inclusive, end_1based_inclusive)`.
It requests Ensembl sequence-region data first. A usable Ensembl sequence ends the
request. UCSC hg38 `getData/sequence` is invoked only after an operational or
response-validity failure, never after a usable Ensembl sequence contradicts a source
allele.

The provider adapters, not Stage 2B normalization, own transport details:

- Ensembl receives the unchanged inclusive region string.
- UCSC receives `chrom=chr<chromosome>`, `start=internal_start-1`, and
  `end=internal_end`; UCSC documents sequence retrieval start as zero-relative and
  end as one-relative.

Official contracts: [Ensembl sequence-region](https://rest.ensembl.org/documentation/info/sequence_region)
and [UCSC getData/sequence](https://genome.ucsc.edu/goldenPath/help/api.html).

## Safety policy

The two public endpoints are redundant routes to the same GRCh38 reference identity.
They are not evidence sources, independent biological support, Evidence Graph nodes,
coverage capabilities, or LLM context.

- Valid primary sequence: use it; do not call UCSC.
- Primary operational failure or malformed/length-invalid response: use bounded UCSC
  fallback.
- Valid sequence with source `REF` mismatch: `REFERENCE_MISMATCH`; do not seek a
  second source to override it.
- Both routes unavailable: `REFERENCE_LOOKUP_UNAVAILABLE`.
- Invalid reference response: `REFERENCE_RESPONSE_INVALID`.
- A deterministic lower-level comparison of conflicting valid responses fails closed
  as `REFERENCE_SOURCE_CONFLICT`; production does not double-query to detect it.

Reference responses retain provider, bounded attempt count, coordinate-request
provenance, fallback flag, and safe status without raw HTTP bodies. Input provenance
retains only a reviewer-safe route/verification summary. The analysis-scoped cache is
keyed by assembly, normalized chromosome, inclusive start, and inclusive end; it is
not persisted as evidence.

## Canonical handoff

Stage 2B deletion and insertion transformations are unchanged. A successful fallback
sequence can establish the same canonical CPRA as primary Ensembl sequence. Only that
canonical allele reaches downstream providers; unresolved inputs remain selected-input
records with no annotation, Evidence Object, or LLM interpretation.

Schemas remain PipelineResult `3.3`, SQLite `4`, recovery request `3`, and
EvidenceObject `2.5`.
