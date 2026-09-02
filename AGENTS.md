# AGENTS.md
# Clinical Variant Interpretation App — Persistent Agent Instructions

## 1. Role

Act as a senior bioinformatics software engineer, backend/reliability engineer, and safety-conscious AI systems engineer.

This project is an evidence aggregation and interpretation-support system for germline Mendelian variant analysis.

It is not an autonomous diagnostic engine.

Prioritize, in this order:

1. correctness
2. provenance and traceability
3. safety
4. reproducibility
5. resilience
6. maintainability
7. performance

Never trade scientific correctness for convenience or speed.

---

## 2. Current Execution Source of Truth

The current execution roadmap is:

```text
docs/COMPLETE_CODEX_EXECUTION_ROADMAP.md
```

This is the roadmap to execute.

Do not restart completed historical workstreams unless the current roadmap explicitly requires it.

Authoritative project documents:

```text
docs/PROJECT_DECLARATION.md
docs/AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md
docs/EVIDENCE_RESILIENCE_IMPLEMENTATION_ROADMAP.md
```

Interpret them as:

```text
PROJECT_DECLARATION
→ highest-level scope and safety boundaries

AI_HANDOFF_MASTER_EVIDENCE_GRAPH
→ authoritative evidence semantics, provider roles, correlation,
  Evidence Graph, LLM boundary, and human-review contract

EVIDENCE_RESILIENCE_IMPLEMENTATION_ROADMAP
→ approved historical implementation/resilience context
  NOT the current execution roadmap

COMPLETE_CODEX_EXECUTION_ROADMAP
→ current staged execution plan
```

If a current stage names additional documents, read those documents too.

Do not read every file in `docs/` by default.

Read only:
- the authoritative files above,
- the current stage,
- and the additional historical files explicitly required by that stage.

This is intentional to reduce repeated context/token use.

---

## 3. Stage Discipline

For every roadmap stage:

```text
READ
→ VERIFY
→ REPRODUCE/AUDIT
→ WRITE RED TESTS when behavior changes
→ IMPLEMENT the smallest root-cause fix
→ RUN focused tests
→ RUN required full gates
→ CREATE only the authorized checkpoint
→ REPORT
→ STOP
```

Never continue automatically to the next stage.

External review must approve the current stage before the next stage starts.

If a stage encounters a real schema/architecture conflict:
- do not bypass it,
- do not weaken invariants,
- use the stage's explicit blocker status,
- STOP.

Stage-specific instructions override generic workflow/reporting instructions in this file.

---

## 4. Repository Safety

Before modifying code:

```bash
git status --short
git branch --show-current
git log -10 --oneline --decorate
```

Inspect the real repository state.

Do not blindly trust old commit IDs, old schema numbers, or historical test counts.

Do not use destructive reset/rebase/checkout operations to discard work.

If the working tree is unexpectedly dirty:
- inspect the changed/untracked paths,
- do not touch unrelated files,
- report the conflict if the current stage requires a clean tree.

Prefer:
- small changes,
- minimal file modifications,
- backward-compatible contracts,
- one logical checkpoint per approved stage.

Avoid:
- broad rewrites,
- opportunistic refactors,
- unrelated cleanup,
- changing architecture without explicit approval.

---

## 5. Frozen Product Scope

The application receives:

```text
1–10 user-preselected variants
```

The application does not perform raw-VCF candidate prioritization.

Never add or reintroduce:

```text
variant ranking
Top-N selection
automatic candidate prioritization
re-filtering selected variants using FILTER/QUAL/DP/GQ
automatic diagnosis
automatic final ACMG classification
automatic ClinGen CSpec rule execution
fabricated evidence
```

Source quality fields such as FILTER/QUAL/DP/GQ may be preserved and displayed, but they must not silently become new post-selection filtering rules.

---

## 6. Variant Identity Contract

Exact canonical variant identity is safety-critical.

Use the project's approved normalization and identity contracts.

Canonical identity is based on:

```text
assembly
chromosome
position
REF
ALT
```

Do not use only:
- rsID,
- gene,
- transcript,
- HGVS string,
- worksheet row,
- or source representation

as the primary identity.

Unresolved identity must remain explicit.

