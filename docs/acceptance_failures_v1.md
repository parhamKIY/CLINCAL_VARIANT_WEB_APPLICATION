# Acceptance Failures V1

**Project:** Clinical Variant Interpretation  
**Freeze date:** 2026-08-11  
**Roadmap stage:** 79  
**Status:** Frozen baseline for Stages 80-106  
**Scope:** Acceptance defects observed after the Stage 78 provider-resilience milestone

## 1. Purpose

This registry freezes the post-acceptance product defects before report-first
architecture work begins. Later stages must resolve these defects without weakening
the established privacy, evidence, provenance, ordering, or human-review contracts.

This document records observed behavior and acceptance expectations. It does not
implement the fixes assigned to later stages.

## 2. Authoritative design reference

The professor-supplied PDF is the authoritative visual and structural reference for
the future per-variant clinical report. The roadmap names the asset
`TS-Final Report(2).pdf`; the locally available copy inspected for this freeze is
`../TS-Final Report.pdf`.

The reference is a four-page, US Letter report created in Microsoft Word. Its key
visual and structural characteristics are:

- a centered `NGS Result Report` title and restrained laboratory-document typography;
- a prominent bordered conclusive-result block;
- a central `Main Finding(s) in Detail` evidence table with grouped headers and
  shaded value rows;
- a long-form `Variant interpretation` narrative immediately after the main table;
- separate classification, method/comments, references, and footnote areas;
- dense but readable page composition with consistent margins and hierarchy.

Only the visual structure and document family are authoritative. Names, identifiers,
dates, sample metadata, diagnoses, recommendations, signatures, and other clinical
content in the supplied PDF must not be copied into project fixtures or generated
reports. The PDF contains sensitive clinical identifiers and remains a local,
uncommitted reference asset.

## 3. Frozen defect registry

| ID | Frozen defect | Baseline status | Required outcome | Primary verification stage |
|---|---|---|---|---:|
| AF-01 | Draft report format mismatch | Confirmed | One high-fidelity, editable, professor-style report per accepted variant | 83, 84, 85, 106 |
| AF-02 | Interpretation request failure is opaque and terminal after one failure | Confirmed | Typed diagnostics plus bounded retry/repair and explicit final failure state | 88, 89, 102 |
| AF-03 | Irrelevant phenotype is not represented as a normal non-concordant outcome | Confirmed acceptance gap | Interpretation continues and states that no supported association was found | 90, 102, 105 |
| AF-04 | Non-literature references can lead to raw or non-useful machine endpoints | Confirmed | Literature and data-source provenance are separated; links are human-appropriate or absent | 92, 93, 94, 103 |
| AF-05 | Analysis/API status is provider-first and developer-oriented | Confirmed | Concise analysis summary followed by variant-first report status | 95, 96, 104 |
| AF-06 | Warning semantics mix expected missingness with failures | Confirmed | Stable `INFO`, `PARTIAL`, `ACTION_REQUIRED`, and `BLOCKING` consequences | 97, 104 |
| AF-07 | Displayed variant count or input-mode wording may disagree with the accepted input | Investigation item | Every displayed count and label derives from accepted input identity and cardinality | 87, 95, 105 |

AF-07 is frozen as an investigation item because no deterministic reproduction was
captured during Stage 79. It must not be described as a confirmed data-loss defect
unless a test reproduces it. Existing Stage 60 and Stage 78 gates continue to prove
their own fixture cardinality; Stage 87 must add the dedicated end-to-end invariant.

Stage 87 resolution: no silent-loss defect was reproduced. A dedicated seven-variant
end-to-end gate now proves parser, normalized, pipeline, evidence, draft-report, and
review-record cardinality remain equal after persistence and reload. The same fixture
proves multiple alleles annotated to one gene remain independent.

## 4. Frozen acceptance scenarios

### AF-01 - Report format mismatch

**Baseline evidence:** The active review surface is assembled from Streamlit
headings, status messages, evidence sections, editable text areas, warnings, and a
reference list. The current exported report is not the authoritative editable
per-variant review object.

**Acceptance expectation:** Every accepted allele receives an independent Word report
whose section order, table hierarchy, typography, spacing, and narrative placement
belong to the same visual family as the professor reference. The report is the primary
review surface. Generic cards, Markdown, JSON, or unstyled `python-docx` output do not
satisfy this defect.

### AF-02 - Interpretation request failure

