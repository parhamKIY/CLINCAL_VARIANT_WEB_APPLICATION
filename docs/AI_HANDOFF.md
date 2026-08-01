# AI Project Handoff

## Purpose

This file is the authoritative continuation guide for a new Codex chat working
in:

```text
C:\university\internship project\clinical_variant_app
```

Read this file completely before changing the project.

## Required working method

1. Inspect `git status` before editing.
2. Break every requested alteration into small, sequential steps.
3. Complete and validate one step before moving to the next.
4. Preserve unrelated user changes and generated output.
5. Use `apply_patch` for source-file edits.
6. Run tests appropriate to the changed area.
7. After completing an alteration, provide a concise Git commit title.
8. Explain implementation and biological purpose when introducing a new
   clinical-variant workflow stage.
9. Keep LLM provider-specific details isolated behind the existing LLM
   boundary; changing provider credentials or the default model should not
   require changes throughout the application.
10. Do not make live international API calls unless the user requests them or
    they are necessary and the user has been warned that network problems may
    affect the result.

## Current project state

The original 16-stage educational MVP is complete. The application currently
provides:

- a Streamlit frontend with light and dark themes;
- filtered VCF upload and manual variant-table input;
- Ensembl VEP, MyVariant.info, NCBI ClinVar, and UCSC GenCC annotation;
- local HPO search, matching, and data updates;
- sanitized Evidence Objects;
- provider-neutral LLM interpretation with selectable models;
- plain-text, PDF, and DOCX report downloads;
- cancellable background analysis;
- SQLite persistence;
- structured logging, safe errors, privacy controls, and secrets auditing;
- offline acceptance, regression, coverage, and security tests.

The application is clinical decision support only. It is not a diagnosis or
treatment system.

The next project direction has been defined as a planning roadmap only. No
ACMG/AMP rules engine, gene-phenotype graph, patient-priority model,
conflict router, or tiered LLM workflow has been implemented yet.

## Latest architecture decision: filtered input

The professor changed the input responsibility:

- filtering, optimization, normalization, and clinical ranking happen
  upstream;
- the application receives an already-filtered VCF-style table;
- the table must contain between one and five source rows;
- input may be uploaded as `.vcf` or `.vcf.gz`, or entered manually;
- every supplied variant is annotated in original input order;
- multi-allelic rows are split into allele-specific variants;
- the generated interpretation report focuses on the first listed allele;
- the application must not claim that it ranked or prioritized that allele;
- VCF genotype and sample fields are ignored and excluded from public output.

The Streamlit manual input is a fixed five-row, table-style group of native
widgets with:

```text
CHROM | POS | REF | ALT | QUAL | FILTER
```

`CHROM`, `POS`, `REF`, and `ALT` are required for each used row. `QUAL` and
`FILTER` are optional.

Each CHROM dropdown is restricted to the 25 standard primary labels `1`–`22`,
`X`, `Y`, and `MT`. Its POS field remains disabled until CHROM is selected,
then displays the exact valid GRCh37/GRCh38 range as an in-field placeholder.
The native Streamlit number input receives the row-specific minimum and
maximum, so out-of-range input is marked invalid and cannot be analyzed.
Backend validation enforces the same boundary. Only POS is
chromosome-dependent: REF and ALT use allele-syntax validation, QUAL is an
optional non-negative value, and FILTER is optional text.

## Latest completed implementation

The row-aware manual-input refinement is complete and committed.

Main changes:

- replaced the shared `st.data_editor` with five table-style rows composed
  exclusively of native Streamlit widgets;
- retained the 25-option CHROM dropdown in every row;
- disabled each POS field until its own CHROM is selected;
- added the selected chromosome's exact range inside its POS placeholder;
- passed row-specific minimum and maximum values to `st.number_input` for
  native invalid-field feedback and submission blocking;
- preserved authoritative frontend and backend coordinate validation;
- updated offline UI regression tests and synchronized documentation.

Commit:

```text
6a3aac5 refactor: add row-aware manual variant inputs
```

The preceding chromosome-coordinate validation was committed as:

```text
f8a84b1 feat: validate manual chromosome coordinates
```

## New professor-requested direction: planned, not implemented

The professor clarified that the final output should provide a complete
clinical-decision-support section for every supplied variant, not only a
general annotation summary.

The target output for each allele is now planned to include:

