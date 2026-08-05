# Stage 28 — MyDisease.info Gene–Disease–Phenotype Context

## Decision

The active Stage 28 Monarch integration was replaced after repeated
development-environment requests returned HTTP 403. A bounded live
MyDisease.info check returned HTTP 200 and structured MONDO gene–disease
relations plus HPO disease annotations. Monarch evidence in historical
results is never relabelled; the frontend can display it only as legacy,
inactive, read-only provenance.

Phen2Gene remains the patient phenotype-to-gene provider. MyDisease.info
provides bounded gene-to-disease and disease-to-HPO context. ClinGen/GenCC
remains the gene–disease validity source. MyDisease does not overwrite any
of them.

## Verified API contract

- Base URL: `https://mydisease.info/v1`
- Query: `GET /query`
- Metadata: `GET /metadata`
- Query fields: `q`, `fields`, `size`, and `from`
- Query response: `total`, `max_score`, `took`, and `hits`
- Documented default page size: 10
- Documented maximum page size: 1,000
- Current live build metadata on 2026-08-05:
  `build_version=20260720`

Official references:

- <https://docs.mydisease.info/en/latest/doc/disease_query_service.html>
- <https://docs.mydisease.info/en/latest/doc/disease_annotation_service.html>
- <https://docs.mydisease.info/en/latest/doc/data.html>
- <https://docs.mydisease.info/en/latest/doc/response_status_codes.html>
- <https://mydisease.info/v1/api>

## Gene query and direct validation

For every unique annotated gene, Stage 28 obtains the normalized symbol and
an unambiguous HGNC CURIE from existing annotation metadata. It sends:

```text
q=mondo.synonym.exact:<GENE>*
```

Only requested fields are returned. A hit enters the primary disease list
only when:

```text
mondo.has_material_basis_in_germline_mutation_in
```

contains the exact requested HGNC identifier, including the deployed
`http://identifiers.org/hgnc/<number>` representation. Symbol text, a
definition, synonym search, or CTD inference alone never proves a direct
association. An unambiguous HGNC identifier is required because the query
finds candidate disease records while the structured relation validates
their gene identity.

## Normalized evidence

Each variant receives an independent `mydisease` object with provider status,
build version, retrieval time, query gene and HGNC identifier, sanitized query,
HTTP and result counts, bounded diseases, optional CTD context, upstream
sources, warnings, failure reason, and cache state.

Primary disease identity prefers MONDO, then DOID, OMIM, MedGen, and finally
a stable provider document identifier. Available MONDO, DOID, OMIM, MedGen,
GARD, MeSH, and UMLS cross-references are normalized without creating
synthetic identifiers.

`hpo.phenotype_related_to_disease` supplies core phenotype annotations.
Stage 28 retains bounded HPO identifiers and names, evidence, frequency,
original disease ID, OMIM/PMID references, biocuration, and HPO lineage.
Inheritance, clinical course, and clinical modifiers remain separate.

Patient terms are exact canonical HPO IDs from the existing dropdown. Exact
set intersection produces `exact_match`, `partial_match`, `no_exact_match`,
or `no_disease_hpo_annotations`. No semantic similarity or ontology traversal
is performed. No match and no annotation are missing context, never negative
phenotype evidence.

CTD pathway records are retained only as
`association_type=inferred_pathway_context` with
`classification_effect=context_only`. They do not enter the direct disease
list and cannot affect pathogenicity or ACMG evidence.

MyDisease.info is recorded as provider while MONDO, HPO, Disease Ontology,
CTD, and UMLS remain upstream sources. Aggregated sources are not treated as
independent votes.

## Bounds, cache, retries, and missingness

Configuration flows from `.env` through `config.py`:

- `MYDISEASE_BASE_URL`
- `MYDISEASE_TIMEOUT`
- `MYDISEASE_MAX_RETRIES`
- `MYDISEASE_CACHE_SIZE`
- `MYDISEASE_MAX_DISEASES_PER_GENE`
- `MYDISEASE_MAX_HPO_TERMS_PER_DISEASE`

Duplicate genes are queried once per analysis. The process-local LRU cache is
bounded and keyed by normalized symbol, HGNC CURIE, provider URL, requested
fields, evidence limits, and schema context. Requests use a timeout,
exponential backoff, and retries only for retryable transport/status failures.
Failures are isolated per gene.

Statuses:

- `available`: one or more validated direct diseases
- `partial`: mixed outcomes across variants
- `no_association`: valid response, no validated direct disease
- `unsupported`: no usable gene/HGNC query model
- `unavailable`: timeout, network, or HTTP failure
- `invalid_response`: HTTP 200 with invalid JSON or schema

An empty response is not benign evidence and is not a failure. Provider search
`_score` is discarded and never becomes a clinical score or ranking signal.
Raw provider responses are discarded after normalization and do not enter the
Evidence Object, LLM prompt, report, or database.

## Verification

Mocked tests cover direct relation validation, identifier and HPO
normalization, exact matching states, CTD/text-only rejection, empty results,
unsupported genes, transport and schema failures, retry behavior,
deduplication, caching, bounds, input-order preservation, classification
isolation, Evidence Object exclusion, UI status semantics, and legacy
Monarch display.

The separate live diagnostic is:

```powershell
.\.venv\Scripts\python.exe tests\manual_mydisease_diagnostic.py
```

It queries SCN1A (`HGNC:10585`) with patient HPO `HP:0001250` and prints only
sanitized counts, normalized identifiers, provider status, and persistence
checks. It never prints the raw provider body.
