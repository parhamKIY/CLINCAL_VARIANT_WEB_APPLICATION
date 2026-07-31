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
- Generated clinical reports can be downloaded as Markdown, PDF, or Word.
  PDF and Word files are created locally in memory from the validated
  Markdown report, without additional provider calls or clinical data.
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

## Stage 10 complete pipeline integration: complete

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

Stage 10 step 6 finalizes the integration with a complete offline end-to-end
test that crosses the real processing, prioritization, annotation,
phenotype-matching, Evidence Object, LLM, and report boundaries while mocking
only remote services. A live `--pipeline` smoke mode exercises the same public
`run_analysis()` entry point with configured production services and verifies
the saved report. Stage 10 is complete; the temporary random prioritizer
remains the documented non-clinical MVP limitation.

## Stage 11 Streamlit frontend: complete

Stage 11 establishes the complete Streamlit execution and result-presentation
boundary. The root `app.py` delegates rendering to
`frontend/ui.py`, which configures the
page, loads local responsive styles, displays the clinical decision-support
notice, and presents the analysis workflow without duplicating backend logic.
The interface now accepts either a `.vcf`/`.vcf.gz` upload or a manual
`CHROM:POS:REF:ALT` variant, supports local HPO term search and phenotype
selection, and exposes the coordinated HPO dataset update operation. Input
submission now calls the public `run_analysis()` boundary, provides live
stage-by-stage progress, retains frontend-safe completion or error state across
reruns, and removes temporary uploaded VCF data after execution. The result
dashboard displays bounded candidates, standardized annotations, optional HPO
scores, sanitized Evidence Objects, source statuses, references, and warnings
without exposing VCF genotype fields or raw provider payloads. A generated
Markdown clinical report is loaded only from the configured `REPORT_DIR`,
rendered in the application, and offered as a bounded download without
exposing its server path.

Stage 11 step 6 completes the frontend with automated application tests and
interactive browser verification. The verified paths cover VCF and manual
input selection, empty-input validation, local HPO search and phenotype
selection, the coordinated HPO update control, pipeline result persistence,
candidate and evidence presentation, safe report rendering and download, and
responsive layouts without horizontal overflow at desktop, tablet, and mobile
widths. Remote annotation and LLM calls remain outside the offline UI test
suite and must be checked separately with the live Stage 10 smoke mode.

Run the frontend:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

## Stage 12 SQLite persistence: complete

Stage 12 step 1 adds the versioned SQLite persistence foundation in
`backend/database.py`. `DATABASE_PATH` is configured through `.env`, while
connection creation enables foreign-key enforcement and a bounded busy
timeout. `initialize_database()` creates or validates the versioned analysis,
candidate-variant, Evidence Object, and report tables without overwriting an
unknown or newer schema. Database files and SQLite sidecar files remain
excluded from Git under `storage/database/`.

Stage 12 step 2 adds `save_analysis()` for bounded analysis metadata. It
generates a unique analysis ID and UTC execution time, accepts only known
pipeline statuses, normalizes bounded warnings, and replaces an uploaded VCF
name with an analysis-scoped alias before storage. Manual-variant analyses
store no filename. Parameterized SQL and explicit transactions prevent input
text from changing the database structure, and an existing record is never
silently overwritten.

Stage 12 step 3 adds atomic candidate and Evidence Object persistence.
`save_variants()` stores only bounded chromosome, position, allele, quality,
and filter fields; VCF genotype data is deliberately excluded.
`save_evidence_objects()` passes every item through the existing Stage 7
validation and sanitization boundary before deterministic JSON storage. Both
operations require an existing analysis, reject unknown fields and oversized
collections, preserve collection order, and roll back the complete operation
when any item is invalid.

Stage 12 step 4 adds `save_report()` for secure report references. The
database stores only a portable filename rather than an absolute server path.
The referenced file must be a regular, non-symlinked, bounded UTF-8 Markdown
report directly inside the configured `REPORT_DIR` and must follow the
generated clinical-report filename convention. Missing parent analyses,
outside paths, nested files, duplicate references, empty files, invalid UTF-8,
and oversized reports are rejected without modifying existing records.

Stage 12 step 5 adds `get_analysis()` as the validated retrieval boundary. It
reconstructs metadata, ordered genotype-free candidates, sanitized Evidence
Objects, warnings, and the optional report reference from an analysis ID.
Stored JSON and metadata are revalidated instead of trusted, collection limits
are re-applied during reads, and the portable report filename is resolved only
after confinement and file-integrity checks. Missing analyses and corrupted
records return explicit database errors rather than partial or unsafe data.

