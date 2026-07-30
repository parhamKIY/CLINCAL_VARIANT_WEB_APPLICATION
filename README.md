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

## Stage 8 LLM interpretation: complete

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

Stage 8 step 3 adds a versioned clinical-interpretation prompt builder in
`backend/report.py`. It accepts only a complete Stage 7 Evidence Object,
validates and sanitizes it again, serializes it deterministically as bounded
JSON, and places it between explicit data delimiters. The fixed system rules
prohibit outside medical knowledge, invented evidence, independent ACMG/AMP
classification, definitive diagnosis, treatment advice, and unsupported
citations. They require explicit uncertainty, source attribution, limitations,
and qualified professional review. Values inside the Evidence Object are
treated as untrusted data rather than instructions.

Stage 8 step 4 connects that prompt to the provider-neutral client through
`generate_clinical_interpretation()`. The production path always validates the
Evidence Object before making an LLM request and fixes interpretation
temperature at `0.0`. Automated tests cover successful standardized responses,
invalid evidence before network access, provider authentication, rate-limit,
timeout, request and response failures, partial source evidence, and rejection
of provider-specific response payloads.

Stage 8 step 5 provides configuration-only, basic live connectivity, and full
synthetic clinical-interpretation smoke modes in `tests/manual_llm_smoke.py`.
The clinical mode sends no raw VCF, genotype, patient identifier, or real
patient record. It verifies all required interpretation sections and rejects
URLs and selected identifier expansions that were not present in the supplied
Evidence Object. It also requires the fixed clinical decision-support notice.

For another OpenAI-compatible service, restart the application after changing
only these `.env` values:

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://provider.example/v1
LLM_API_KEY=your-api-key
LLM_MODEL=model-name
LLM_TIMEOUT=30
```

## Stage 9 clinical report generation: complete

Stage 9 step 1 defines a versioned, JSON-safe `ClinicalReport` contract in
`backend/report.py`. It fixes the nine-section display order for case summary,
variant summary, gene and consequence, clinical evidence, phenotype
correlation, interpretation, limitations, references, and medical disclaimer.
Exact-field validation excludes raw VCF sample fields and unapproved patient
data. The contract records evidence and prompt versions, model provenance,
assembly, the minimal variant, bounded traceable references, warnings, and the
approved medical disclaimer without adding timestamps or storage concerns.

Stage 9 step 2 treats LLM Markdown as untrusted input. It normalizes line
endings and hidden control characters, enforces the exact seven-section LLM
output structure and fixed decision-support notice, rejects empty or oversized
sections, code fences, and raw HTML, and blocks URLs plus HPO, MONDO, ClinVar,
and PMID identifiers that are absent from the validated Evidence Object. The
live `--clinical` smoke mode now passes provider output through this same
production validator.

Stage 9 step 3 composes the validated `ClinicalReport` and renders deterministic
Markdown. Case, variant, gene, consequence, clinical evidence, phenotype,
source status, warnings, references, and disclaimer content are built directly
from the sanitized Evidence Object. Only the validated interpretation and
limitations narratives come from the LLM. References are normalized and
deduplicated, missing evidence is explicit, evidence values are Markdown
escaped, and nested unsafe headings, code fences, or raw HTML are rejected.

Stage 9 step 4 saves rendered reports as deterministic UTF-8 Markdown under the
configured `REPORT_DIR`. Filenames contain bounded assembly and variant slugs
plus a SHA-256 content identifier. Publication uses a fully written temporary
file and an atomic no-overwrite link, so repeated saves are idempotent while a
different pre-existing file is never replaced. Paths cannot escape the report
directory, output size is bounded, and failed publication removes temporary
files.

Stage 9 step 5 provides `generate_and_save_clinical_report()` as the complete
Evidence Object to saved-report boundary for the future pipeline. Offline
integration tests cover candidate conversion, LLM interpretation, validation,
composition, rendering, idempotent storage, sensitive-field exclusion, and
failure before file creation. The live `--report` smoke mode performs the same
path with bundled synthetic evidence and saves the result under `REPORT_DIR`.

## Stage 10 complete pipeline integration: in progress

Stage 10 step 1 defines the public analysis-input and frontend-result contracts
in `backend/pipeline.py`. Exactly one VCF path or manual variant is accepted,
while phenotype selections are bounded and normalized without touching the
filesystem. The versioned result retains variants, candidates, annotations,
phenotype results, Evidence Objects, report path, warnings, and structured
errors. Stable stage ordering and progress states are JSON-safe and exclude
exception objects and stack traces.

Stage 10 step 2 connects mutually exclusive VCF or manual input to the existing
streaming VCF processor and bounded MVP prioritizer. The complete processed
stream is evaluated by reservoir sampling, while only the first 100 parsed
variants are retained for frontend display to prevent unbounded memory use.
The total parsed count and truncation state remain explicit, candidate output
is copied into the pipeline result, and successful processing advances the
stable progress contract to the annotation stage.

Stage 10 step 3 connects selected candidates to the existing Ensembl VEP,
MyVariant.info, NCBI ClinVar, and ClinGen/GenCC annotation boundary, then
passes the standardized annotations into HPO gene matching. Source-level
annotation warnings remain visible without discarding other source results.
When no phenotypes are supplied, matching is explicitly marked as skipped and
the annotated candidates continue unchanged to the Evidence Object stage.

Stage 10 step 4 adds the public `run_analysis()` happy path. It builds and
retains one bounded Evidence Object per enriched candidate, sends only the
leading candidate's sanitized Evidence Object through the provider-neutral LLM
boundary, validates the response, and atomically saves its deterministic
Markdown report. The result then exposes the saved report path and reaches
100 percent completion. Until clinical prioritization replaces the documented
random MVP selector, the leading candidate must not be interpreted as a
clinically ranked result.

Stage 10 step 5 makes `run_analysis()` a frontend-safe execution boundary.
Expected input, VCF, prioritization, annotation, Evidence Object, LLM, and
report failures are converted into bounded structured issues without exposing
exception objects or stack traces. Completed stage outputs remain available,
later stages are explicitly marked as skipped, and recoverable failures return
partial results. HPO data or matching failures are recoverable: annotation
continues into evidence and reporting without phenotype scores. Annotation
source warnings also remain visible and produce an explicit partial result.

## Tests

Run the offline test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run a live request through the production LLM boundary:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py
```

Run the complete live Stage 8 interpretation using bundled synthetic evidence:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py --clinical
```

Run the complete live Stage 9 report workflow and save its Markdown output:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py --report
```

Validate configuration without sending an LLM request:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py --config-only
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
