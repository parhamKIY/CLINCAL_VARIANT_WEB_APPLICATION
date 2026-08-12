# POST-ACCEPTANCE FIX ROADMAP

**Project:** Clinical Variant Interpretation  
**Roadmap type:** Acceptance-driven corrective roadmap  
**Primary objective:** Turn the current developer-facing workflow into a report-first clinical review workflow  
**Authoritative visual reference:** `TS-Final Report(2).pdf` provided by the professor  
**Previous milestone:** Provider resilience completed through Stage 78  
**This roadmap begins at:** Stage 79

---

# 0. Executive priority

This roadmap follows a Pareto-style product priority:

> The highest-value correction is the report itself.

The most important acceptance failure is not a minor UI issue. The current application exposes analysis content as generic web controls, cards, warnings, and evidence blocks, while the intended product is a **clinical-report-first workflow**.

The professor-provided report therefore becomes the **authoritative visual and structural reference** for the user-facing per-variant draft report.

If the system can reliably generate a high-fidelity, editable, Word-based per-variant report that looks and reads like the professor reference, most of the perceived product gap is closed.

Priority order:

```text
P0  High-fidelity per-variant clinical report
P1  Reliable interpretation generation
P1  Correct, human-appropriate references
P2  Clean per-variant status UX
P2  Warning semantics and degraded-mode clarity
```

Do not spend major effort polishing provider status panels before the report experience is correct.

---

# 1. Acceptance failures captured by this roadmap

## Issue 1 — Draft report UX and format mismatch

### Observed

The current application exposes report content through generic Streamlit-style cards, text areas, evidence blocks, and references.

### Required

For **every input variant**, produce a **separate clinical variant report** whose layout, visual hierarchy, tables, typography, and narrative organization are based directly on the professor report.

The report should feel like a laboratory Word report, not like a web form.

The report must be editable by the reviewer.

### Authoritative structural cues

The professor report contains the following important report patterns:

```text
NGS Result Report
Clinical Features
Conclusive Result(s)
Brief Interpretation(s)
Main Finding(s) in Detail
    structured evidence table
Variant interpretation
    long-form narrative
Variant(s) classification
Method / Comments
References
```

For this project, the **visual structure is authoritative**, while out-of-scope clinical content remains excluded.

---

## Issue 2 — Variant interpretation reliability

### Observed

Some variants show:

```text
Interpretation is unavailable because the model request failed
(VariantInterpretationError)
```

### Required

Interpretation is a core deliverable and should not disappear after a single opaque failure.

The system must distinguish:

```text
API request failure
rate limit
timeout
provider error
invalid structured output
schema validation failure
empty model response
safety/finish condition
internal parsing failure
```

Transient and repairable failures need bounded recovery.

---

## Issue 2A — Phenotype mismatch must be valid

### Observed test

An intentionally irrelevant phenotype such as abdominal pain was supplied.

### Required

Irrelevant phenotype must **not** invalidate interpretation.

A valid interpretation may conclude:

```text
No supported phenotype association was identified between the
provided phenotype and the current gene/variant evidence.
```

Phenotype concordance is one evidence dimension, not a prerequisite for interpreting the variant.

---

## Issue 3 — Reference architecture is incorrect for non-literature sources

### Observed

- PubMed/PMC links were generally appropriate.
- MyVariant opened a raw JSON API response.
- Some database/tool references had no useful human-facing link.
- Some non-PubMed links did not open useful pages.

### Required

Literature references and database/tool provenance must be treated differently.

Human-facing links must resolve to human-appropriate records when such records exist.

Raw machine endpoints must never masquerade as normal clinical references.

---

## Issue 4 — Analysis/API status UX is developer-oriented and confusing

### Observed

The current status panel exposes a dense wall of provider-specific warnings.

The user cannot easily answer:

```text
Which variant is affected?
What is actually missing?
Does the variant still have enough evidence?
Did the analysis fail, or only one source?
What action is required?
```

Some status text also appears inconsistent with the actual input mode or expected variant count.

### Required

The primary status experience must become:

```text
variant-first
concise
clinically understandable
action-oriented
```

Provider diagnostics should move behind an expandable technical-details view.

---

## Issue 5 — Warning semantics are not sufficiently differentiated

### Observed

The interface visually mixes:

```text
no exact record
unsupported allele
provider unavailable
partial evidence
interpretation failure
```

### Required

These are different states and must not look equivalent.

The application needs a small, stable severity vocabulary.

---

# 2. Hard product constraints

The following constraints must remain true during all fixes.

## 2.1 Per-variant independence

Every input variant remains an independent analysis unit and independent report unit.

Never merge variants merely because they belong to the same gene.

---

## 2.2 Original order

All variants must retain original input order through:

```text
input
normalization
evidence
interpretation
draft reports
review
selection
final output
```

---

## 2.3 No hidden variant loss

