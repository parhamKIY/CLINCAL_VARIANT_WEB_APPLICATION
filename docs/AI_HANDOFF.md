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

The Streamlit manual input is a fixed five-row editor with:

```text
CHROM | POS | REF | ALT | QUAL | FILTER
```

`CHROM`, `POS`, `REF`, and `ALT` are required for each used row. `QUAL` and
`FILTER` are optional.

## Latest uncommitted implementation

The filtered-input refactor is complete but has not yet been committed.

Main changes:

- removed `backend/prioritization.py`;
- removed `TOP_VARIANTS` from `config.py` and `.env.example`;
- simplified `backend/vcf_processing.py` for strict one-to-five-row input;
- added `parse_manual_variants()` for manual VCF-style rows;
- removed sample selection, genotype extraction, large-file preview limits,
  random selection, and the unused `bcftools` normalization path;
- removed the prioritization stage and candidate-preview fields from the
  public pipeline result;
- renamed the public processing boundary to `run_variant_processing()`;
- changed annotation to process every supplied variant directly;
- changed the frontend from one coordinate text field to a five-row editor;
- added strict upload row-count validation before temporary storage;
- renamed the frontend result view to **Input variants**;
- renamed the submit control to **Analyze variants**;
- updated smoke scripts, tests, README, and Stage 16 documentation;
- removed obsolete large sample files:
  - `data/samples/homo_sapiens-chrMT.vcf.gz`
  - `data/samples/homo_sapiens_clinically_associated.vcf.gz`
- retained `data/samples/mvp_demo.vcf` as the valid one-row demonstration file.

Suggested commit title:

```text
refactor: accept filtered VCF tables and remove prioritization
```

## Validation baseline

The latest completed validation was:

```text
451 passed, 3 skipped
Total coverage: 87.94%
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

When testing the current uncommitted deletion set, the secrets audit sees
deleted tracked files until those deletions are staged. Do not weaken the
audit to work around this. Once the refactor is committed, the normal
acceptance command works directly.

## Current Git state warning

At the time this handoff was written, the filtered-input refactor is
uncommitted. Inspect the current worktree before doing anything:

```powershell
git status --short
git diff --check
```

There is also an unrelated untracked generated file:

```text
output/pdf/clinical-report-grch38-mt-5791-g-a-e5ccf339d531ec1c.pdf
```

Treat it as user-owned output. Do not delete, modify, stage, or commit it unless
the user explicitly asks.

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
- this `docs/AI_HANDOFF.md`

## Starting prompt for a new chat

Use:

```text
Read docs/AI_HANDOFF.md completely, inspect git status and the latest commit,
then continue the project. Break every requested alteration into small
sequential steps, validate each step, preserve unrelated changes, and provide
a Git commit title after completion.
```
