# Stage 24 — Direct ClinVar Integration

Status: complete  
Completed: 2026-08-02

## Scope

NCBI ClinVar is an independent direct source of clinical classification
evidence. It is not populated from MyVariant.info, GeneBe, or another
aggregator.

Direct ClinVar evidence remains separate from GeneBe automated ACMG evidence
and from GeneBe fields whose upstream source is ClinVar. No provider can
overwrite another provider's classification.

## Direct query contract

The adapter uses NCBI E-utilities directly:

```text
GET /esearch.fcgi
GET /esummary.fcgi
```

For each supplied variant it:

1. normalizes the edit representation;
2. builds an assembly-specific RefSeq genomic HGVS identifier;
3. searches ClinVar's variant-name field;
4. requests bounded ESummary 2.0 records;
5. requires exactly one matching assembly, chromosome, position, REF, and ALT.

GRCh38 exactness also requires an exact canonical SPDI match. GRCh37 uses the
exact assembly-specific HGVS query plus exact assembly and coordinate
validation because ClinVar reports canonical SPDI on GRCh38.

NCBI traffic uses TLS verification, the central timeout, bounded retries,
exponential backoff, and a request interval below three requests per second.

## Standardized direct evidence

Every annotation contains a separate `sources.clinvar` object with:

```text
status
direct_verification_status
provider
provider_version
api
api_version
source_type
retrieved_at
assembly
query_hgvs
variation_id
accession
accession_version
gene
clinical_significance
review_status
last_evaluated
conditions
condition_count
conditions_truncated
scv_accessions
scv_accession_count
scv_accessions_truncated
rcv_accessions
rcv_accession_count
rcv_accessions_truncated
conflicting_submissions
```

Condition records retain bounded names and relevant external identifiers such
as MedGen, MONDO, OMIM, Orphanet, and MeSH identifiers when NCBI supplies
them. Raw responses are discarded after standardization.

The provenance fields record that this is a direct NCBI ClinVar query, the
NCBI E-utilities interface and ESummary version, retrieval time, requested
assembly, and exact HGVS query. ClinVar does not expose a data-release version
in this response, so `provider_version` remains `null` rather than being
invented.

## Conflict representation

`conflicting_submissions` is deterministic and uses only ClinVar's aggregate
germline review-status wording:

```text
explicit "conflicting" status -> conflicting / true
explicit "no conflicts" status -> no_conflict / false
otherwise -> unknown / null
```

An expert-panel or practice-guideline aggregate can take precedence over
lower-level submissions. Therefore, the adapter does not infer that conflict
is absent merely because the aggregate classification has a strong review
status.

The conflict object records its basis as `aggregate_review_status`. When
conflict is explicit, the aggregate clinical-significance text is retained as
bounded details. Resolution of disagreements remains outside Stage 24.

## Missingness and failure behavior

The direct verification states are:

```text
verified
no_record
unsupported
unavailable
invalid_response
```

`no_record` retains `clinical_significance = null`. It is not mapped to
Benign, Likely benign, or negative evidence.

Network, timeout, API, and HTTP failures produce `unavailable`. Malformed,
ambiguous, or non-exact successful responses produce `invalid_response`.
Both are retried automatically within the configured bounded retry budget.
Other providers continue independently.

## Validation

Offline tests cover:

- exact direct ESearch and ESummary requests;
- GRCh37 and GRCh38 identifiers;
- exact response matching;
- direct provenance;
- VCV, RCV, SCV, gene, condition, review, and evaluation fields;
- explicit conflict and no-conflict states;
- independent GeneBe and ClinVar classifications;
- no-record missingness without a benign inference;
- timeout, HTTP, malformed-response, and non-exact-response isolation;
- automatic retry behavior.

Official NCBI references:

- <https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/>
- <https://www.ncbi.nlm.nih.gov/clinvar/docs/review_status/>
- <https://www.ncbi.nlm.nih.gov/clinvar/docs/variation_report/>

Complete repository acceptance:

```text
478 passed, 3 skipped
coverage 88.61%
Stage 16 MVP release acceptance: PASSED
```

## Live-provider observation

A direct production-path lookup on 2026-08-02 verified the public GRCh38 demo
variant `1:941284:G:A` using
`NC_000001.11:g.941284G>A`. NCBI returned `VCV001006858.12`, an aggregate
germline significance of `Conflicting classifications of pathogenicity`, and
the review status `criteria provided, conflicting classifications`.

No live response was retained in application storage or used as mocked test
evidence.
