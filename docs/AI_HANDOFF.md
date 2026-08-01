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

The manual editor restricts `CHROM` to the 25 standard primary labels
`1`–`22`, `X`, `Y`, and `MT`. Selecting a chromosome displays its valid POS
range for the configured GRCh37 or GRCh38 assembly. Invalid coordinates are
shown immediately and are rejected before analysis. Only POS is
chromosome-dependent: REF and ALT use allele-syntax validation, QUAL is an
optional non-negative value, and FILTER is optional text.

## Latest uncommitted implementation

The manual chromosome and coordinate validation improvement is complete but
has not yet been committed.

Main changes:

- changed manual CHROM cells from free text to a dropdown containing
  `1`–`22`, `X`, `Y`, and `MT`;
- added authoritative GRCh37 and GRCh38 primary chromosome length tables;
- added live, row-specific POS range guidance after CHROM selection;
- added immediate invalid-POS feedback and pre-analysis rejection;
- retained backend validation so the rules cannot be bypassed through the UI;
- documented that REF, ALT, QUAL, and FILTER are not chromosome-length-bound;
- updated offline regression tests and synchronized project documentation.

Suggested commit title:

```text
feat: validate manual chromosome coordinates
```

The preceding filtered-input refactor was committed as:

```text
4a68ff8 refactor: accept filtered VCF tables and remove prioritization
```

## Validation baseline

The latest completed validation was:

```text
456 passed, 3 skipped
Total coverage: 87.87%
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

At the time this handoff was written, the manual chromosome-coordinate
validation change is uncommitted. Inspect the current worktree before doing
anything:

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