A variant must not disappear because:

```text
a provider returns no exact match
a provider does not support the allele
population data are missing
phenotype is unrelated
literature is absent
interpretation fails temporarily
```

Any real rejection must occur through explicit input-validation rules and be visible.

---

## 2.4 No fabricated evidence

Never synthesize provider results merely to fill a report field.

If evidence is unavailable, use states such as:

```text
NA
Not available
No exact record
Not supported by this source
No validated association found
```

---

## 2.5 No silent source substitution

Fallback evidence must retain its actual source.

Example:

```text
Ensembl population evidence
```

must never be displayed as:

```text
gnomAD frequency
```

if gnomAD was not the source.

---

## 2.6 No autonomous diagnosis/treatment expansion

The professor report includes clinical recommendations and diagnostic-style language.

The application may copy the **report layout and document style**, but must not automatically reproduce out-of-scope content such as:

```text
diagnosis
treatment recommendation
prenatal diagnosis recommendation
PGD recommendation
genetic counseling recommendation
clinical management instruction
```

unless the project scope is explicitly changed later.

---

# 3. Target user experience

After this roadmap:

```text
Input variants + phenotype
        ↓
Evidence collection
        ↓
Interpretation
        ↓
PER-VARIANT WORD REPORT GENERATION
        ↓
High-fidelity report preview
        ↓
Reviewer edit
        ↓
Reviewer include/exclude decision
        ↓
Confirm
        ↓
Final DOCX / PDF report output
```

The first meaningful object the reviewer sees after analysis should be the **report**, not a developer status console.

---

# 4. Report architecture decision

Use one structured report data model and two presentation outputs:

```text
Structured ReportData
        │
        ├── DOCX renderer
        │       ↓
        │   authoritative editable Word artifact
        │
        └── Preview renderer
                ↓
            in-app high-fidelity document view
```

Do not make raw HTML, Streamlit widgets, or LLM prose the source of truth.

The source of truth is:

```text
ReportData + template version
```

---

# Stage 79 — Acceptance Defect Freeze and Golden Test Assets

## Goal

Freeze the observed failures before changing architecture.

## Tasks

### 79.1 Create acceptance-defect registry

Add:

```text
docs/acceptance_failures_v1.md
```

Record:

```text
AF-01 report format mismatch
AF-02 interpretation request failure
AF-03 irrelevant phenotype handling
AF-04 non-literature reference failure
AF-05 status UX failure
AF-06 warning semantics failure
AF-07 variant-count/status inconsistency if reproducible
```

### 79.2 Preserve professor report as design reference

Document it as:

```text
Authoritative visual and structural reference
```

### 79.3 Create a synthetic non-PHI report fixture

Use synthetic data matching the professor report structure without real patient identifiers.

### 79.4 Freeze current screenshots

Store acceptance screenshots in developer documentation when useful.

## Acceptance criteria

All later changes can be tested against explicit observed failures.

---

# Stage 80 — Professor Report Template Specification

## Goal

Translate the professor PDF into an explicit implementation specification before writing rendering code.

This is the highest-value stage in the roadmap.

## 80.1 Define the per-variant report anatomy

Each variant gets its own report.

Recommended structure:

```text
SECTION A
NGS Result Report
Clinical Features
Conclusive Result(s)
Brief Interpretation(s)

SECTION B
Main Finding(s) in Detail
Evidence table
Variant interpretation

SECTION C
Variant(s) classification
Method / technical note
Comments / scope disclaimer
References
Data Sources
```

The exact page count may vary with narrative length.

The structural order should remain stable.

## 80.2 Define visual tokens

Extract and document:

```text
page size
page margins
font family
base font size
heading sizes
heading weights
table border thickness
table cell shading
alignment
line spacing
paragraph spacing
header/footer behavior
variant emphasis
classification emphasis
footnote style
reference style
```

Create a template-style specification such as:

```text
report_style_spec.yaml
```

or equivalent constants.

## 80.3 Conclusive Result block

Prominently show:

```text
Gene
HGVS c.
HGVS p. when available
Zygosity
Classification
```

Classification should be visually emphasized in a restrained clinical-report style.

## 80.4 Main Finding(s) table

The table is the most important structural element after the result block.

Support fields such as:

```text
Gene
Transcript
Variant
Population frequency
Associated disease
OMIM ID
Zygosity
Computational evidence
dbSNP / stable variant ID
Classification
Inheritance
```

Only show values supported by actual evidence.

## 80.5 Variant Interpretation section

The interpretation should be a coherent narrative, not an evidence dump.

Expected narrative sequence:

```text
gene function/context
gene-disease relationship
variant identity and molecular consequence
population evidence
clinical/database classification evidence
computational evidence
literature evidence
phenotype concordance or lack of concordance
conflict handling
evidence-limited conclusion
```

The sequence may adapt to missing evidence.