**Baseline evidence:** The current review UI can display
`Interpretation is unavailable because the model request failed` followed by the
internal failure type. Evidence remains available, but the user cannot distinguish a
timeout, rate limit, invalid response, structured-output failure, or parser failure.

**Acceptance expectation:** Internal diagnostics identify the failure category and
attempt history without exposing secrets. Transient request failures receive bounded
retry, repairable structured-output failures receive one constrained repair path, and
an exhausted failure remains attached to the variant as an action-required state.

### AF-03 - Irrelevant phenotype handling

**Baseline evidence:** The acceptance test supplied a valid but intentionally
irrelevant phenotype and did not receive the required explicit non-concordance
outcome.

**Acceptance expectation:** A phenotype mismatch never removes the variant, blocks
report generation, forces a disease relationship, or becomes negative pathogenicity
evidence by default. Interpretation continues from the remaining evidence and may
state `No supported phenotype association was identified`.

### AF-04 - Reference architecture

**Baseline evidence:** PubMed and PMC destinations are generally useful, while a
MyVariant destination can expose raw JSON and some database/tool sources lack a
useful human-facing record.

**Acceptance expectation:** PubMed, PMC, and DOI publications appear under
`References`. ClinVar, Ensembl, GeneBe, MyVariant.info, ClinGen, CSpec, phenotype,
disease, and population services appear under `Data Sources`. A normal-looking link
must never unexpectedly open raw machine JSON. When no validated human page exists,
the report shows provenance without fabricating a URL.

### AF-05 - Status UX

**Baseline evidence:** The active UI renders an `Analysis status` area, stage records,
an `External API status` block, pipeline warnings, and provider/evidence tables before
the reviewer can form a concise product-level understanding.

**Acceptance expectation:** The default result view first states how many variants
were analyzed, how many reports are ready, and which variants require attention.
Technical provider details are collapsed and remain available for debugging.

### AF-06 - Warning semantics

**Baseline evidence:** No exact record, unsupported source behavior, operational
failure, partial coverage, and interpretation failure can all be presented with
similar warning/error emphasis.

**Acceptance expectation:** Expected evidence absence is informational; optional
source failure is partial; exhausted core interpretation failure requires action; and
only a failed minimum invariant blocks the workflow. Valid `no_match` remains
distinct from provider failure.

### AF-07 - Cardinality or terminology inconsistency

**Baseline evidence:** Acceptance feedback reported status text that did not appear
to match the input mode or expected variant count. Stage 79 did not reproduce silent
loss in a deterministic fixture.

**Acceptance expectation:** Parser, normalization, pipeline, evidence, draft report,
and review counts remain equal for every accepted input. Provider success counts are
never presented as total variant counts. Input labels reflect VCF, Excel, or manual
input accurately.

## 5. Synthetic non-PHI golden asset

`tests/fixtures/stage79_synthetic_report_fixture.json` freezes synthetic content and
the expected professor-style section order. It is a design and acceptance fixture,
not the future `ReportData V4` production schema. Later schema work may transform the
fixture through an explicit adapter, but must preserve its semantic sections,
independent allele identity, missingness states, and separation of literature from
data-source provenance.

The fixture intentionally contains:

- one synthetic allele with an explicit input index and assembly;
- a de-identified clinical-feature summary;
- a prominent conclusive-result payload;
- a typed main-findings row;
- a coherent evidence-limited interpretation with phenotype non-concordance;
- distinct classification, method, comments, references, and data-source sections;
- expected informational and partial-evidence warnings;
- no names, sample identifiers, national identifiers, contact details, raw VCF rows,
  or real clinical recommendations.

## 6. Screenshot freeze decision

No current application screenshot is committed at Stage 79. The available professor
input and report contain sensitive identifiers, and the repository has no
deterministic synthetic screenshot workflow yet. Freezing those screens would create
privacy and reproducibility risk without improving the defect contract.

Stages 83, 84, and 101 must create deterministic rendered artifacts and visual
baselines from synthetic non-PHI data. Until then, this registry, the synthetic JSON
fixture, the inspected local PDF, and source-level baseline evidence are the frozen
acceptance assets.

## 7. Stage 79 completion boundary

Stage 79 is complete when:

- AF-01 through AF-07 have stable identifiers and explicit expected outcomes;
- the professor PDF is documented as a local confidential design reference;
- the synthetic fixture is parseable, structurally complete, and privacy-safe;
- tests prevent accidental loss of defect IDs, section order, variant identity,
  literature/data-source separation, and non-PHI safeguards;
- no Stage 80 or later production architecture has been implemented early.
