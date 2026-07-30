# Clinical Variant Interpretation

Clinical variant interpretation MVP built with Python and Streamlit.

## Current implementation status

- Central configuration is loaded from `.env` through `config.py`.
- `.vcf` and `.vcf.gz` files are validated and streamed.
- Multi-allelic records are split into one object per ALT allele.
- Genotype is extracted when a sample column exists.
- Manual `CHROM:POS:REF:ALT` input is supported.
- A `bcftools norm` integration point exists for reference-aware
  normalization.
- Normalization is opt-in for the Windows MVP and requires an explicitly
  matching reference FASTA.
- The public VCF processing entry point returns an iterator so large files
  can flow into prioritization without being fully loaded into memory.
- MVP prioritization uses bounded-memory uniform random selection.
- Candidate count defaults to `TOP_VARIANTS` and can be overridden per call.
- Annotation phase one connects candidates to Ensembl VEP in bounded batches.
- Annotation phase two queries exact MyVariant.info records and standardizes
  population-frequency evidence.
- Annotation phase three queries NCBI ClinVar directly and standardizes
  germline classification and review evidence.
- Annotation phase four queries ClinGen-submitted GenCC records through
  the UCSC European REST API and standardizes exact gene-disease validity
  claims.
- External-source failures are isolated so evidence from another source is
  preserved.
- Stage 6 provides local HPO search, normalization, gene and disease
  associations, and explainable phenotype scoring for annotated candidates.
- VCF processing tests are stored in `tests/test_pipeline.py`.

The two current files under `data/samples/` are Ensembl reference VCFs.
They do not contain `FORMAT` or patient sample columns, so their genotype
output is correctly reported as `None`.

## Environment

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Stage 6 phenotype and HPO: complete

Stage 6 uses the official Human Phenotype Ontology OBO release stored at:

```text
data/hpo/hp.obo
data/hpo/phenotype_to_genes.txt
data/hpo/phenotype.hpoa
```

The local loader keeps only active term IDs, alternate IDs, and names in its
lookup index. Obsolete, malformed, and unknown terms are not accepted.
`search_hpo_terms()` provides deterministic local suggestions from canonical
names, synonyms, or HPO identifiers for physician-entered phenotype phrases.
`normalize_phenotypes()` validates multiple selected terms, resolves alternate
IDs, removes canonical duplicates, and preserves the user's selection order.
The phenotype-to-gene loader validates the official table, deduplicates gene
symbols, and returns a deterministic gene list for each existing HPO term.
`calculate_hpo_similarity()` provides an explainable MVP score: the fraction
of the patient's unique HPO terms that are directly associated with a gene.
`match_phenotypes()` adds that score, the canonical patient terms, and the
matched terms to each annotated candidate without modifying source evidence.
The disease-annotation loader returns unique disease identifiers and names
while excluding negated findings and non-phenotypic annotation branches.

The backend provides `update_hpo_data()` for a future user-triggered frontend
update button. It downloads the ontology, phenotype-to-gene table, and disease
annotations from the same official release, validates all three before
installation, rejects downgrades, preserves the previous files, and rolls back
a partial installation. This keeps phenotype terms, gene associations, and
disease annotations release-compatible.

## Stage 7 evidence object: complete

`backend/report.py` defines the versioned `EvidenceObject` contract and its
validation boundary. The schema keeps only standardized variant, annotation,
clinical, phenotype, provenance, and warning fields. Exact-field validation
rejects raw VCF fields, raw API payloads, and unapproved personal data, while
missing evidence remains explicit through `None`, empty lists, and source
statuses. `build_evidence_object()` and `build_evidence_objects()` now convert
Stage 5 and Stage 6 candidates into that contract without mutating the source
objects. They deliberately select only approved fields and reduce nested
ClinVar and ClinGen evidence to compact representations.
`sanitize_evidence_object()` removes control characters, normalizes
whitespace, bounds nested evidence and provenance lists, records visible
truncation warnings, and enforces a 64 KiB serialized-size ceiling. Automated
boundary integration and a complete local smoke test verify the Stage 5,
Stage 6, and Stage 7 handoff without exposing genotype or raw source payloads.

## Stage 8 LLM interpretation: in progress