## 80.6 Variant Classification section

Summarize classification-related evidence without claiming independent ACMG adjudication unless the app actually performs it.

Differentiate:

```text
provider-reported classification
ClinVar classification
automated annotation classification
reviewer-confirmed interpretation
```

## 80.7 References section

Reserve for literature:

```text
PubMed
PMC
DOI-backed publications
```

## 80.8 Data Sources section

Separate database/tool provenance:

```text
ClinVar
Ensembl
GeneBe
MyVariant.info
ClinGen/GenCC
ClinGen CSpec
Phen2Gene / local HPO-Gene fallback
MyDisease
population provider
```

## 80.9 Remove out-of-scope professor-report content

Do not automatically generate real patient identifiers or clinical recommendations.

## Acceptance criteria

A developer unfamiliar with the project can recreate the target report from the specification without looking at the old UI.

---

# Stage 81 — ReportData V4 Schema

## Goal

Create one stable data contract for both DOCX generation and in-app preview.

## Recommended conceptual schema

```python
ReportData(
    report_id,
    analysis_id,
    input_index,
    variant_identity,
    phenotype_summary,
    conclusive_result,
    main_findings,
    interpretation,
    classification_summary,
    literature_references,
    data_sources,
    warnings,
    provenance,
    review_state,
    template_version,
)
```

## 81.1 Variant identity

Include stable allele identity:

```text
genome build
chromosome
position
reference
alternate
gene
transcript
HGVS c.
HGVS p.
zygosity
```

Do not require display fields that are genuinely unavailable.

## 81.2 Evidence table model

Populate the report table from typed fields, not preformatted LLM text.

## 81.3 Interpretation model

Store:

```text
original_model_interpretation
current_reviewer_interpretation
model
prompt_version
generated_at
edit_history
interpretation_status
```

## 81.4 Phenotype-concordance enum

Use:

```text
supported
partially_supported
no_supported_association
unavailable
not_assessed
```

This field must never require a positive association.

## 81.5 Report completeness

Do not require every provider to succeed before ReportData is valid.

## Acceptance criteria

A report can represent rich evidence and legitimate missingness without schema failure.

---

# Stage 82 — Authoritative DOCX Template

## Goal

Build an actual editable Microsoft Word template based on the professor report.

This is the most important implementation stage.

## 82.1 Create template file

Recommended:

```text
templates/clinical_variant_report_v1.docx
```

The template should visually reproduce the professor report as closely as practical.

## 82.2 Use placeholders/content controls

Examples:

```text
{{REPORT_TITLE}}
{{CLINICAL_FEATURES}}
{{VARIANT_DISPLAY}}
{{ZYGOSITY}}
{{CLASSIFICATION}}
{{BRIEF_INTERPRETATION}}
{{MAIN_FINDINGS_TABLE}}
{{VARIANT_INTERPRETATION}}
{{CLASSIFICATION_SUMMARY}}
{{REFERENCES}}
{{DATA_SOURCES}}
```

## 82.3 Keep layout in the template

The template should own:

```text
fonts
margins
table layout
borders
paragraph styles
spacing
heading styles
page breaks
footer styles
```

Python should primarily populate content.

## 82.4 Support variable-length interpretation

Handle short/long interpretations, many/few references, and missing fields without destructive layout shifts.

## 82.5 Table behavior

Ensure:

```text
no unreadable cell overflow
no clipped HGVS
reasonable row splitting
repeat header row where useful
controlled column widths
```

## 82.6 Per-variant artifact

Generate:

```text
variant_001_report.docx
variant_002_report.docx
...
```

Do not produce one shared draft document as the only review artifact.

## Acceptance criteria

Opening a generated report in Microsoft Word or LibreOffice should immediately resemble the professor reference in structure and visual hierarchy.

---

# Stage 83 — Golden DOCX Fidelity Gate

## Goal

Prevent a "close enough" implementation from replacing the real design requirement.

## Tasks

1. Create one golden synthetic variant with all major fields.
2. Generate DOCX through the production path.
3. Compare manually against the professor report.
4. Inspect:

```text
heading hierarchy
result emphasis
table structure
typography
paragraph flow
page breaks
reference formatting
overall density
clinical-document feel
```

## Explicit rejection criteria

Fail the stage if output looks like:

```text
generic Streamlit export
JSON dump
markdown document
unstyled python-docx output
form printout
developer report
```

## Acceptance criteria

The document visually reads as the same report family as the professor reference.

---

# Stage 84 — In-App High-Fidelity Report Preview

## Goal

Make the report—not the API dashboard—the dominant review surface.

## Architecture

Preferred:

```text
ReportData
    ↓
DOCX
    ↓
deterministic rendering
    ↓
PDF/image/HTML document preview
    ↓
embedded in application
```

The rendering technology may vary, but preview fidelity must remain high.