Stage 12 step 6 integrates persistence into the public `run_analysis()`
boundary. Every terminal result from valid input is saved as one atomic
transaction containing analysis metadata, genotype-free candidates, sanitized
Evidence Objects, and the optional report reference. The returned pipeline
result exposes its application-generated `analysis_id`. If local persistence
fails, the completed clinical output remains available, internal database
details are not exposed, and a successful result is safely downgraded to
partial with a bounded warning. The live `--pipeline` smoke mode also retrieves
the saved analysis and verifies its report and Evidence Objects.

## Stage 13 comprehensive testing: complete

Stage 13 step 1 completes the unit-test coverage audit. Central configuration
now has explicit tests for environment parsing, numeric limits, path
resolution, directory creation, URL and genome-assembly validation, and
initialization order. The existing unit suites cover VCF processing,
prioritization, annotation parsing, phenotype matching, Evidence Objects, and
LLM error handling. `pytest-cov` provides a repeatable coverage measurement,
with an initial project-wide minimum of 80%.

Stage 13 step 2 adds explicit offline integration tests for the three required
handoffs: VCF processing through multi-source annotation, annotation through
Evidence Object and clinical-report generation, and the complete VCF pipeline
through HPO matching, LLM interpretation, report storage, atomic database
persistence, and validated retrieval. Provider responses and the LLM are
deterministic test doubles, so the integration suite never depends on live
network services.

Stage 13 step 3 enforces mocked service isolation for the complete automated
suite. An automatic test guard fails immediately if any test attempts an
unmocked HTTP request. A degraded-network pipeline test verifies that
MyVariant.info, ClinVar, and ClinGen/GenCC connection failures remain isolated:
validated VEP evidence still reaches a partial clinical report, source statuses
remain explicit, and private transport exception details do not enter
user-facing pipeline output.

Stage 13 step 4 adds `tests/manual_stage13_validation.py` for repeatable live
validation. Its non-identifying matrix covers a known manual variant, missing
phenotype input, and a bounded run over the bundled public Ensembl clinical
VCF. It validates report creation, analysis persistence, and retrieval, then
writes a compact JSON record under `output/`. A temporary timeout override
supports degraded-network testing. Multiple LLM providers are tested by
changing only the provider settings in `.env` and rerunning the same command.

Stage 13 step 5 establishes the final regression and acceptance gate. Eleven
critical checks are marked across public VCF parsing, HPO validation,
multi-source annotation, provider-neutral LLM transport, prompt-injection
containment, report generation, degraded-network behavior, complete pipeline
persistence, database retrieval, and frontend startup.
`tests/run_stage13_acceptance.py` compiles the project, runs this focused
regression set, and then requires the complete offline suite to pass with at
least 80% coverage. Live provider checks remain explicitly separate from the
deterministic acceptance gate.

## Stage 14 logging and error handling: complete

Stage 14 step 1 adds the central logging boundary in
`backend/logging_config.py`. The Streamlit entry point configures one
idempotent application logger with UTC console output and a bounded rotating
UTF-8 log file. `LOG_LEVEL`, `LOG_PATH`, `LOG_MAX_BYTES`, and
`LOG_BACKUP_COUNT` are configurable through `.env`; generated logs remain
excluded from Git. Every application handler redacts the configured LLM API
key, labelled credentials, bearer tokens, nested message arguments, and
exception text before output. Existing annotation logging now uses the central
application logger.

Stage 14 step 2 adds correlated analysis lifecycle logging. Each complete
pipeline run receives a non-identifying application-generated run ID, records
the safe input mode and phenotype count, logs every stage start and terminal
status, and records the final pipeline status and persisted analysis ID.
Context-local run IDs keep concurrent analyses distinguishable and are reset
after completion. Variant coordinates, alleles, HPO identifiers, genes, raw
VCF data, and stage error messages are deliberately excluded from lifecycle
logs.

Stage 14 step 3 adds structured external API telemetry for Ensembl VEP,
MyVariant.info, NCBI ClinVar, and UCSC GenCC. Every request records only the
service, bounded operation name, attempt number, outcome, elapsed
milliseconds, HTTP status, and configured timeout. Timeout, connection, HTTP,
and API-error retries record their safe reason, next attempt, and bounded
delay. URLs, query parameters, coordinates, alleles, genes, provider payloads,
and transport exception text are never logged.