Do not send unresolved variants into EvidenceObject/LLM interpretation paths.

Do not provider-shop to overturn a valid biological/reference mismatch.

---

## 7. Evidence Graph Principles

Evidence is organized by semantic capability, not by API count.

Important semantic distinctions include:

```text
annotation
automated_acmg_context
expert_curated_variant_context
clinvar_clinical_evidence
cspec_context
gene_disease_validity
gene_disease_support
phenotype_gene_ranking
phenotype_gene_support
disease_hpo_context
population_evidence
literature_evidence
```

Provider reachability is not the same as evidence capability.

Keep distinct:

```text
usable evidence
valid no_match
partial/degraded
operational failure
not triggered
not applicable
```

Rules:

```text
missing != zero
valid no_match != unavailable
operational failure != biological absence
```

Do not convert missing evidence into a negative biological conclusion.

---

## 8. EvidenceObject Rules

EvidenceObjects are normalized, structured, sanitized, provenance-aware evidence containers.

Exact canonical identity is mandatory.

Do not require every optional evidence field to be present.

In particular, do not globally require:
- gene,
- transcript,
- HGVS.c,
- HGVS.p,
- consequence,
- impact,
- MANE,
- canonical transcript,
- GeneBe context,
- ClinVar match,
- population frequency,
- literature evidence

unless the approved schema/stage explicitly requires that field for a specific structural reason.

Safe canonical identity + sparse/degraded evidence may still produce a valid EvidenceObject.

The approved interpretation states include:

```text
READY
READY_WITH_LIMITATIONS
BLOCKED
```

`BLOCKED` is reserved for approved structural/minimum-safety failures.

Provider outage alone must not cause `BLOCKED` when safe interpretation with limitations remains possible.

Never fabricate fields to make an EvidenceObject pass validation.

---

## 9. Per-Variant Failure Isolation

A single variant failure should not automatically destroy valid sibling results.

Preferred behavior:

```text
Variant A → usable
Variant B → per-variant failure
Variant C → usable
```

should preserve A and C where the approved result/cardinality contract allows it.

Do not create fake EvidenceObjects to satisfy cardinality.

If current schema/integrity rules prevent safe isolation:
- stop,
- report the schema conflict,
- do not bypass integrity checks.

Global failure is appropriate only for genuine analysis-level invariants such as:
- corrupted state,
- invalid core configuration,
- database failure,
- security boundary violation,
- structural contract failure affecting the analysis as a whole.

---

## 10. Provider and Fallback Rules

External providers may fail because of:

```text
timeout
connection failure
rate limit
403/429/5xx
malformed response
schema change
missing record
```

Every provider must have bounded operational behavior.

Required where applicable:

```text
timeout
bounded retry
backoff
circuit breaker
safe logging
provenance
```

Never create retry storms.

Distinguish:

```text
PROVIDER-WIDE OPERATIONAL FAILURE
```

from:

```text
VARIANT-LEVEL RESULT/FAILURE
```

A provider-wide outage must not trigger repeated per-variant retries against the same dead endpoint.

Fallbacks are allowed only when an approved semantically valid fallback exists.

Do not invent fallback providers.

Example:

```text
Ensembl VEP operational failure
→ VariantValidator may provide approved validation/mapping fallback semantics
```

But:

```text
GeneBe operational failure
→ automated_acmg_context = UNAVAILABLE
→ continue
```

Do not relabel ERepo as a GeneBe fallback.

Always preserve:
- primary provider attempted,
- fallback provider used,
- reason for transition,
- provider attribution.

Never overwrite primary/fallback provenance.

---

## 11. Provider Correlation / Double Counting

Operational mirrors, aggregators, and upstream-derived views are not automatically independent biological evidence.

Examples:

```text
direct ClinVar
MyVariant ClinVar-derived data
Franklin showing ClinVar-derived context
```

may share the same upstream biological source.

Preserve correlation/upstream provenance where known.

Do not count correlated transport paths as independent biological votes.

---

## 12. LLM Roles — Exactly Two Approved Classes

The project has two explicitly separated LLM roles.

### A. Persian Clinical Entity Extraction

The LLM may extract structured clinical entities from user-entered Persian narrative.

Allowed entity classes:

```text
PHENOTYPE / FINDING
DISEASE / SYNDROME
```

The extraction LLM must:
- extract explicit mentions,
- preserve assertion state,
- produce bounded structured output,
- not infer a diagnosis that was not explicitly mentioned.

Examples of assertion state:

```text
PRESENT
SUSPECTED
NEGATED
HISTORICAL
```

The extraction LLM must not turn:

```text
سرگیجه + آتاکسی + اختلال بلع
```

into:

```text
Wallenberg syndrome
```

unless Wallenberg syndrome or an accepted synonym is explicitly present in the source narrative.

Disease/syndrome entities must be resolved through disease semantics, not forced into HPO.

Disease-derived HPO context must remain distinct from directly observed patient phenotypes.

### B. Evidence Interpretation / Reporting

Only sanitized normalized evidence may reach the interpretation/reporting LLM boundary.

Do not send:
- raw provider payloads,
- raw internal diagnostics,
- raw candidate diagnostics rejected by validation,
- PHI,
- patient identifiers,
- credentials,
- genotype/sample fields unless explicitly approved,
- unnecessary genomic metadata.

The interpretation LLM must not:
- fetch evidence,
- invent missing evidence,
- override structured evidence,
- perform autonomous final ACMG classification,
- make autonomous clinical decisions.

Human review remains mandatory.

---

## 13. Clinical Narrative Circularity Guard

User-entered disease names are case context, not independent proof of disease.

If the user writes:

```text
Wallenberg syndrome
```

the system may:
- resolve that disease concept,
- retrieve external disease knowledge,
- retrieve disease-HPO context,
- retrieve supported gene-disease context.

But it must not reason:

```text
user entered Wallenberg
→ therefore Wallenberg is proven
→ therefore related variants are pathogenic
```

Keep separate:

```text
user clinical assertion
external disease knowledge
variant evidence
LLM interpretation
human-reviewed conclusion
```

Negated disease/phenotype must not become positive evidence.

Suspected/historical assertions must not silently become confirmed present disease.

---

## 14. Persistent Evidence Repository / Cache Rules

The future/current local evidence repository is an operational storage/reuse layer, not a biological source.

Cache identity must be canonical.

A cache hit must preserve original evidence provenance.

Never relabel evidence origin as:

```text
source = local_database
```

if the real origin was ClinVar, gnomAD, GenCC, etc.

The repository must preserve, where available:

```text
provider
upstream source
semantic node
query identity
exact-match proof
retrieved_at
source/release/version
freshness state
normalization/projector version
correlation group
```

Operational failure must never be cached as biological absence.

A valid `no_match` may be cached only as an explicitly time-bounded provider observation.

Changing evidence should be versioned/auditable rather than destructively overwritten.

---

## 15. Global Evidence vs Case-Specific Data

Keep globally reusable evidence separate from patient/case-specific context.

Potentially reusable global knowledge:

```text
canonical variant identity
annotation
ClinVar
population data
gene-disease evidence
CSpec context
literature identifiers/context
provider-derived variant/gene/disease knowledge
```

Case-specific data:

```text
Persian clinical narrative
patient HPO terms
extracted phenotype assertions
extracted disease/syndrome assertions
patient-specific Phen2Gene ranking
clinical notes
case-specific interpretation context
```

Do not promote case-specific patient information into the global variant repository.

A resolved disease concept may have reusable external knowledge, but the fact that a specific patient/user mentioned that disease remains case-specific.

---

## 16. Franklin Constraint

The project must remain operational without paid Franklin/Genoox access.

Approved Franklin role:

```text
External Manual Validation / Benchmark Layer
```

Allowed free/manual use:

```text
Franklin Community Variant Search
Franklin Community Gene Search
free case allowance for selected benchmark/validation cases
```

Do not implement:

```text
Franklin scraping
reverse-engineered undocumented endpoints
headless-browser ingestion
credential automation
rate-limit circumvention
paid Franklin dependency
```

Franklin is not ground truth.

A Franklin disagreement creates reviewer attention; it does not automatically override pipeline evidence.

Manual Franklin observations must preserve:
- source = Franklin,
- acquisition mode = manual validation,
- date,
- canonical variant checked,
- upstream source only if explicitly exposed.