## 84.1 Default review screen

After analysis:

```text
Variant 1 of N
[document preview]

Previous
Next
Edit
Include in final report
Technical details
```

## 84.2 Do not lead with API status

Provider status must not occupy the main visual hierarchy.

## 84.3 Page navigation

Support multi-page reports clearly.

## 84.4 Variant navigation

Display stable allele identity, not gene alone.

## Acceptance criteria

A reviewer can inspect each report without reading raw evidence panels.

---

# Stage 85 — Document-Like Editing Workflow

## Goal

Allow editing without returning to generic form-first UX.

## Design principle

Default state:

```text
report view
```

Editing is an explicit mode.

## Option A — Preferred practical V1

Use a document-like editing surface for designated editable regions:

```text
Brief Interpretation
Variant Interpretation
Classification Summary
Reviewer Notes
```

Changes update ReportData and regenerate the Word artifact.

## Option B — Fallback architecture

If high-fidelity browser editing becomes excessively complex:

```text
high-fidelity report preview
+
Download editable DOCX
+
Upload reviewed DOCX / structured review confirmation
```

This should be a fallback, not the first choice if stable in-app editing is practical.

## 85.1 Never silently edit generated evidence

Reviewer changes to evidence fields must be explicit and audited.

## 85.2 Preserve edit history

Persist old/new value and timestamp.

## 85.3 Editing invalidates confirmation

Any material edit after confirmation requires reconfirmation.

## Acceptance criteria

The reviewer edits a clinical report, not a disconnected collection of widgets.

---

# Stage 86 — Per-Variant Report Lifecycle Refactor

## Goal

Make the report the primary artifact throughout review.

## Lifecycle

```text
evidence package
    ↓
interpretation
    ↓
ReportData
    ↓
DOCX draft
    ↓
review/edit
    ↓
selection
    ↓
confirmation
    ↓
final artifact
```

## 86.1 One report per input variant

Every accepted variant creates one draft report record.

## 86.2 Exclusion is reporting-only

Excluded variants remain analyzed, stored, and auditable.

## 86.3 Preserve original order

Selected reports appear in original input order.

## Acceptance criteria

Internal lifecycle matches the user-facing concept of reviewing variant reports.

---

# Stage 87 — Variant Cardinality and Identity Integrity Gate

## Goal

Eliminate any possibility that 7 input variants become 5 without explicit explanation.

## Required invariant

For an accepted 7-variant input:

```text
parser count = 7
normalized count = 7
pipeline count = 7
evidence package count = 7
draft report count = 7
review record count = 7
```

Provider-specific success counts may be lower, but total variant cardinality may not silently shrink.

## 87.1 Persist input index

Every object retains:

```text
input_index
```

## 87.2 Stable allele identity

Use allele-level identity, not gene-level identity.

## 87.3 Duplicate-gene safety

Two variants in the same gene remain separate everywhere.

## Acceptance criteria

A dedicated test proves cardinality preservation end to end.

---

# Stage 88 — Interpretation Error Taxonomy and Observability

## Goal

Replace opaque `VariantInterpretationError` with actionable internal diagnostics.

## Internal failure types

```text
request_timeout
connection_error
http_429
http_5xx
authentication_error
invalid_request
empty_response
output_schema_failure
output_parse_failure
safety_or_finish_failure
internal_conversion_failure
unknown_failure
```

## 88.1 User message remains concise

User-facing messages should not expose internal exception names by default.

## 88.2 Structured logging

Store:

```text
variant_id
model
prompt_version
attempt
failure_type
http_status
finish_reason
schema_error
fallback_used
```

Never store secrets.

## Acceptance criteria

A developer can identify the real failure without blind reproduction.

---

# Stage 89 — Interpretation Recovery Policy

## Goal

Make interpretation reliable without reintroducing weak/strong semantic routing.

## Primary rule

The user-selected Variant Interpretation Model remains the primary model for all variants.

Conflict and no-conflict variants use the same selected model.

## 89.1 Retry transient request failures

Recommended:

```text
attempt 1
↓
bounded retry
↓
attempt 2
```

## 89.2 Structured-output repair

If request succeeds but response fails schema validation:

```text
one constrained repair/retry
```

Do not immediately discard a useful response.

## 89.3 Optional operational fallback model

Allow a separately configured backup interpretation model only for operational recovery.

This must not become difficulty-based routing.

## 89.4 Preserve original evidence

Fallback receives the same normalized evidence package.

## Acceptance criteria

Transient failures do not immediately produce an empty interpretation.

---

# Stage 90 — Phenotype Non-Concordance Contract

## Goal

Make irrelevant phenotype a first-class valid outcome.

## Required behavior

Given:

```text
valid variant evidence
+
valid but unrelated phenotype
```

interpretation still succeeds.

## 90.1 Allowed phenotype conclusions

