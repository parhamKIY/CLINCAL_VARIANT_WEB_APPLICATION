# Stage 25 — ClinGen and CSpec Upgrade

Status: complete  
Completed: 2026-08-02

## Scope

Stage 25 separates two related but scientifically different ClinGen resources:

1. ClinGen-submitted GenCC claims provide gene-disease validity context.
2. The ClinGen Criteria Specification Registry provides VCEP-authored,
   gene- and disease-specific ACMG/AMP specification metadata.

Neither source directly changes the application's pathogenicity
classification. Both are retained as auditable clinical context.

## Gene-disease validity context

The existing UCSC GenCC integration remains the transport for exact
ClinGen-submitted gene-disease validity claims. Its standardized
`sources.clingen` object now records:

```text
status
provider
provider_version
data_provider
retrieved_at
assembly
query_region
query_gene
gene
gene_id
context_type
classification_effect
curations
curation_count
curations_truncated
```

`query_region` records the UCSC assembly and the exact zero-based, half-open
interval sent to the provider. Each curation retains the disease and MONDO
identifier, validity classification, mode of inheritance, classification
date, ClinGen submitter, criteria URL, submission identifier, PMIDs, and
public report URL.

The source declares:

```text
context_type = gene_disease_validity
classification_effect = context_only
```

## CSpec API contract

The application queries the public ClinGen CSpec Registry directly:

```text
GET /Gene/id/{gene}
GET /Disease/id/{MONDO_ID}
```

The exact gene symbol comes from the standardized annotation. Bounded MONDO
disease context is collected from direct ClinVar conditions and exact
ClinGen/GenCC curations.

The application intersects linked
`SequenceVariantInterpretation` identifiers from the gene and disease
entities. Only specifications whose explicitly current workflow state is
`Released` are retained.

The default central configuration is:

```text
CSPEC_BASE_URL=https://cspec.clinicalgenome.org/cspec
```

Requests use TLS verification, the central timeout, bounded retries, and
exponential backoff.

## CSpec evidence contract

Every annotation contains a separate `sources.cspec` object:

```text
status
provider
provider_version
retrieved_at
query_gene
query_disease_ids
disease_queries_truncated
specification_available
specifications
specification_count
specifications_truncated
context_type
classification_effect
rule_logic_applied
```

Each released specification retains only bounded metadata:

```text
specification_id
title
short_title
version
status
vcep
approved_at
modified_at
source_document_url
specification_url
concept_doi
document_doi
matched_disease_ids
scope_match
applicable_to_disease_context
```

The scope values are:

```text
gene_and_disease
gene_only
```

When MONDO context is available, `applicable_to_disease_context` is `true`
only for an exact linked CSpec identifier match. It is `false` for an
explicit disease mismatch and `null` when no structured disease context is
available.

No raw CSpec payload, criteria-code text, thresholds, or provider rule engine
output is retained.

## Classification boundary

The stable source contract is:

```text
context_type = gene_disease_acmg_specification
classification_effect = context_only
rule_logic_applied = false
```

Stage 25 does not apply ACMG/AMP criteria, alter GeneBe automated
classification, alter direct ClinVar significance, or create a final
classification.

The current Evidence Object `1.0` and LLM payload remain unchanged. This
prevents the LLM from inventing or applying CSpec rules. CSpec metadata will
enter the bounded downstream evidence contract only during the planned
Evidence Object V2 stage.

## Missingness and failure behavior

The CSpec states are:

```text
success
not_found
not_applicable
unavailable
invalid_response
```

`not_found` means no released CSpec specification was linked to the exact
gene. It is valid missingness, not negative evidence, and does not make an
otherwise successful analysis partial.

Network, timeout, API, and HTTP failures produce `unavailable`. Malformed or
non-matching successful responses produce `invalid_response`. Failed
variant-provider attempts are retried automatically within the configured
budget, while all other provider results remain available.

## Frontend

The live external-API panel includes `ClinGen CSpec Registry` as an
independent provider. The annotation table shows CSpec status and released
specification count. No scientific rule logic runs in Streamlit.

## Validation

Tests cover:

- direct CSpec gene and MONDO disease queries;
- exact entity identity validation;
- released-version filtering;
- VCEP, version, DOI, approval, modification, and source metadata;
- exact gene-and-disease matching;
- explicit gene-only and disease-mismatch context;
- valid no-specification missingness;
- invalid JSON, malformed structure, and non-matching entity isolation;
- timeout and automatic retry;
- absence of rule application and criteria payloads;
- enriched ClinGen/GenCC provenance;
- CSpec status in the live Streamlit API panel.

Final acceptance passed on 2026-08-02:

```text
488 passed, 3 skipped
88.52% total coverage
Stage 16 MVP release acceptance: PASSED
```

A live CSpec contract check for `TP53` and `MONDO:0018875` returned released
specification `GN009`, version `2.4`, with `gene_and_disease` scope. No raw
provider response was retained.

Official references:

- <https://www.clinicalgenome.org/working-groups/sequence-variant-interpretation/>
- <https://genboree.org/gitlab/clingen/cspec/cspec-registry/-/wikis/CSpec-Registry-API>
- <https://cspec.clinicalgenome.org/cspec/srvc>