1. a bounded, versioned, and cited Variant Evidence Package;
2. deterministic ACMG/AMP evidence criteria;
3. a five-tier ACMG/AMP classification;
4. ACMG/AMP evidence points when the approved framework supports them;
5. a separate patient-specific clinical-priority result;
6. a separate evidence-confidence result;
7. deterministic evidence-conflict detection;
8. a provenance-backed gene-phenotype graph conclusion;
9. phenotype, disease, inheritance, and gene relevance;
10. an evidence-constrained LLM interpretation for every supplied allele.

Keep these concepts separate:

- ACMG/AMP classification answers how pathogenic a variant is for a selected
  disease and inheritance context.
- ACMG/AMP evidence points quantify evidence strength under a selected,
  versioned framework.
- Clinical priority estimates how relevant the finding is to the current
  patient/case.
- Evidence confidence describes completeness, review quality, provenance, and
  agreement.
- The gene-phenotype graph explains patient HPO to disease to gene to variant
  relationships.
- The LLM explains the deterministic results and must not independently assign
  ACMG/AMP criteria, classification, points, priority, or confidence.

The planned conclusion flow is:

```text
Validated variant
  -> Variant Evidence Package
  -> ACMG/AMP criteria and classification
  -> gene-phenotype graph relevance
  -> patient priority and evidence confidence
  -> deterministic conflict routing
  -> inexpensive LLM for straightforward evidence
     OR strong LLM for complex/conflicting evidence
  -> mandatory human-review flag for material unresolved conflict
  -> one clinical report section per allele
```

The conflict router must be deterministic. A cheap LLM must not decide whether
evidence is conflicting. Structured comparison should first normalize allele,
assembly, transcript, disease, inheritance, review status, and date.

The gene-phenotype graph is a parallel patient-relevance layer, not a
replacement for ACMG/AMP. It should use provenance-backed relationships among:

- variants;
- genes;
- MONDO diseases;
- HPO phenotypes;
- inheritance modes;
- publications and evidence.

Its semantic-similarity result may help evaluate patient relevance and provide
structured input for PP4, but graph similarity alone must never apply PP4 or
upgrade an ACMG/AMP classification.

The first graph implementation should use local/versioned Python data
structures and indexes. A graph database such as Neo4j is not required for the
current one-to-five-variant scope.

## Planning documents created

The following planning documents define the new direction:

- `docs/ACMG_RULES_ENGINE_CHALLENGE_AND_ROADMAP.txt`
  - explains the ACMG/AMP evidence challenge, missing data, acquisition
    options, risks, and proposed solution;
- `docs/ACMG_VARIANT_INTERPRETATION_IMPLEMENTATION_ROADMAP.md`
  - contains the complete Stage 0 through Stage 14 implementation plan,
    including evidence acquisition, ACMG/AMP, gene-phenotype graph, scoring,
    conflict handling, tiered LLM use, reporting, and validation;
- `docs/GENETICS_STUDY_CHECKLIST_FOR_COMPUTER_ENGINEERS.md`
  - contains the project owner's genetics curriculum, project exercises,
    roadmap knowledge gates, and an eight-week study sequence.

These documents are proposals. They do not prove that any planned clinical
feature works.

The roadmap order is:

```text
Stage 0  Requirements and output approval
Stage 1  Evidence contracts
Stage 2  Disease/inheritance/case context
Stage 3  Evidence-source infrastructure
Stage 4  Population and ClinGen CSpec evidence
Stage 5  ClinVar assertions and literature
Stage 6  Gene-phenotype evidence graph
Stage 7  ACMG/AMP engine framework
Stage 8  Automated ACMG/AMP criteria
Stage 9  Manual and mixed ACMG/AMP criteria
Stage 10 ACMG/AMP classification and evidence points
Stage 11 Priority, confidence, and conflict routing
Stage 12 Tiered LLM interpretation
Stage 13 Per-variant frontend and reports
Stage 14 Scientific validation and release
```

Do not skip Stage 0. Before implementation, the professor should approve:

- the exact per-variant output contract;
- the initial germline Mendelian SNV/small-indel scope;
- continued one-to-five filtered-variant input;
- disease and inheritance selection;
- whether genotype, phase, trio, and pedigree data may be collected;
- the ACMG/AMP combination/point framework;
- the meaning and validation plan for clinical priority;
- gene-phenotype graph sources and semantic-similarity method;
- mandatory human review for material unresolved conflicts.

## Current evidence and API-key conclusion

The application currently integrates:

- Ensembl VEP;
- MyVariant.info;
- NCBI ClinVar;
- UCSC GenCC;
- local HPO data.

These sources provide only part of the evidence required for complete
ACMG/AMP interpretation.