```text
supported
partially supported
no supported association found
phenotype evidence unavailable
```

## 90.2 Forbidden behavior

Do not:

```text
hallucinate a phenotype link
force a disease match
fail the interpretation
drop the variant
treat phenotype mismatch as negative pathogenicity evidence by default
```

## 90.3 Prompt contract

Explicitly instruct:

```text
If the supplied phenotype is not supported by the evidence,
state that no supported association was identified.
Continue interpreting the variant from the remaining evidence.
```

## 90.4 Test case

Keep an intentionally unrelated phenotype fixture such as abdominal pain.

Expected:

```text
interpretation generated
explicit non-concordance statement
no hallucinated association
```

## Acceptance criteria

Irrelevant phenotype no longer causes interpretation failure.

---

# Stage 91 — Interpretation Quality Upgrade

## Goal

Evaluate whether the current interpretation model is appropriate after architecture is fixed.

Do not use model switching to mask integration defects.

## Sequence

```text
fix request/schema/retry behavior
then benchmark model quality
```

## 91.1 Evaluation cases

Use:

```text
strong phenotype match
partial phenotype match
irrelevant phenotype
conflicting ClinVar evidence
sparse evidence
rich literature
no literature
```

## 91.2 Compare candidate models

Measure:

```text
groundedness
hallucination rate
clinical-style coherence
conflict handling
phenotype restraint
structured-output reliability
latency
cost
```

## 91.3 Keep task-specific model selection

Phenotype extraction and variant interpretation remain independently selectable.

## Acceptance criteria

Default model choice is evidence-based rather than assumed.

---

# Stage 92 — Reference Model V2

## Goal

Separate scientific literature from database/tool provenance.

## Two categories

### Literature References

```text
PubMed
PMC
DOI
```

### Data Sources

```text
ClinVar
Ensembl
GeneBe
MyVariant.info
ClinGen/GenCC
CSpec
Phen2Gene/local HPO-Gene
MyDisease
population source
```

Do not mix them in one numbered bibliography.

---

# Stage 93 — Canonical Human-Link Resolver

## Goal

Every clickable user-facing link opens an appropriate human-readable target whenever one exists.

## Resolver policy

### PubMed

PMID → canonical PubMed article.

### PMC

PMCID → canonical PMC article.

### DOI

DOI → canonical DOI resolver.

### ClinVar

Use stable ClinVar identifiers and human-facing record pages.

### GeneBe

Use a human-facing variant page when a valid stable mapping exists.

### Ensembl

Prefer stable human-facing variation/record pages when appropriate identifiers exist.

Do not pretend a transient VEP API response is a permanent clinical reference.

### MyVariant.info

Do not make raw JSON the primary reviewer-facing reference.

Display:

```text
MyVariant.info
Programmatic annotation source
```

If raw endpoint is retained for transparency, label it explicitly:

```text
View raw source data
```

### No validated record URL

Display provenance only and do not fabricate URLs.

## Acceptance criteria

A reviewer never clicks a normal-looking reference and lands unexpectedly on raw machine JSON.

---

# Stage 94 — Reference Validation Tests

## Goal

Prevent regression of broken links.

## Tests

```text
PMID → correct article
PMCID → correct article
DOI → correct resolver
ClinVar ID → correct record
GeneBe supported variant → human record
MyVariant → no misleading primary raw-JSON link
missing identifier → no fabricated link
unsupported provider → provenance only
```

Optional live tests remain separate from deterministic offline CI.

---

# Stage 95 — User-Facing Analysis Summary Redesign

## Goal

Replace the wall of API statuses with a concise product-level summary.

## Top-level summary

Example:

```text
Analysis complete

7 variants analyzed
7 draft reports prepared
3 variants have partial source coverage
1 interpretation requires attention
```

This is what the user sees first.

## 95.1 Do not expose provider logs by default

Technical provider details are collapsed.

## 95.2 Use actual input terminology

For XLSX:

```text
Excel input validated
```

not:

```text
manual input
VCF processing
```

## 95.3 Status text must reflect real counts

Never display a provider subset count as the total number of variants.

## Acceptance criteria

A non-developer understands the analysis state in under 10 seconds.

---

# Stage 96 — Variant-First Status Cards

## Goal

Show what happened to each variant.

## Example

```text
Variant 1 of 7 — PAX3 c....
Status: Report ready

Annotation available
Population evidence available
ClinVar available
Phenotype relationship supported
Interpretation completed
```

Another:

```text
Variant 4 of 7 — TELO2 c....
Status: Report ready with partial evidence

Annotation available
ClinVar: no exact record
Population: available
Phenotype relationship: no supported association
Interpretation completed
```

## 96.1 Stable statuses

Recommended:

```text
Report ready
Report ready with partial evidence
Interpretation requires attention
Input requires attention
```

Do not use internal exception names as primary labels.

