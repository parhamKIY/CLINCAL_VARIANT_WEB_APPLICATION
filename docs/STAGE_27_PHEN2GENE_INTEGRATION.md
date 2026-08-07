# Stage 27 — Phen2Gene Integration

Status: complete

Date: 2026-08-03

## Scope

Stage 27 adds Phen2Gene as an independent phenotype-to-gene evidence
provider. It does not rank variants, select a top variant, change
pathogenicity, or apply an ACMG/AMP criterion.

The canonical HPO set selected in the existing searchable dropdown is sent to
Phen2Gene once per analysis. The response is then matched to the annotated
gene of each input variant while preserving the original variant order.

## Provider contract

- Endpoint: `GET https://phen2gene.wglab.org/api`
- Required parameter: `HPO_list`, using semicolon-separated HPO identifiers
- Weight model: `sk`
- Authentication: none
- Central configuration: `PHEN2GENE_BASE_URL`
- Timeout: `REQUEST_TIMEOUT`
- Retry budget: the existing bounded `ANNOTATION_MAX_RETRIES` default

The implementation follows the official provider documentation:

- <https://phen2gene.wglab.org/docs>
- <https://github.com/WGLab/Phen2Gene2>

## Normalized evidence

Each variant receives a bounded `phen2gene` object containing:

- `availability`: `available`, `partial`, or `unavailable`
- `gene`
- `gene_id`
- `rank`
- `score`
- `status`
- canonical `hpo_terms`
- `weight_model`
- `provider`
- nullable `provider_version`
- `retrieved_at`
- `cache_hit`
- bounded `warnings`

The provider currently returns `Gene ID` as either a numeric identifier or a
symbolic identifier, so both documented and live response shapes are
validated without inventing an identifier.

No raw Phen2Gene response is retained in the pipeline result, report, cache,
or LLM payload.

## Missingness rules

- No Phen2Gene hit does not mean the phenotype is unrelated.
- A missing annotated gene produces `availability=unavailable` without a
  provider request.
- A successful provider response without the requested gene produces
  `availability=partial`.
- Exhausted transport or response failures produce
  `availability=unavailable`.
- Local HPO evidence and every annotation remain available when Phen2Gene
  fails.

## Resilience and cache

- Requests use HTTPS, bounded timeouts, limited retries, exponential backoff,
  and numeric `Retry-After` handling.
- Only one request is made for the analysis HPO set per attempt.
- The in-process cache is bounded to 32 entries with a one-hour TTL.
- Cache keys are SHA-256 digests over provider context, the HPO set, and the
  requested genes; HPO identifiers are not stored in cache keys.
- Cached values contain only normalized evidence for genes in the current
  analysis.
- Logs contain provider, operation, attempt, outcome, duration, HTTP status,
  and timeout only; they exclude HPO identifiers, genes, URLs, and payloads.

## Pipeline and frontend

- Pipeline schema version is `1.4`.
- `phen2gene` is part of the stable external API status order.
- No HPO input marks the provider as skipped.
- Available evidence maps to API status `success`.
- Partial evidence maps to API status `warning`.
- Provider unavailability maps to API status `error` while the phenotype
  stage remains recoverable.
- The Streamlit phenotype table displays availability, score, provider rank,
  and gene status.
- The UI explicitly labels provider rank as service metadata and states that
  it does not reorder variants or alter pathogenicity.

## Validation

Offline regression excluding the pre-existing unreadable-document audit:

```text
497 passed, 3 skipped, 1 deselected
Total coverage: 92.08%
Coverage gate: passed (minimum 80%)
```

The complete unfiltered suite has one pre-existing failure because nine
tracked Stage 15–25 documentation files are deleted in the current worktree.
Stage 27 did not restore or modify those user-owned deletions.

Live provider verification:

```text
HPO set: HP:0001250
Annotated gene: SCN1A
Availability: available
Rank: 235
Score: 0.378871
Status: SeedGene
Provider version: null
```

The live response was accepted without using it as deterministic automated
test evidence.

## Stage 27 acceptance

- HPO terms are sent once per analysis: passed.
- Gene, rank, score, and status are normalized: passed.
- Every variant receives Phen2Gene evidence or explicit missingness: passed.
- Variant input order is preserved: passed.
- Provider rank remains metadata only: passed.
- Timeout, retry, cache, and failure isolation are implemented: passed.
- Provider and retrieval provenance are recorded without inventing a version:
  passed.