Stage 14 step 4 adds safe operational outcome logging. Variant processing
records total, retained, truncated, and candidate counts; Evidence Object
construction records only its count. Provider-neutral LLM calls record
duration, outcome, message count, token limit, optional usage totals, and
bounded error type without prompts, responses, or provider exception text.
Successful report storage records the managed report directory and an opaque
content-derived report ID, never the coordinate-bearing report filename.

Stage 14 step 5 centralizes user-safe error handling in
`backend/error_handling.py`. Typed pipeline, annotation, evidence, LLM, report,
and unexpected failures are converted to stable error codes, fixed actionable
messages, and explicit recoverability flags before they reach the result
contract. HPO update and phenotype-search failures use the same boundary for
fixed frontend messages. Logs retain only the stage or UI context, public error
code, recoverability, and exception type; internal exception text, paths,
provider responses, patient data, and stack traces remain excluded.

Stage 14 step 6 completes the security audit and acceptance gate. Nested
credential fields are redacted before interpolation, exception logging retains
only the exception class, and annotation-source failure logs use bounded event
fields instead of exception messages. Rotation tests verify that redaction is
preserved across the configured backup files. The `stage14_security` test
marker covers logging configuration, API telemetry, LLM telemetry, correlated
pipeline lifecycle events, centralized public errors, and an end-to-end
internal-failure leak check. `tests/run_stage14_acceptance.py` compiles the
project, runs those focused security checks, and then requires the complete
offline suite to pass with at least 80% coverage.

## Stage 15 security: complete

Stage 15 step 1 adds a repeatable repository secrets audit in
`tests/run_secrets_audit.py`. It verifies that `.env` remains ignored, rejects
tracked environment files, keys, certificates, databases, logs, uploads, and
reports, and scans all non-ignored source files plus every Git patch for
high-confidence provider credentials and private keys. Findings identify only
the scope, path, and rule; detected values are never printed. `.env.example`
remains tracked with an explicit non-secret placeholder.

Stage 15 step 2 hardens the VCF upload boundary before any file is written.
Original filenames are treated as untrusted metadata and must be bounded,
control-character-free basenames ending exactly in `.vcf` or `.vcf.gz`.
Uploaded and gzip-expanded sizes are independently bounded through
`MAX_UPLOAD_BYTES` and `MAX_UNCOMPRESSED_VCF_BYTES`, with absolute
configuration ceilings. Plain and compressed content must match the declared
extension, decode as UTF-8 without NUL bytes, and contain both the VCF
file-format declaration and required column header. Streamlit also rejects
uploads above 25 MB before application execution, while the backend validation
remains authoritative and returns only fixed user-safe errors.

Stage 15 step 3 hardens temporary and persistent storage. Every upload is
written with exclusive-create and no-follow semantics to a randomized private
directory beneath the configured upload root, uses a fixed application
filename instead of user-controlled path text, and is removed after successful
or failed analysis execution. Symbolic-link upload roots, report directories,
database files, and log files are rejected. Application-created private
directories request owner-only `0700` permissions and uploaded VCFs, reports,
databases, and logs request owner-only `0600` permissions on platforms that
support POSIX modes. Report publication remains atomic, while storage paths
remain confined to their resolved configured roots. Windows deployments still
inherit NTFS access control from the configured storage directories and must
apply the institution's production ACL policy before real patient data is
used.

Stage 15 step 4 adds an explicit clinical-data minimization boundary in
`backend/privacy.py`. Genotype and sample fields are removed immediately after
internal candidate selection, so public pipeline variants, annotations,
progress snapshots, frontend state, Evidence Objects, reports, and database
records retain only approved non-sample fields. Pipeline-result validation
recursively rejects genotype, patient, sample, source-filename, raw-VCF, and
raw-provider fields. Before every clinical LLM request, the sanitized Evidence
Object is checked again for prohibited keys, raw VCF headers, labelled patient
or sample identifiers, medical-record identifiers, dates of birth, genotype
labels, and email addresses. The LLM receives only the bounded Evidence Object;
the uploaded filename, raw VCF, sample name, and genotype never cross that
boundary.

Stage 15 step 5 hardens outbound transport and the application runtime. Startup
now validates configuration before the UI is rendered. Every external
bioinformatics and LLM endpoint must use credential-free HTTPS without query
parameters or fragments, and all outbound requests explicitly require TLS
certificate verification. Streamlit binds only to `127.0.0.1` for direct
access, keeps CORS and XSRF protections enabled, disables static serving and
usage telemetry, limits WebSocket messages to 25 MB, hides internal exception
details, and exposes only the minimal toolbar. A production deployment must
place an authenticated HTTPS reverse proxy on the same host in front of this
loopback-only service; Stage 15 does not add authentication by itself.

