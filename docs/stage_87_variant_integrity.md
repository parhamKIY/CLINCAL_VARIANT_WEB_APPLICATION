# Stage 87 - Variant Cardinality and Identity Integrity Gate

## Implemented invariant

Every accepted allele now receives an immutable zero-based `input_index` before it
enters annotation. The index is retained in the minimized pipeline variant and its
nested annotation/phenotype variant identity. Existing interpretation, draft, and
review contracts retain their established `variant_index`; the integrity gate proves
that each value is the same original input index. `ReportData V4` retains the field as
`input_index`.

Pipeline schema `3.1` persists one `VariantIntegrityRecord V1` per accepted allele.
Each record binds the input index to:

- parser identity;
- normalized identity;
- pipeline identity;
- Evidence Object identity;
- Draft Variant Report ID;
- Stage 86 review lifecycle record ID.

Allele identities are assembly-qualified SHA-256 digests of chromosome, position,
reference, and alternate allele. They remain allele-specific and deterministic while
avoiding a new concatenated-coordinate string in frontend-safe error snapshots.

## Cardinality gate

For a completed seven-variant analysis, the required counts are all seven:

```text
parser = normalized = pipeline = evidence package = draft report = review record
```

The pipeline rejects any non-empty annotation, phenotype, evidence, draft, or review
collection whose cardinality no longer matches the accepted input. It also rejects
reordering, index duplication, allele drift, or mismatched draft/review identities.
Provider-specific success and `not_found` counts may remain lower without removing an
allele from the workflow.

Two variants annotated to the same gene remain independent because identity never
uses the gene symbol. Original input order controls all downstream linkage.

## Persistence and compatibility

Pipeline-state migration upgrades schemas `2.8`, `2.9`, and `3.0` without rerunning
providers or interpretation. It assigns indices from stored candidate order and
reconstructs the integrity ledger from existing evidence, draft, and report lifecycle
records. Older in-memory test fixtures may leave the ledger empty; production analyses
create it as soon as parser output is accepted.

## Acceptance evidence

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m stage87_variant_integrity
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\run_stage60_acceptance.py
```

Validated on 2026-08-11:

- Stage 87 gate: `2 passed`;
- dedicated seven-variant end-to-end scenario: all six cardinalities equal `7` after
  persistence and reload;
- duplicate-gene scenario: seven distinct allele identities remain seven reports;
- full repository suite: `988 passed, 4 skipped`;
- Stage 60 release gate: passed at `85.78%` coverage.

Stage 88 subsequently added the interpretation-error taxonomy and structured
failure observability; it did not alter this integrity gate.
