# Stage 19 — Repository Reality Check

Status: complete  
Audit date: 2026-08-02  
Branch: `master`  
Baseline commit: `0662075 plan changed to use api`

## Purpose

This audit records the implementation that exists before the API-first
continuation begins. Current code is the source of truth when an older
guideline or README statement differs from the repository.

No API-first feature was implemented in this stage.

## Verified baseline

The worktree was clean before this documentation change.

The Stage 16 final acceptance command completed successfully:

```powershell
.\.venv\Scripts\python.exe tests\run_stage16_final_acceptance.py
```

Result:

- Python compilation passed.
- Installed dependency consistency passed.
- Repository secrets audit passed.
- Stage 15 security controls: `51 passed, 3 skipped`.
- Stage 16 workflow: `11 passed`.
- Complete regression: `457 passed, 3 skipped`.
- Coverage: `87.99%`, above the required `80%`.
- Stage 16 MVP release acceptance passed.

## Current implementation status

| Component | Reality-check status | Current implementation | API-first continuation |
|---|---|---|---|
| `config.py` | DONE — MODIFY | Central `.env` → `config.py` boundary; explicit assembly; VEP, MyVariant, direct ClinVar, UCSC GenCC, HPO, LLM, storage, logging, and request settings | Add only the new provider, feature-flag, and light/strong LLM settings required by later stages |
| `backend/vcf_processing.py` | DONE — MODIFY | Uses `vcfpy`; validates `.vcf`/`.vcf.gz` and manual rows; accepts one to five source rows; splits multi-allelic ALT values; removes sample/genotype data | Migrate the product contract to exactly five independently analyzed input variants without reintroducing ranking |
| `backend/prioritization.py` | BYPASS | File is absent and the pipeline contains no prioritization import or stage | Keep ranking and `Top-N` out of the main workflow; do not recreate this module without a separate need |
| `backend/annotation.py` | DONE — MODIFY | Annotates every supplied variant with isolated VEP, MyVariant, direct ClinVar, and UCSC GenCC requests; returns standardized output without raw payloads | Harden VEP metadata; add GeneBe; upgrade ClinGen/CSpec handling; preserve direct ClinVar as an independent source |
| `backend/phenotype.py` | DONE — MODIFY | Local, searchable HPO ontology; synonym/obsolete-term handling; versioned data updates; exact HPO-to-gene/disease associations; explainable local overlap score | Preserve the HPO selector contract, replace main phenotype scoring with Phen2Gene, and add Monarch API evidence |
| `backend/llm.py` | DONE — MODIFY | One provider-neutral `LLMClient` and public `call_llm()` boundary with validation, timeout, error mapping, and bounded telemetry | Preserve the boundary while adding logical LLM-1/LLM-2 roles and deterministic routing inputs |
| `backend/report.py` | DONE — MODIFY | Evidence schema `1.0`; one bounded Evidence Object per annotated allele; validated interpretation prompt; one detailed clinical report | Add Evidence Object V2, lineage/conflict fields, per-variant final interpretations, and a second final-only report view |
| `backend/pipeline.py` | DONE — MODIFY | No prioritization stage; every supplied allele reaches annotation, phenotype matching, and Evidence Object construction in input order | Enforce the five-variant contract; isolate failures per variant/provider; add lineage, conflict audit, conditional enrichment, routing, and both report outputs |
| `backend/database.py` | VERIFY | Versioned SQLite persistence stores terminal analysis metadata, genotype-free variants, Evidence Objects, warnings, and a report reference | Migrate only if V2 provenance, conflict, routing, and final-output fields cannot remain safely inside existing JSON contracts |
| `frontend/ui.py` | DONE — MODIFY | Streamlit upload/manual input, five manual rows, assembly-aware coordinate validation, searchable local HPO selection, progress, evidence display, and report downloads | Require a clear five-variant workflow, retain detailed evidence, and add conflict plus final-interpretation-only views |
| `tests/test_pipeline.py` | DONE — MODIFY | Single comprehensive offline suite covering Stages 0–16 with mocked external providers and security/acceptance gates | Preserve regression coverage and add provider, lineage, conflict, routing, exactly-five, and dual-output tests incrementally |

## Important contract findings

### Already aligned

- There is no active candidate ranking, quality ranking, or `Top-N` stage.
- Every valid supplied allele is annotated and receives an Evidence Object.
- Variant order is preserved.
- Pathogenicity evidence is not numerically combined with the phenotype score.
- Direct ClinVar evidence is already separated from other annotation sources.
- External annotation failures are isolated by source.
- Raw API responses and raw VCF data are not sent to the LLM.
- Configuration is centralized.
- Security, privacy, error handling, persistence, and regression foundations are
  implemented and verified.

### Requires migration

- Input currently accepts one to five source rows; the target contract requires
  exactly five variants.
- Multi-allelic rows can produce more than five allele objects after splitting;
  the target meaning of “exactly five variants” must be enforced at the
  allele-level contract.
- Evidence Objects are built for all alleles, but only the first Evidence
  Object is sent to the LLM and receives a saved report.
- Only one detailed report exists; there is no final-interpretation-only report.
- GeneBe, Phen2Gene, Monarch, CSpec-aware context, evidence lineage, a
  deterministic conflict auditor, conditional enrichment, and LLM routing do
  not exist.
- The current local phenotype overlap score is not the target Phen2Gene
  evidence contract.
- The LLM configuration exposes one model, not light and strong logical roles.
- Provider version and retrieval metadata are not yet uniform across all
  evidence sources.

### Documentation corrections

- The current Windows implementation uses `vcfpy`, not `cyvcf2`.
- Reference-aware normalization and clinical filtering are upstream
  responsibilities in the current product contract; no local `bcftools`
  normalization stage exists in this repository.
- `backend/prioritization.py` is absent by design and must not be described as
  an active foundation.
- The existing ClinGen integration is UCSC GenCC evidence. It is not yet a
  complete ClinGen/CSpec rule-specification integration.
- `docs/AI_HANDOFF.md` is not present in the current checkout.

## Current and target workflows

Current:

```text
1–5 filtered source rows
→ allele validation and ALT splitting
→ VEP + MyVariant + direct ClinVar + UCSC GenCC
→ local HPO matching
→ Evidence Object 1.0 for every allele
→ first allele only: one LLM interpretation
→ one detailed report
→ optional SQLite persistence
```

Target:

```text
exactly 5 variants
→ validate and normalize
→ analyze all 5 without ranking
→ VEP + GeneBe + direct ClinVar + ClinGen/CSpec
→ Phen2Gene + Monarch
→ Evidence Object V2 + lineage
→ deterministic conflict audit
→ optional conditional enrichment
→ LLM-1 or LLM-2
→ detailed report + final-interpretation-only report for all 5
```

## Stage 19 exit decision

The repository has a stable and verified Stage 16 foundation. The API-first
continuation must be implemented as additive, bounded migrations rather than a
rewrite.

The next authorized stage is Stage 20, Product Contract Migration. Its first
implementation slice should define and test the exact five-variant
allele-level input contract before changing annotation or adding any provider.