`backend/llm.py` is the only application-facing LLM boundary. Stage 8 step 1
defines immutable provider-neutral request, response, message, and token-usage
contracts; a single `LLMClient`; the public `call_llm()` function; and
standard configuration, validation, request, authentication, rate-limit,
timeout, and response errors. Application modules must not import a provider
SDK or construct provider-specific request payloads.

Provider selection and connection values are centralized in `.env` through
`LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, and `LLM_TIMEOUT`.
Stage 8 step 2 implements the `openai_compatible` adapter using a non-streaming
`/chat/completions` request. It applies the configured timeout, uses
deterministic temperature by default, converts provider output into the
standard response contract, and maps authentication, permission, rate-limit,
timeout, connection, HTTP, malformed JSON, and malformed response failures to
the public error hierarchy. Changing between OpenAI-compatible services now
requires only changing the `.env` base URL, API key, and model.

## Tests

Run the offline test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run a live request through the production LLM boundary:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py
```

Run the complete local Stage 6 phenotype smoke test:

```powershell
.\.venv\Scripts\python.exe tests\manual_phenotype_smoke.py
```

Run the complete local Stage 7 Evidence Object smoke test:

```powershell
.\.venv\Scripts\python.exe tests\manual_evidence_smoke.py
```

Custom phenotype inputs can be supplied when needed:

```powershell
.\.venv\Scripts\python.exe tests\manual_phenotype_smoke.py `
  --query "seizure" `
  --hpo HP:0001250 `
  --hpo HP:0001263 `
  --gene SCN1A
```

## Windows VCF parser decision

`cyvcf2` has no compatible native Windows wheel for the current Python
3.13 environment and its Windows support is experimental. The MVP uses
`vcfpy`, a tested parser with native Python 3.13 support. The public
functions remain isolated in `backend/vcf_processing.py`, allowing the
parser implementation to be replaced later without changing the
pipeline interface.

## Normalization limitation

Reference-aware normalization is optional during MVP parsing but must be
enabled before clinical annotation whenever reference-aware left alignment
or representation normalization is required. It needs:

- a matching reference genome FASTA;
- `bcftools` available on `PATH`.

`bcftools` is not installed in the current Windows environment, so the
normalization command is implemented and error-tested but has not yet
been executed against a reference FASTA.

## Temporary prioritization strategy

`backend/prioritization.py` currently uses reservoir sampling. This keeps
memory usage bounded while giving each parsed variant an equal chance of
selection.

Random selection is only an MVP placeholder. It is not a clinically valid
ranking algorithm. The pipeline calls only `prioritize_variants()`, so the
internal strategy can later be replaced with configurable frequency,
functional-impact, phenotype, gene-disease, and inheritance scoring without
changing the pipeline interface.

## Stage 5 annotation: complete

`backend/annotation.py` currently integrates Ensembl VEP, MyVariant.info,
NCBI ClinVar, and ClinGen-submitted UCSC GenCC evidence:

- explicit GRCh37/GRCh38 assembly configuration;
- POST batching with Ensembl's 200-variant maximum;
- exact MyVariant chromosome, position, REF, ALT, and assembly matching;
- direct ClinVar ESearch and ESummary queries using exact assembly-specific
  HGVS identifiers;
- NCBI E-utility requests limited to fewer than three requests per second;
- request timeout and limited retries for transient failures;
- structured per-variant error output without stopping the whole pipeline;
- cleaned transcript, gene, consequence, impact, HGVS protein change, and
  source-reference fields;
- standardized gnomAD, ExAC, and exact-ALT dbSNP population frequencies;
- standardized ClinVar VCV/RCV/SCV accessions, germline clinical
  significance, review status, evaluation date, and conditions;
- exact UCSC assembly, coordinate, ClinGen submitter, and gene-symbol
  matching with standardized disease, classification, inheritance,
  criteria URL, PMID, and report fields;
- unified multi-source output with independent failure isolation;
- no raw API response is passed to later pipeline stages.

Run live Ensembl VEP, MyVariant.info, NCBI ClinVar, and ClinGen checks
through the production annotation path:

```powershell
.\.venv\Scripts\python.exe tests\manual_annotation_smoke.py
```

An optional manual variant can be supplied in `CHROM:POS:REF:ALT` format.
