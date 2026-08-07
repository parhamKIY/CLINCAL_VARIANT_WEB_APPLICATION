# Stage 32 — Conditional Evidence Enrichment

Status: complete and verified

Date: 2026-08-07

## Scope

Stage 32 enriches only variants with deterministic triggers: meaningful
conflict, VUS, insufficient evidence, ambiguous population evidence, or an
explicit literature request. It never ranks variants or changes a clinical
classification.

## Population evidence

- The active adapter queries the gnomAD GraphQL API directly.
- Queries use the exact normalized `chrom-pos-ref-alt` identity and the
  assembly-specific dataset (`gnomad_r2_1` or `gnomad_r4`).
- Response `variant_id`, REF, and ALT must exactly match the request before
  any frequency is accepted.
- Joint, exome, genome, ancestry frequency, allele-count, allele-number,
  homozygote, and hemizygote values are bounded and standardized.
- A missing gnomAD record is missingness, not benign evidence.
- MyVariant.info may be retained only as a non-independent fallback.
- The previous Ensembl Variation adapter remains compatibility-only and now
  also requires exact assembly, coordinate, REF, and ALT matching.

## Literature evidence

- LitVar2 is queried first.
- Europe PMC and PubMed are bounded fallbacks for provider failure or invalid
  responses.
- Articles are deduplicated by PMID, PMCID, or DOI.
- Raw provider payloads and abstracts are not copied into Evidence Object V2.

## Reliability and provenance

- Requests use bounded timeout, limited retry, HTTPS verification, and
  explicit provider statuses.
- gnomAD requests observe the documented public API rate limit.
- Provider, dataset, retrieval time, upstream source, query identity, and
  exact missingness/failure state are retained.
- One provider failure does not remove other evidence or stop other variants.

## Verification

- Focused Stage 32 and Stage 34 suite: passed.
- Full regression suite: passed.
- Live gnomAD and LitVar2 diagnostic: passed on 2026-08-07.
- The live gnomAD check verifies the exact query identity and confirms that
  every retained population row belongs to the requested ALT allele.

## Completion condition

Conditional enrichment runs only for triggered variants, completes before
editable Output A is built, retains exact-allele bounded evidence, and never
changes input order or clinical classification.