Stage 15 step 6 completes the offline security review and acceptance gate.
`docs/STAGE_15_SECURITY_ACCEPTANCE.md` records the implemented controls, exact
acceptance criterion, live-test boundary, and remaining production blockers.
`tests/run_stage15_acceptance.py` compiles the project, verifies installed
dependency consistency without network access, runs the repository secrets
audit, executes all focused `stage15_security` checks, and requires the complete
test suite to pass with at least 80% coverage.

## Stage 16 MVP preparation: complete

Stage 16 step 1 completes the MVP requirements audit in
`docs/STAGE_16_MVP_REQUIREMENTS.md`. Every roadmap capability is mapped to its
implementation and offline verification evidence. The audit found no missing
core feature. Dedicated MVP acceptance, fresh live-provider validation, a
professor-facing demo runbook, and the final release decision remain.

Stage 16 step 2 adds `tests/run_stage16_mvp_acceptance.py` as the deterministic
offline MVP gate. The `stage16_mvp` checks cover the real backend stage
boundaries with mocked providers, VCF upload confinement, Streamlit startup,
manual analysis, local phenotype selection, safe missing-input behavior, and
report viewing and download. This gate performs no international network
requests.

Stage 16 step 3 verifies the known public variant through the configured live
annotation and LLM providers. The final baseline completed every required
stage, saved the report, and returned zero errors. UCSC GenCC had no exact
SAMD11 validity claim, so the pipeline correctly returned one explicit
missing-evidence warning and an acceptable partial status.

Stage 16 step 4 adds the presentation-safe `data/samples/mvp_demo.vcf` and the
professor-facing `docs/STAGE_16_MVP_DEMO_RUNBOOK.md`. The runbook contains
preflight commands, the primary VCF workflow, a manual-input fallback, expected
results, the verified live baseline, presentation talking points, and network
troubleshooting. The demo VCF is GRCh38, contains one public variant, and has no
sample or genotype columns.

Stage 16 step 5 completes the MVP release decision.
`tests/run_stage16_final_acceptance.py` compiles the project, checks installed
dependency consistency, runs the secrets audit, executes the Stage 15 security
and Stage 16 MVP checks, and requires the complete suite to pass with at least
80% coverage. `docs/STAGE_16_MVP_RELEASE_CHECKLIST.md` records the
professor-demonstration decision, evidence, assets, pre-presentation checks,
accepted MVP limitations, and production restriction.

## Tests

Run the offline test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run the unit-coverage audit:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --cov=config --cov=backend --cov=frontend --cov-report=term-missing --cov-fail-under=80
```

Run the complete offline Stage 13 acceptance gate:

```powershell
.\.venv\Scripts\python.exe tests\run_stage13_acceptance.py
```

Run the complete offline Stage 14 security acceptance gate:

```powershell
.\.venv\Scripts\python.exe tests\run_stage14_acceptance.py
```

Run the Stage 15 repository secrets audit:

```powershell
.\.venv\Scripts\python.exe tests\run_secrets_audit.py
```

Run the complete offline Stage 15 security acceptance gate:

```powershell
.\.venv\Scripts\python.exe tests\run_stage15_acceptance.py
```

Run the deterministic offline Stage 16 MVP acceptance gate:

```powershell
.\.venv\Scripts\python.exe tests\run_stage16_mvp_acceptance.py
```

Run the final offline Stage 16 MVP release gate:

```powershell
.\.venv\Scripts\python.exe tests\run_stage16_final_acceptance.py
```

Run the complete live Stage 13 manual-validation matrix:

```powershell
.\.venv\Scripts\python.exe tests\manual_stage13_validation.py --case all
```

Run the public sample VCF case only:

```powershell
.\.venv\Scripts\python.exe tests\manual_stage13_validation.py --case sample-vcf
```

Exercise timeout and degraded-network behavior:

```powershell
.\.venv\Scripts\python.exe tests\manual_stage13_validation.py `
  --case known-variant `
  --timeout-seconds 1
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

Run the complete live Stage 10 pipeline from manual variant to saved report:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py --pipeline
```

Custom Stage 10 inputs can be supplied when needed:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py --pipeline `
  --variant "1:941284:G:A" `
  --hpo HP:0001250
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