## 96.2 Technical details

Expandable:

```text
Technical provider details
```

## Acceptance criteria

Every warning is clearly attributable to a specific variant.

---

# Stage 97 — Warning Semantics V2

## Goal

Create a small severity model.

## INFO

Expected evidence absence:

```text
No exact record
No literature found
No supported phenotype association
```

## PARTIAL

Analysis succeeded, but optional sources were unavailable or unsupported.

## ACTION REQUIRED

A core output such as interpretation could not be produced after recovery attempts.

## BLOCKING

A true minimum invariant failed:

```text
invalid allele identity
unsupported input format
corrupt input
required normalization impossible
```

## 97.1 Never treat no-match as provider failure

Maintain the resilience contract.

## 97.2 Do not over-warn

Normal missing-data states must not look like application crashes.

## Acceptance criteria

Warnings communicate consequence, not implementation trivia.

---

# Stage 98 — Technical Diagnostics Drawer

## Goal

Preserve developer observability without polluting reviewer workflow.

## Contents

For each provider:

```text
provider
variant identity
status
attempt count
latency
fallback used
failure category
provider-specific note
```

## Access

Collapsed by default under:

```text
Show technical details
```

## Acceptance criteria

Developer debugging remains possible while the main UI stays clean.

---

# Stage 99 — Persistence and Recovery V4

## Goal

Persist the report-first workflow correctly.

Persist:

```text
ReportData
DOCX artifact metadata
template version
interpretation version
reference resolution
review edits
include/exclude state
confirmation state
warning state
```

## 99.1 Recovery

After refresh/restart:

```text
report preview returns
review edits return
selection returns
```

Do not regenerate interpretation unnecessarily.

## 99.2 Artifact identity

Generated DOCX artifacts map to:

```text
analysis_id
variant_id
report_version
```

## Acceptance criteria

Report review survives normal recovery scenarios.

---

# Stage 100 — DOCX Export and Final Package

## Goal

Make Word the authoritative editable deliverable.

## Per-variant export

Provide:

```text
Download editable DOCX
```

for each report.

## Selected final package

After confirmation:

```text
selected reports only
original order
```

Possible outputs:

```text
individual DOCX files
combined final DOCX
PDF rendering
ZIP package
```

according to project export scope.

## 100.1 No hidden regeneration on export

Final export uses reviewer-approved ReportData.

Do not silently rerun the LLM during export.

## Acceptance criteria

What the reviewer approved is what gets exported.

---

# Stage 101 — Visual Regression Harness

## Goal

Protect the highest-value feature: report format.

## Strategy

Create deterministic synthetic ReportData fixtures:

```text
fully populated
sparse evidence
long interpretation
many references
no phenotype match
complex indel
```

Generate reports and render them to a stable preview format.

Check:

```text
page count range
table existence
heading order
required labels
overflow
unexpected blank pages
```

Where practical, add image/PDF snapshot checks with reasonable tolerance.

Do not rely only on text tests for visual layout.

## Acceptance criteria

Future code changes cannot silently destroy the report layout.

---

# Stage 102 — Interpretation Acceptance Suite

## Goal

Validate corrected interpretation architecture.

## Required scenarios

### 102.1 Normal evidence
Interpretation succeeds.

### 102.2 Irrelevant phenotype
Interpretation succeeds and explicitly states non-concordance.

### 102.3 Partial provider coverage
Interpretation succeeds with restrained language.

### 102.4 Conflicting evidence
Interpretation acknowledges conflict without forcing resolution.

### 102.5 Transient model failure
Retry/recovery occurs.

### 102.6 Structured-output failure
Repair path is exercised.

### 102.7 Total interpretation failure
Variant remains available and user gets a clear action-required state.

## Acceptance criteria

Interpretation is reliable, transparent, and evidence-grounded.

---

# Stage 103 — Reference Acceptance Suite

## Goal

Validate user-facing references and provenance.

## Required checks

```text
all literature links open correct records
database sources are separated from literature
no raw JSON masquerades as normal reference
no fabricated links
human-readable provider pages used when available
missing stable link handled cleanly
```

---

# Stage 104 — Status and Warning UX Acceptance Suite

## Goal

Verify that the new UI is understandable without developer logs.

A tester should be able to answer:

```text
How many variants were analyzed?
How many reports are ready?
Which variant needs attention?
Which source is missing for that variant?
Is this missing evidence or an application failure?
```

without opening technical details.

---

# Stage 105 — Professor Testcase End-to-End Acceptance

## Goal

Re-run the real professor-provided acceptance input after all fixes.

## Required invariants

For the professor Excel testcase:

```text
first worksheet only
7 variants
original order preserved
all 7 analyzed
all 7 have independent report records
same-gene variants remain separate
```

## Report checks

For each variant:

```text
high-fidelity professor-style report exists
DOCX generated
preview shown
interpretation present or explicit action-required state
references correct
data sources correct
warnings understandable
```