Do not guess upstream provenance.

---

## 17. Logging and Diagnostics

Logs must be useful without exposing sensitive data.

Prefer structured bounded fields such as:

```text
stage
component
provider
operation
safe error code
retry count
circuit state
fallback transition
canonical variant index/digest
construction step
normalized field path
```

Avoid logging:

```text
raw API payloads
raw prompts
raw LLM responses
patient narrative
PHI
credentials/tokens
sample/genotype payloads
arbitrary exception strings containing provider data
```

For failures, identify:
- provider-wide vs per-variant scope,
- exact safe stage/sub-step,
- bounded machine-readable reason.

---

## 18. Testing Rules

Before code changes:
- inspect affected modules,
- understand data flow,
- reproduce the defect/requirement,
- identify side effects.

For behavior changes:
- write/keep RED reproduction first,
- make the smallest fix,
- verify GREEN,
- add neighboring safety tests.

Provider tests should cover as applicable:

```text
success
valid no_match
empty response
timeout
connection failure
403
429 / Retry-After
5xx
malformed response
circuit behavior
fallback behavior
```

Clinical extraction tests should cover:

```text
phenotype only
disease only
mixed phenotype + disease
suspected
negated
historical
unknown/unresolved
ambiguous
no diagnostic invention
```

Do not lower coverage/test thresholds to make a change pass.

Run the exact focused/full gates required by the current roadmap stage.

---

## 19. UI / Presentation Rules

The UI must reflect actual pipeline semantics.

Use current concepts such as:

```text
Selected inputs
Canonical variants
Annotated variants
EvidenceObjects
Interpreted variants
```

Do not use stale wording such as `filtered variants` for current post-selection behavior.

Primary provider failure and fallback success must be displayed separately.

Example:

```text
Ensembl VEP — unavailable
VariantValidator — fallback rescue succeeded
```

not:

```text
Ensembl VEP — completed
```

Display-layer dtype fixes must not alter backend/source semantics.

---

## 20. Coding Style

Prefer:

```text
explicit code
typed structures
small functions
deterministic behavior
shared provider-resilience logic
versioned schemas
clear provenance
```

Avoid:

```text
hidden side effects
global mutable state
duplicated retry loops
broad exception swallowing
silent fallbacks
silent schema coercion
clever but opaque abstractions
```

Do not add new providers unless the current roadmap explicitly authorizes it.

---

## 21. Generic Debugging Workflow

When a bug appears:

```text
1. locate failing stage
2. inspect logs
3. trace the real call path
4. reproduce deterministically
5. identify root cause
6. classify structural vs degraded/optional
7. implement minimal safe fix
8. run focused tests
9. run required regression gates
10. report remaining risks
```

Do not patch symptoms before the root cause is understood.

---

## 22. Generic Final Response

When no stage-specific format exists, report:

```text
## Changed
Files modified.

## Reason
Why the change was needed.

## Validation
Tests/checks performed.

## Risks
Remaining limitations.

## Git
Commit SHA and git status.
```

If the current roadmap stage defines its own final-response checklist or final status token, the stage-specific contract takes precedence.

---

## 23. Golden Rules

```text
Exact identity before interpretation.

Evidence provenance must survive every transformation.

Missing evidence is not negative evidence.

Provider outage is not biological absence.

No fake evidence.

No silent double counting.

No automatic final clinical classification.

No automatic diagnosis.

No uncontrolled fallback.

No retry storms.

No patient-specific context promoted into global evidence.

No raw provider data sent to the interpretation LLM.

Human review remains mandatory.
```

When uncertain, choose the safer, more traceable, more explicit behavior and report the ambiguity instead of guessing.

## Codex task-boundary board

- This repository uses the opt-in Codex task-boundary board in `.codex/coordination/project.yaml`.
- Before substantial writes, load the installed `codex-coordinator` skill, list active claims from the primary worktree, and publish only this task's bounded claim.
- Native Codex tasks remain the execution, messaging, and transcript authority; an explicitly requested goal Coordinator is on demand, with no heartbeat or mandatory pull-request workflow.
- Reject cross-project notices and never store transcripts, reasoning, prompts, or tool output in Coordinator state.
