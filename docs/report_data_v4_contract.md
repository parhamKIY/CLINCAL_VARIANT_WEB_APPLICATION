# ReportData V4 Contract

**Stage:** 81 only  
**Schema version:** `4.0`  
**Implementation:** `backend/report_data.py`  
**Presentation source:** `docs/professor_report_template_spec.md`

## Purpose

`ReportData V4` is the renderer-neutral source contract for one allele-level report.
Future DOCX and in-app preview renderers must consume the same validated record. The
contract contains typed facts and review state, not table markup, HTML, Markdown, or
LLM-formatted evidence.

Stage 81 defines and validates the contract only. It does not create the DOCX
template, render reports, migrate persistence, replace Draft Variant Report V2, or
change the Streamlit workflow.

## Top-level contract

Every record contains exactly:

```text
schema_version
report_id
analysis_id
input_index
variant_identity
phenotype_summary
conclusive_result
main_findings
interpretation
classification_summary
literature_references
data_sources
warnings
provenance
review_state
template_version
```

`input_index` is the zero-based internal index from accepted input order. A renderer
may display `input_index + 1`, but must never use gene identity to order or merge
reports.

Fields whose values are genuinely unavailable remain present with `null`, an empty
typed list, or an explicit availability state. They are not fabricated and do not
invalidate the whole report.

## Typed sections

### Variant identity

The stable identity is genome build, chromosome, position, reference, and alternate.
Gene, gene ID, transcript, coding HGVS, protein HGVS, and zygosity are nullable display
annotations. Zygosity is accepted only when explicitly supported and is never inferred
from stripped genotype data.

### Phenotype summary

Accepted and matched HPO terms are typed ID/label pairs. The exact concordance enum is:

```text
supported
partially_supported
no_supported_association
unavailable
not_assessed
```

Positive phenotype association is never required. Score and narrative are nullable.

### Conclusive result

The result block stores nullable gene/HGVS/zygosity plus a classification and its
source. An `available` classification requires both value and source. Missing,
conflicting, or unsupported context uses an explicit non-available state rather than
an unattributed label.

### Main findings

The evidence-table model contains typed lists for:

- population frequencies;
- disease associations and inheritance context;
- computational evidence;
- stable variant identifiers;
- source-attributed classifications.

Each entry carries its own source and availability. Population fallback records must
identify the fallback role. Available findings require the value needed by their
type. Preformatted table text and unknown fields are rejected.

### Interpretation

The contract stores:

```text
original_model_interpretation
current_reviewer_interpretation
model
prompt_version
generated_at
edit_history
interpretation_status
failure_type
conflict_assessment
```

Edit history is contiguous and replayable from the original model text to the current
reviewer text. An unavailable interpretation is valid and does not remove evidence or
the report.

### Classification summary

Reviewer-confirmed, ClinVar, and automated classifications remain separate. Conflict
state, source attribution, and the independent-ACMG-adjudication flag prevent an
unattributed consensus. CSpec remains contextual and must appear through source
attribution/provenance, not as executed classification rules.

### Literature and data sources

`literature_references` accepts canonical PubMed, PMC, or DOI records only. ClinVar,
Ensembl, GeneBe, MyVariant.info, ClinGen/CSpec, phenotype, disease, population, and
other provider records belong in `data_sources`.

Each data-source record keeps capability, availability, operational status, provider
role, method, record/dataset, human-link state, fallback use, primary failure, and
retrieval time. A successful fallback requires both fallback role and preserved
primary failure.

### Warnings, provenance, and review

Warnings use `INFO`, `PARTIAL`, `ACTION_REQUIRED`, or `BLOCKING`. Provenance retains
source schema versions, providers, upstream sources, and generation time. Review state
stores draft/confirmed status, reporting-only inclusion, confirmation time, reviewer
summary/notes, and replayable selection history.

## Core validation invariants

- Exact field sets reject schema drift and embedded presentation markup.
- All text, collections, alleles, histories, and serialized output are bounded.
- Timestamps are timezone-aware ISO-8601 values.
- Numeric values are finite; frequencies and phenotype scores are within `[0, 1]`.
- Report content passes the established privacy-field and labelled-identifier gate.
- Literature reference IDs are unique and restricted to literature identifier types.
- Validated human URLs use the existing allowlisted URL boundary.
- Interpretation and selection histories replay to their persisted current states.
- The serialized record is JSON-safe and no larger than 256 KiB.

## Stage 81 acceptance evidence

`tests/test_report_data.py` proves that both a rich report and a sparse report with
missing gene/transcript/HGVS/zygosity, population no-match, phenotype
non-concordance, and unavailable interpretation validate successfully. It also proves
rejection of preformatted table fields, invalid audit replay, non-literature entries
in References, inconsistent fallback provenance, prohibited identity fields, and
selection-history mismatch.
