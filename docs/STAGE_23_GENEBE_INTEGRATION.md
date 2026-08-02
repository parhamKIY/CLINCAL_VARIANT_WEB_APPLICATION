# Stage 23 — GeneBe Integration

Status: complete  
Completed: 2026-08-02

## Scope

GeneBe is an independent annotation provider beside Ensembl VEP. It supplies
supplementary consequence, automated ACMG, population, predictor, and
ClinVar-derived evidence.

GeneBe output does not overwrite VEP output and is not treated as final
clinical truth.

## Official API contract

The adapter follows the documented GeneBe batch endpoint:

```text
POST /api-public/v1/variants
```

Documentation:

- <https://docs.genebe.net/docs/api/overview/>
- <https://docs.genebe.net/docs/api/variant-description-fields/>

Requests use documented CPRA objects:

```json
{
  "chr": "7",
  "pos": 140753336,
  "ref": "A",
  "alt": "T"
}
```

The configured assembly is mapped explicitly:

```text
GRCh37 → hg19
GRCh38 → hg38
```

## Configuration

The central configuration supports:

```text
GENEBE_BASE_URL
GENEBE_EMAIL
GENEBE_API_KEY
```

Unauthenticated access is supported by the provider. If authentication is
configured, email and API key must both be present and are sent through HTTP
Basic authentication. Credentials are never placed in URLs, logs, annotations,
or reports.

## Request behavior

- Uses the documented batch POST endpoint.
- Supports up to 1,000 variants per GeneBe batch.
- Requests RefSeq and Ensembl consequences.
- Requests automated ACMG, basic, and advanced annotations.
- Keeps GeneBe normalization enabled.
- Uses HTTPS certificate verification.
- Uses the central request timeout.
- Uses bounded retries and exponential backoff.
- Validates HTTP status, JSON structure, item types, and response count.

## Normalized evidence

Every annotation contains a separate `sources.genebe` object with:

```text
status
provider
provider_version
retrieved_at
request_assembly
returned_variant
representation_mismatch
transcript_mismatch
gene
gene_hgnc_id
transcript
effect
consequences
total_consequences
consequences_truncated
automated_acmg_classification
automated_acmg_criteria
automated_acmg_score
population_annotations
predictor_annotations
clinvar_derived
```

The automated classification field is deliberately named
`automated_acmg_classification`. It is provider evidence, not the final
interpretation.

ClinVar-derived fields retain:

```text
upstream_source = ClinVar
```

They remain separate from the direct NCBI ClinVar source.

## Source independence

GeneBe never overwrites these VEP-selected fields:

```text
gene
gene_id
transcript
consequence
hgvsc
hgvsp
impact
canonical and MANE metadata
```

Different returned coordinates or alleles are retained under
`returned_variant` and marked with `representation_mismatch`.

A different GeneBe and VEP transcript is marked with
`transcript_mismatch`.

## Failure behavior

GeneBe service failures produce:

```text
sources.genebe.status = unavailable
```

Malformed successful responses produce:

```text
sources.genebe.status = invalid_response
```

The warning is bounded and visible. VEP, MyVariant.info, direct ClinVar, and
GenCC continue independently.

No missing GeneBe result is interpreted as benign or as negative evidence.

## Downstream boundary

GeneBe is visible in pipeline annotation output and in the frontend annotation
table. Evidence Object `1.0` remains unchanged to prevent an unplanned schema
migration. GeneBe enters the bounded LLM evidence contract during the planned
Evidence Object V2 stage.

Raw GeneBe responses are discarded after standardization.

## Validation

Complete offline acceptance:

```powershell
.\.venv\Scripts\python.exe tests\run_stage16_final_acceptance.py
```

Result:

```text
472 passed, 3 skipped
coverage 88.27%
Stage 16 MVP release acceptance: PASSED
```

Tests cover:

- successful batch request and normalized output;
- optional Basic authentication;
- hg19/hg38 request mapping;
- representation and transcript mismatch metadata;
- automated ACMG, population, predictor, and ClinVar-derived fields;
- timeout and retry;
- HTTP failure;
- invalid JSON, invalid structure, invalid variant, and response-count mismatch;
- VEP non-overwrite;
- continued operation of every other annotation source;
- safe API telemetry without credentials or variant content.

## Live-provider observation

A live anonymous batch request from the development environment timed out
after 30 seconds on 2026-08-02. No live response was used as acceptance
evidence. The implementation is verified offline against the official contract,
including the exact timeout and unavailable-source path.