The only configured secret API key is `LLM_API_KEY`. More API keys are not the
main solution. The roadmap requires additional evidence and context such as:

- granular gnomAD population evidence;
- individual ClinVar assertions rather than only aggregate labels;
- ClinGen CSpec and Evidence Repository data;
- literature evidence through bounded PubMed/LitVar/PubTator retrieval;
- calibrated computational and splice evidence;
- disease mechanism and transcript context;
- optional patient genotype, inheritance, phase, de novo, and segregation
  data;
- structured manual functional and case evidence.

An NCBI API key may increase request throughput, and licensed OMIM or
commercial data may be optional, but no API key can reconstruct missing
patient or family evidence.

## Validation baseline

The latest completed validation was:

```text
457 passed, 3 skipped
Total coverage: 87.99%
Stage 16 MVP release acceptance: PASSED
Stage 15 secrets audit: PASSED
```

Primary validation commands:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\run_stage16_final_acceptance.py
```

The final acceptance command also checks compilation, installed dependency
consistency, secrets, security controls, the Stage 16 workflow, regression
tests, and the 80% coverage gate.

## Current Git state warning

At the time of this update, the latest code implementation is committed as
`6a3aac5`. The new planning documents and their `.gitignore` allow-list entries
are uncommitted. This handoff update is also uncommitted.

Inspect the current worktree before doing anything:

```powershell
git status --short
git diff --check
```

## Important implementation boundaries

- `app.py` is the application launcher.
- `frontend/ui.py` owns Streamlit presentation and input controls.
- `frontend/execution.py` owns secure upload handling and cancellable jobs.
- `frontend/results.py` owns frontend-safe result tables.
- `backend/vcf_processing.py` owns filtered VCF/manual-row validation.
- `backend/pipeline.py` owns stage orchestration and the public result contract.
- `backend/annotation.py` owns external annotation adapters.
- `backend/phenotype.py` owns HPO data, search, matching, and updates.
- `backend/report.py` owns Evidence Objects, LLM interpretation, and reports.
- `backend/llm.py` owns the provider-neutral LLM client boundary.
- `backend/database.py` owns SQLite persistence.
- `backend/privacy.py` owns prohibited-field and LLM-payload validation.
- `config.py` is the central `.env` configuration boundary.
- `tests/test_pipeline.py` is the main offline regression suite.

The database still uses historical internal names such as
`candidate_variants` and `candidates` for schema compatibility. The public
pipeline and frontend no longer expose a prioritization/candidate-selection
stage.

Proposed future modules in the roadmap, such as `backend/evidence.py`,
`backend/phenotype_graph.py`, `backend/acmg/`, `backend/prioritization.py`,
`backend/confidence.py`, `backend/conflicts.py`, and
`backend/literature.py`, do not exist yet. Do not treat proposed paths as
implemented code.

## LLM configuration rule

The frontend offers pinned and searchable model choices. The selected model is
passed through the provider-neutral LLM boundary. Do not scatter provider SDK
logic or model-specific conditionals across pipeline/report/frontend modules.

Never read, print, log, or commit `.env` credentials.

## Network notes

The user's international connection can be unstable. A live test failure may
be network-related rather than a code defect.

ClinGen GeneGraph previously returned HTTP 403 on the user's network. The
application uses UCSC GenCC as the working ClinGen/GenCC evidence source.

Offline automated tests must not make unmocked HTTP calls.

## Documentation

Keep these synchronized with architecture changes:

- `README.md`
- `docs/STAGE_16_MVP_REQUIREMENTS.md`
- `docs/STAGE_16_MVP_RELEASE_CHECKLIST.md`
- `docs/STAGE_16_MVP_DEMO_RUNBOOK.md`
- `docs/ACMG_RULES_ENGINE_CHALLENGE_AND_ROADMAP.txt`
- `docs/ACMG_VARIANT_INTERPRETATION_IMPLEMENTATION_ROADMAP.md`
- `docs/GENETICS_STUDY_CHECKLIST_FOR_COMPUTER_ENGINEERS.md`
- this `docs/AI_HANDOFF.md`

## Starting prompt for a new chat

Use:

```text
Read docs/AI_HANDOFF.md completely, inspect git status and the latest commit,
then read docs/ACMG_VARIANT_INTERPRETATION_IMPLEMENTATION_ROADMAP.md. Continue
only the single roadmap stage authorized by the user. Break every alteration
into small sequential steps, validate each step, preserve unrelated changes,
keep clinical evidence deterministic and auditable, and provide a Git commit
title after completion.
```