## Phenotype negative-control test

Run an intentionally irrelevant phenotype.

Expected:

```text
report still generated
interpretation still generated
phenotype non-concordance stated
no fabricated relation
```

## Provider degradation test

Force at least one fallback path.

Expected:

```text
report still generated
actual source named
technical failure hidden from primary UX
```

## Acceptance criteria

The professor testcase completes as a coherent report-review workflow.

---

# Stage 106 — Professor-Style Report Sign-Off Gate

## Goal

Do not call the roadmap complete until the report itself is approved visually.

## Manual sign-off questions

### Report

```text
Does this look like the professor report family?
Would a reviewer recognize it as a clinical-style variant report?
Is the Main Finding(s) table immediately readable?
Is the interpretation positioned and formatted correctly?
Do long reports still look professional?
```

### Editing

```text
Can the reviewer edit the narrative without fighting the UI?
Does editing regenerate the approved Word artifact correctly?
```

### References

```text
Do literature references look like references?
Do database sources look like provenance?
```

### Status

```text
Is the report more visually prominent than provider diagnostics?
```

## Acceptance criteria

Manual approval is mandatory.

Automated tests alone cannot pass this stage.

---

# 39. Recommended implementation order

```text
79  Acceptance defect freeze
        ↓
80  Professor report template specification
        ↓
81  ReportData V4
        ↓
82  Authoritative DOCX template
        ↓
83  Golden DOCX fidelity gate
        ↓
84  High-fidelity in-app preview
        ↓
85  Document-like editing workflow
        ↓
86  Per-variant report lifecycle
        ↓
87  Variant cardinality/identity integrity
        ↓
88  Interpretation error taxonomy
        ↓
89  Interpretation recovery
        ↓
90  Phenotype non-concordance
        ↓
91  Interpretation model quality evaluation
        ↓
92  Reference model V2
        ↓
93  Canonical human-link resolver
        ↓
94  Reference tests
        ↓
95  Analysis summary redesign
        ↓
96  Variant-first status
        ↓
97  Warning semantics
        ↓
98  Technical diagnostics drawer
        ↓
99  Persistence/recovery
        ↓
100 DOCX/final export
        ↓
101 Visual regression
        ↓
102 Interpretation acceptance
        ↓
103 Reference acceptance
        ↓
104 Status/warning UX acceptance
        ↓
105 Professor testcase E2E
        ↓
106 Professor-style report sign-off
```

---

# 40. Priority grouping

## P0 — Must complete first

```text
80
81
82
83
84
85
86
87
```

These stages deliver the main product value.

## P1 — Reliability and correctness

```text
88
89
90
91
92
93
94
```

## P2 — UX cleanup

```text
95
96
97
98
```

## P3 — Hardening and final acceptance

```text
99
100
101
102
103
104
105
106
```

---

# 41. Suggested Codex model strength

To minimize token usage while protecting architectural stages:

| Stage | Recommended mode |
|---|---|
| 79 | Medium |
| 80 | High |
| 81 | High |
| 82 | High |
| 83 | High |
| 84 | High |
| 85 | High |
| 86 | High |
| 87 | Medium |
| 88 | Medium |
| 89 | High |
| 90 | Medium |
| 91 | High |
| 92 | Medium |
| 93 | High |
| 94 | Medium |
| 95 | Medium |
| 96 | Medium |
| 97 | Medium |
| 98 | Medium |
| 99 | High |
| 100 | High |
| 101 | High |
| 102 | High |
| 103 | Medium |
| 104 | Medium |
| 105 | High |
| 106 | High/manual |

If token budget is especially tight:

```text
Use High only for:
80–86
89
93
99–102
105–106

Use Medium for the rest.
```

---


# 41A. Global token-efficiency rule

This roadmap must be executed with **disciplined token efficiency**, but token savings must **never** come from reducing implementation quality, skipping validation, weakening correctness, or cutting architectural corners.

The rule is:

```text
Minimize token usage without compromising implementation quality,
debugging rigor, test coverage, or acceptance fidelity.
```

## Non-negotiable interpretation

Token efficiency must mean:

- narrower scope per step;
- more focused repository inspection;
- less repetitive prompting;
- shorter and cleaner progress reports;
- targeted testing before broad testing where appropriate;
- avoiding unnecessary re-reading of unrelated files.

Token efficiency must **not** mean:

- lower-quality reasoning on a hard stage;
- weaker fixes;
- speculative shortcuts;
- skipping root-cause analysis;
- skipping regression tests;
- ignoring visual fidelity requirements;
- replacing the professor-style report goal with a simpler generic output;
- bypassing validation or review logic merely to "make it work";
- silently deferring important failures.

## Mandatory token-efficiency rules for Codex

For every stage, Codex must follow all of the rules below:

1. Read only the current stage and the minimum directly related project files.
2. Do not repeatedly re-read the full roadmap unless needed for dependency alignment.
3. Do not scan the whole repository broadly if a targeted search will do.
4. Prefer narrow code inspection over open-ended exploration.
5. Reuse working abstractions rather than rewriting stable subsystems.
6. Do not implement future stages early.
7. Do not perform speculative refactors.
8. Do not produce long explanations unless they are necessary to resolve a blocking ambiguity.
9. Do not dump large logs, full diffs, full source files, or oversized test output into the response.
10. Summarize command/test results concisely.
11. Run targeted tests first where appropriate.
12. Run the full offline suite when the stage requires it or after a meaningful code change is complete.
13. Use the recommended model strength from this roadmap rather than escalating unnecessarily.
14. If a stage is hard, quality takes precedence over token reduction.
15. If a stage is visually or architecturally critical, do not simplify the deliverable merely to save tokens.

## Required completion report format

At the end of each stage, report only:

```text
1. files changed
2. exact behavior changed
3. tests added
4. tests run / commands run
5. pass/fail
6. remaining manual checks
```

unless a blocking design ambiguity truly requires more explanation.

## Explicit quality override

If there is ever a conflict between:

```text
lower token use
```

and:

```text
correctness
acceptance fidelity
report visual quality
robust debugging
safe recovery behavior
```

Codex must choose the second group.

In short:

```text
Be economical with tokens,
but never cheap with quality.
```

# 42. Codex execution protocol

Use the roadmap stage by stage.

Recommended prompt pattern:

```text
Read Stage <N> only from POST_ACCEPTANCE_FIX_ROADMAP.md.

Work in a token-efficient way, but do not reduce quality, correctness, test rigor, or report fidelity.

Implement Stage <N> only.

Before editing:
1. inspect the current implementation relevant to this stage;
2. identify the smallest coherent change;
3. preserve unrelated behavior.

Do not implement later stages early.

Hard constraints:
- professor report is the authoritative visual/structural reference;
- one independent report per variant;
- DOCX is the authoritative editable artifact;
- no testcase hard-coding;
- no silent variant loss;
- no fabricated evidence;
- irrelevant phenotype is a valid outcome;
- missing provider evidence is not automatically a failure;
- literature references and database sources are separate;
- raw API JSON is not a normal user-facing reference;
- primary UX is variant/report-first, not provider-log-first.

After implementation:
1. run targeted tests;
2. run the full offline test suite;
3. report only:
   - files changed
   - exact behavior changed
   - tests added
   - commands run
   - pass/fail
   - remaining manual checks
```

---

# 43. Guardrail for the most important work

For Stages 80–85, additionally tell Codex:

```text
Do not interpret "professor-style report" as loose inspiration.

Treat TS-Final Report(2).pdf as the authoritative visual and structural
reference for the per-variant report.

The output must not look like:
- Streamlit cards
- a generic web form
- markdown
- JSON
- a developer report
- an unstyled python-docx document

The default user-facing object after analysis must be a clinical-report
document.

The Word artifact must be editable.

If browser editing cannot preserve fidelity, keep the DOCX as the
authoritative artifact and use a high-fidelity rendered preview for review,
with editing handled through a controlled document-like workflow.
```

---

# 44. Definition of done

This roadmap is complete only when all of the following are true.

## Report

- Every accepted variant receives its own report.
- The report closely reproduces the professor report's visual family and structure.
- The Main Finding(s) table is a central visual component.
- Variant interpretation is integrated directly into the report.
- Classification and references appear in report form.
- Editable DOCX is generated for every report.
- Reviewer edits are preserved.

## Variant integrity

- No accepted variant disappears.
- Original order is preserved.
- Same-gene variants remain separate.

## Interpretation

- Normal interpretation succeeds.
- Irrelevant phenotype is handled as non-concordance, not failure.
- Transient LLM failures receive bounded recovery.
- Structured-output failures are diagnosable.
- Evidence remains grounded.

## References

- PubMed/PMC/DOI links are canonical.
- Database/tool sources are separated from literature.
- Human-readable pages are preferred.
- Raw JSON does not masquerade as a normal reference.
- Missing links are not fabricated.

## Status UX

- User sees a concise overall summary.
- User sees per-variant status.
- Technical provider details are collapsed.
- Warnings have meaningful severity.
- Expected missing evidence does not look like an application crash.

## Final acceptance

- Professor Excel testcase completes end to end.
- All expected variants survive.
- Reports are produced.
- Manual visual sign-off confirms that the report experience matches the intended clinical-report workflow.

---

# 45. Final roadmap principle

Do not optimize the wrong surface.

The main product is not the API status panel.

The main product is:

```text
a trustworthy,
evidence-grounded,
editable,
professor-style,
per-variant clinical report
```

Everything else should support that experience.
