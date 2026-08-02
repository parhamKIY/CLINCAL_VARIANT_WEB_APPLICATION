# Stage 22 — Existing VEP Annotation Hardening

Status: complete  
Completed: 2026-08-02

## Scope

This stage hardened the existing Ensembl VEP adapter. It did not add a new
provider or change the main pipeline contract.

## Verified requirements

| Requirement | Result |
|---|---|
| Batch annotation | Existing bounded POST batching remains active, with a maximum batch size of 200 |
| Explicit assembly | Every annotation and VEP source record contains `GRCh37` or `GRCh38`; mismatched response assemblies are rejected per variant |
| Standard fields | Gene, gene ID, transcript, consequence, impact, HGVSc, HGVSp, canonical status, MANE Select, MANE Plus Clinical, and available predictor values are normalized |
| Reliability | HTTPS verification, timeout, bounded retries, retry backoff, HTTP checks, JSON checks, and response-shape checks remain enforced |
| Failure isolation | VEP failure produces a structured source error while MyVariant, direct ClinVar, and GenCC evidence can continue |
| LLM boundary | Raw VEP responses are discarded after standardization and cannot enter the Evidence Object or LLM prompt |
| Provenance | VEP records contain provider, explicit assembly, retrieval timestamp, and provider-version missingness |

## Normalized VEP additions

Representative transcript fields added to the top-level annotation:

```text
hgvsc
hgvsp
is_canonical
mane_select
mane_plus_clinical
predictors
```

Each cleaned transcript consequence also retains:

```text
gene_symbol
gene_id
transcript_id
biotype
consequence_terms
impact
hgvsc
hgvsp
canonical
mane_select
mane_plus_clinical
predictors
```

Predictor output is additive and bounded to VEP values that are present:

```text
sift.prediction
sift.score
polyphen.prediction
polyphen.score
```

No missing predictor is converted to zero or negative evidence.

## Provenance contract

Every `sources.vep` record now includes:

```text
status
provider
provider_version
retrieved_at
assembly
```

`provider_version` remains `null` when the per-variant REST response does not
provide a version. The application does not invent a release number.

`retrieved_at` is a UTC ISO 8601 timestamp ending in `Z`.

## Assembly behavior

- GRCh38 requests enable MANE output.
- GRCh37 requests omit the MANE parameter.
- Every response must report the same assembly configured in `config.py`.
- An assembly mismatch is retained as a per-variant VEP error and warning.

## Validation

Focused annotation regression:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_pipeline.py -k "Annotation and not AnnotationApiLogging"
```

Result:

```text
59 passed, 404 deselected
```

The focused suite verifies successful normalization, GRCh37/GRCh38 request
behavior, batching, timeout retry, exhausted retry, HTTP failure, invalid JSON,
invalid response shape, empty results, assembly mismatch, source isolation,
and provenance metadata.
