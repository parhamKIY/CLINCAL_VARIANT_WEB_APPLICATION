# Evidence Resilience Implementation Roadmap

**Document role:** Executable, review-gated plan for the current evidence-resilience workstream.

**Status:** Active planning document. No stage below is implemented by this document.

Current authorized stage: NONE — Stage 1 is complete and awaiting review
Current implementation permission: No production integration is authorized
Next automatic stage: NONE — explicit review approval required

**Authoritative context:**

- `docs/AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md` — Evidence Graph architecture, provider decisions, and semantic rules.
- `docs/PROJECT_DECLARATION.md` — project boundaries, historical contracts, current schema versions, and prior stage record.

This document deliberately does not reproduce those documents. It records the approved execution order, implementation reality, required safeguards, validation, and review checkpoints for the selected evidence-resilience work.

## Table of contents

1. [Document roles and execution policy](#1-document-roles-and-execution-policy)
2. [Scope and non-negotiable constraints](#2-scope-and-non-negotiable-constraints)
3. [Frozen provider decision record](#3-frozen-provider-decision-record)
4. [Current implementation reality and discrepancies](#4-current-implementation-reality-and-discrepancies)
5. [Evidence-node, trigger, and provenance rules](#5-evidence-node-trigger-and-provenance-rules)
6. [Readiness and final interpretation disposition](#6-readiness-and-final-interpretation-disposition)
7. [VEP-outage field-composition policy](#7-vep-outage-field-composition-policy)
8. [Execution stages](#8-execution-stages)
9. [Cross-stage test and live-validation plan](#9-cross-stage-test-and-live-validation-plan)
10. [File-level implementation map](#10-file-level-implementation-map)
11. [Roadmap maintenance policy](#11-roadmap-maintenance-policy)

## 1. Document roles and execution policy

| Document | Responsibility |
|---|---|
| `PROJECT_DECLARATION.md` | Project scope, safety boundaries, historical implementation state, and major contracts. |
| `AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md` | Architectural source of truth, Evidence Graph model, provider decisions, semantic distinctions, and project context. |
| `EVIDENCE_RESILIENCE_IMPLEMENTATION_ROADMAP.md` | Current executable plan: dependencies, checkpoints, expected files, tests, Definition of Done, status, review state, notes, and validation evidence. |

Execution is sequential and review-gated. A future coding agent must read this roadmap and the master handoff before working on any stage. It may perform only the explicitly approved current stage.

## 2. Scope and non-negotiable constraints

### Product boundary

- The application processes only 1–10 already-filtered/selected variants.
- It is not a raw-VCF filtering system, genome-wide prioritizer, ranking engine, or Top-N selector.
- Human review remains mandatory.

### Safety boundary

- No autonomous ACMG classification.
- No diagnosis.
- No treatment recommendation.
- No automatic ClinGen CSpec execution.
- No raw patient data, raw VCF rows, genotype, sample identifiers, or ignored worksheet content sent to the interpretation LLM.
- Missing evidence is never converted to zero, benign evidence, or negative evidence.
- Valid `no_match` is not an operational provider failure.
- Operational redundancy is not biological independence.

### Integration boundary

- Do not reopen API discovery for this cycle.
- Do not add a provider beyond ClinGen ERepo and NCBI MedGen unless a completed implementation stage demonstrates a concrete critical Evidence Graph gap and receives separate approval.
- Do not overwrite valid primary evidence with ERepo or MedGen evidence.
- Preserve input order and separate EvidenceObjects for same-gene variants.

## 3. Frozen provider decision record

| Decision | Provider | Approved role / observation |
|---|---|---|
| Selected | ClinGen ERepo | Expert-curated variant context. Not a universal GeneBe replacement. |
| Selected | NCBI MedGen | Disease/HPO context, phenotype-gene supporting context, and gene-disease supporting context. |
| Not selected this cycle | Monarch | Tested HTTP 403. |
| Not selected this cycle | Open Targets | Tested HTTP 403. |
| Not selected this cycle | PanelApp | Tested HTTP 403. |
| Not selected this cycle | Gene2Phenotype | Tested TLS timeout. |
| Not selected this cycle | Orphadata | Tested TLS timeout. |
| Not selected this cycle | Direct ClinGen GDV feed | Tested timeout. |

These are point-in-time observations from the tested network, not claims that services are globally unavailable. Reopen the decision only after a concrete unresolved critical evidence gap is demonstrated after the approved work.

## 4. Current implementation reality and discrepancies

### Existing code to reuse

| Area | Observed implementation |
|---|---|
| Annotation orchestration | `backend/annotation.py`: VEP, VariantValidator fallback, GeneBe, MyVariant, ClinVar, GenCC, and CSpec. |
| Exact retrieval intelligence | `backend/retrieval_intelligence.py`: validated CPRA/assembly bundle, HGVS, transcript, rsID, ClinVar, disease IDs, and provenance. |
| Resilience contract | `backend/provider_resilience.py`: bounded connect/read timeouts, retry policy, circuits, provider provenance, and capability results. |
| Phenotype and disease context | `backend/phenotype.py` and `backend/mydisease.py`: canonical HPO handling, one-request Phen2Gene operation, local support paths, and bounded same-gene caching. |
| Evidence serialization and lineage | `backend/report.py`: EvidenceObject `2.5`, strict bounded lineage, shared-upstream groups, capability results, and sanitized LLM evidence. |
| Conflict/classification auditing | `backend/conflict_auditor.py` and `backend/classification_evidence.py`. |
| Readiness | `backend/evidence_readiness.py`: current internal readiness audit. |
| Orchestration and persistence | `backend/pipeline.py` and `backend/database.py`: pipeline `3.2`, SQLite schema `4`, recovery and order/cardinality contracts. |

### Verified implementation reality

- ERepo and MedGen are not currently configured, invoked, serialized, shown in provider diagnostics, or covered by tests.
- When VEP operationally fails, VariantValidator can promote exact gene/transcript/HGVS mapping, but leaves canonical consequence and impact unavailable. GeneBe retains cleaned consequence/transcript evidence under its provider subtree rather than promoting it to canonical fields.
- Current EvidenceObject source statuses are fixed to `vep`, `myvariant`, `clinvar`, and `clingen`; lineage contains additional active sources.
- Current capability results cover annotation, variant context, ClinVar, CSpec, phenotype-gene, disease context, population, and literature. They do not yet model ERepo, MedGen, field composition, or coverage states.
- Current persisted internal readiness states are `READY`, `READY_WITH_LIMITATIONS`, `RESCUE_REQUIRED`, and `MINIMUM_IDENTITY_FAILURE`. The handoff describes final `BLOCKED`, but the current code does not persist that enum value.
- The master handoff refers to several standalone audit/resilience/schema documents that are not currently present in `docs/`; this roadmap and the master handoff are the current evidence-resilience execution references.

## 5. Evidence-node, trigger, and provenance rules

### 5.1 Semantic capability separation

The following are separate semantic nodes. An umbrella may summarize them later, but must never replace, relabel, or collapse the raw nodes.

| Semantic node | Permitted source |
|---|---|
| `automated_acmg_context` | GeneBe only |
| `expert_curated_variant_context` | ClinGen ERepo only |
| `gene_disease_validity` | GenCC / ClinGen validity only |
| `gene_disease_support` | MedGen supporting context |
| `phenotype_gene_ranking` | Phen2Gene only |
| `phenotype_gene_support` | MedGen and existing local support only |
| `disease_hpo_context` | MyDisease plus source-separated MedGen context |

Never convert:

```text
MedGen → Phen2Gene score or rank
MedGen → GenCC / ClinGen validity classification
ERepo  → GeneBe automated ACMG fields
```

### 5.2 Evidence-gap-driven MedGen enrichment

MedGen is triggered by target-node insufficiency, not provider-failure-only chains. Each MedGen role must emit a bounded enrichment-decision record:

```text
triggered
reason_codes
target_semantic_node
primary_retrieval_state
required_fields_missing
query_key
```

Subject to its required prerequisites, trigger MedGen when the target semantic node remains insufficient because:

1. the primary provider operationally failed;
2. the primary provider returned valid `no_match`;
3. primary evidence lacks required fields for that node; or
4. primary evidence is partial, ambiguous, or non-applicable.

Do not trigger MedGen when semantically equivalent evidence already sufficiently populates the target node. Do not overwrite valid primary evidence.

Deduplication rules:

- Disease/HPO and gene-disease support: one bounded normalized query per unique gene.
- Phenotype-gene support: one bounded query per unique `(gene, accepted HPO ID/label)` pair.
- Fan normalized evidence back to distinct per-variant records, preserving original order.

### 5.3 MedGen role normalization

#### Disease/HPO context

MyDisease remains source-labelled primary context. MedGen may add source-separated disease/HPO context when the node is insufficient.

#### Phenotype-gene support

MedGen requires an already-known variant gene and an accepted canonical HPO label. It stores supporting phenotype-gene-disease context only; it is not a ranking result.

#### Gene-disease support

MedGen may store gene-associated disease support. It cannot populate `gene_disease_validity` or imply causal validity by itself.

### 5.4 MedGen provenance and correlation

When available, normalize and retain:

- MedGen UID and ConceptID;
- query gene;
- queried HPO/clinical-feature term;
- condition identifiers and title;
- match method;
- source/database metadata;
- upstream source identifiers;
- ConceptMeta-derived source information;
- retrieval/no-match state; and
- retrieval timestamp.

MedGen is an aggregator/context source, not an automatically independent biological vote. If deterministically exposed upstream source identifiers overlap evidence already retained in the EvidenceObject, add that overlap to lineage/shared-upstream metadata. If no deterministic upstream identity is exposed, retain the MedGen context with its aggregator provenance but do not infer independence.

### 5.5 ERepo normalization

ERepo is exact, expert-curated variant context. It must retain the exact query strategy and identity verification, record ID/UUID, condition, statement outcome, assertion method, available metadata/release, retrieval state, candidate rejections, and valid `no_match`.

ERepo remains distinct from GeneBe, ClinVar, and CSpec. If ERepo metadata explicitly establishes ClinVar derivation, record ClinVar as shared upstream; otherwise do not invent a correlation or independence claim.

## 6. Readiness and final interpretation disposition

Do not prematurely migrate the persisted readiness enum.

### Internal readiness/action state

```text
READY
READY_WITH_LIMITATIONS
RESCUE_REQUIRED
MINIMUM_IDENTITY_FAILURE
```

### Derived final interpretation disposition

```text
READY
READY_WITH_LIMITATIONS
BLOCKED
```

| Internal state | Derived disposition |
|---|---|
| `READY` | `READY` |
| `READY_WITH_LIMITATIONS` | `READY_WITH_LIMITATIONS` |
| `RESCUE_REQUIRED` | No final disposition until post-enrichment reassessment |
| `MINIMUM_IDENTITY_FAILURE` | `BLOCKED` |

`RESCUE_REQUIRED` remains an internal pre-enrichment/action state. A persisted disposition or enum migration requires separate justification, compatibility design, tests, and review. Until then, derive the disposition without changing existing persisted readiness values.

Sparse but valid evidence remains interpretable as `READY_WITH_LIMITATIONS` whenever the variant is structurally safe. A provider outage alone must not block interpretation.

## 7. VEP-outage field-composition policy

```text
VEP success
  → VEP remains preferred canonical annotation source

VEP operational failure or insufficient annotation evidence
  → controlled field-level composition from VariantValidator, GeneBe, and MyVariant
```

Composition requirements:

- exact allele identity and assembly consistency;
- transcript coherence;
- HGVS.c and HGVS.p from the same selected transcript context;
- recognized consequence semantics;
- per-field provider and identity provenance;
- retained disagreements and alternative transcript assertions; and
- no false relabelling of composed evidence as VEP evidence.

VEP success must not be overwritten. GeneBe `effect` alone cannot be promoted without validated consequence semantics. A valid VEP `no_match` remains distinct from operational failure; composition is governed by annotation-node insufficiency rather than an implicit provider fallback claim.

## 8. Execution stages

### Stage 1 — Freeze ERepo response and exact-identity contract

- **Objective:** Freeze the tested ERepo response shape, exact query strategy, identity rules, candidate-rejection rules, and deterministic fixtures before production integration.
- **Existing code to reuse:** `backend/retrieval_intelligence.py`, `backend/provider_resilience.py`, existing ClinVar exact-retrieval tests.
- **Files/modules likely affected:** Documentation and future test fixtures only; no production integration in this stage.
- **Required changes:** Record approved ERepo fixture(s), exact CPRA/assembly acceptance requirements, and valid no-match contract.
- **Contracts that must not change:** No GeneBe replacement, no autonomous ACMG decision, no raw provider payload to LLM.
- **Schema impact:** None.
- **Provenance/lineage impact:** Define required provenance fields; do not emit production lineage yet.
- **Readiness impact:** None.
- **Persistence/backward-compatibility impact:** None.
- **Tests:** Fixture validation and identity acceptance/rejection cases.
- **Live validation:** One bounded known-record and one bounded expected-no-match confirmation if needed to freeze the fixture.
- **Risks:** Endpoint/query semantics or response identity fields are not yet captured in repository fixtures.
- **Dependencies:** None.
- **Definition of Done:** Approved fixture and exact identity contract exist; deterministic cases define success, no-match, malformed payload, and mismatch outcomes.
- **Status:** COMPLETE
- **Review:** PENDING
- **Implementation notes:** The Stage 1 contract now separates bounded discovery identifiers from exact acceptance, defines final `no_match` across all eligible strategies, restores `EXACT_MATCH_EQUIVALENT_REPRESENTATION` only for deterministic normalized-CPRA proof, and freezes detailed-document version verification. No production code, schema, persistence, readiness, or LLM payload changed.
- **Validation evidence:** On 2026-08-16, bounded live summary queries returned one record for each of exact genomic HGVS `NC_000012.12:g.102894804T>A`, exact transcript HGVS `NM_000277.3:c.283A>T`, and exact ClinVar Variation ID `102645`; all resolved to `CA229507`. An eligible genomic-HGVS query for `NC_000001.11:g.1A>G` returned structured `404 Not Found`. The exact detailed-document endpoint returned `data.uuid=c61fa227-893e-4be0-9e02-2d7c1494af20` and `data.metadata.version=1.0.0`, matching the accepted summary record's `uuid` and `docVersion`. Fixture-contract tests pass.

**STOP FOR REVIEW.**

### Stage 2 — ERepo integration

- **Objective:** Add exact ERepo expert-curated variant context as a separate evidence node.
- **Existing code to reuse:** Identifier bundle, capability result contract, lineage builder, conflict/classification auditors, provider readiness targets.
- **Files/modules likely affected:** `config.py`, `.env.example`, proposed `backend/erepo.py`, `backend/annotation.py`, `backend/report.py`, `backend/classification_evidence.py`, `backend/conflict_auditor.py`, `backend/pipeline.py`, `backend/database.py`, UI/report projections, tests.
- **Required changes:** Bounded provider client; exact lookup; normalized ERepo context; no-match and retrieval diagnostics; source-labelled rendering.
- **Contracts that must not change:** GeneBe remains `automated_acmg_context`; ERepo remains `expert_curated_variant_context`; ClinVar direct evidence and CSpec context-only semantics remain unchanged.
- **Schema impact:** Additive, versioned EvidenceObject context and capability only if the strict serializer requires it; no unversioned fields.
- **Provenance/lineage impact:** Provider `clingen_erepo`; shared ClinVar upstream only when explicitly exposed.
- **Readiness impact:** ERepo may populate its own node but cannot fabricate automated ACMG context.
- **Persistence/backward-compatibility impact:** Older evidence hydrates empty ERepo context through a tested compatibility path.
- **Tests:** Exact success/no-match, timeout, `403`, `429`, malformed payload, CPRA mismatch, assembly mismatch, conflict retention, persistence recovery.
- **Live validation:** Bounded exact record plus bounded no-match after deterministic tests pass.
- **Risks:** Strict classification audit currently expects three source outcomes; extension must preserve semantic separation and correlation rules.
- **Dependencies:** Stage 1.
- **Definition of Done:** Exact ERepo context is source-attributed, serializable, persistent, non-equivalent to GeneBe, and covered by deterministic tests.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 3 — Shared MedGen client plus disease/HPO role

- **Objective:** Add a bounded MedGen E-utilities client and source-separated disease/HPO context.
- **Existing code to reuse:** `backend/provider_resilience.py`, MyDisease same-gene cache/normalization patterns, canonical HPO handling, NCBI request conventions.
- **Files/modules likely affected:** Proposed `backend/medgen.py`, `backend/mydisease.py`, `backend/pipeline.py`, `backend/report.py`, configuration, persistence/projections, tests.
- **Required changes:** Evidence-gap decision, normalized same-gene queries, bounded ESearch/ESummary parsing, disease/HPO records, deduplication, and per-variant fan-out.
- **Contracts that must not change:** MyDisease remains source-separated; a MedGen condition record is not causal proof; local context remains local/context-only.
- **Schema impact:** Additive, versioned MedGen disease/HPO context only when serialized evidence requires it.
- **Provenance/lineage impact:** Preserve UID/ConceptID, query basis, source metadata, exposed upstream IDs, retrieval state, and timestamp.
- **Readiness impact:** Supplies only the `disease_hpo_context` node; no readiness enum migration.
- **Persistence/backward-compatibility impact:** Historical evidence remains valid with empty MedGen context.
- **Tests:** Operational failure, valid no-match, incomplete primary result, same-gene deduplication, malformed ESearch/ESummary, correlation retention.
- **Live validation:** Bounded representative gene queries after deterministic success.
- **Risks:** Existing ClinVar and PubMed E-utilities helpers are provider-specific; do not duplicate or broadly refactor them without need.
- **Dependencies:** Stage 1 is not technically required; Stage 2 is independent but remains earlier in the approved sequence.
- **Definition of Done:** MedGen disease/HPO context is evidence-gap-driven, bounded, source-separated, provenance-complete, and cannot overwrite MyDisease.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 4 — MedGen phenotype-gene supporting evidence

- **Objective:** Add known-gene plus accepted-HPO MedGen supporting evidence when the phenotype-gene support node is insufficient.
- **Existing code to reuse:** `backend/phenotype.py`, accepted HPO normalization, per-analysis Phen2Gene deduplication, local HPO-gene fallback provenance.
- **Files/modules likely affected:** `backend/medgen.py`, `backend/phenotype.py`, `backend/pipeline.py`, `backend/report.py`, UI/report projections, tests.
- **Required changes:** Query deduplication by gene/HPO tuple; normalized support records; evidence-gap decision; per-variant attachment.
- **Contracts that must not change:** No MedGen score/rank/weight model; no variant reordering; local support remains distinct.
- **Schema impact:** Additive, versioned `phenotype_gene_support` evidence only if required by strict EvidenceObject serialization.
- **Provenance/lineage impact:** Retain accepted HPO term, exact gene, matching condition IDs, match method, exposed sources, and retrieval state.
- **Readiness impact:** May populate support but never `phenotype_gene_ranking`.
- **Persistence/backward-compatibility impact:** Historical Phen2Gene records remain untouched.
- **Tests:** Primary failure, primary valid no-match, partial primary evidence, SCN1A/seizure support, FBN1/ectopia-lentis support, CFTR/malabsorption valid no-match, same-query reuse.
- **Live validation:** Bounded documented query classes after deterministic tests pass.
- **Risks:** Raw phenotype text must never replace accepted canonical HPO labels.
- **Dependencies:** Stage 3.
- **Definition of Done:** Supporting context is source-separated, query-deduplicated, and cannot populate Phen2Gene ranking fields.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 5 — MedGen gene-disease supporting evidence

- **Objective:** Add MedGen gene-disease support when that support node is insufficient.
- **Existing code to reuse:** GenCC gene selection in `backend/annotation.py`, same-gene caching patterns, provider resilience, lineage builder.
- **Files/modules likely affected:** `backend/medgen.py`, `backend/annotation.py`, `backend/pipeline.py`, `backend/report.py`, tests.
- **Required changes:** Evidence-gap decision, source-separated support records, exact query-gene retention, and distinct capability mapping.
- **Contracts that must not change:** GenCC alone populates `gene_disease_validity`; MedGen cannot emit ClinGen validity classes or causal claims.
- **Schema impact:** Additive, versioned `gene_disease_support` only when serialized evidence requires it.
- **Provenance/lineage impact:** Record MedGen concept metadata and deterministic overlaps with existing disease sources.
- **Readiness impact:** Supplies support only; future umbrella summaries may show support without claiming validity.
- **Persistence/backward-compatibility impact:** Existing GenCC-only records remain unchanged.
- **Tests:** GenCC operational failure, valid no-match, partial/insufficient claim, no-gene not-triggered, malformed response, same-gene reuse.
- **Live validation:** Bounded SCN1A/FBN1/CFTR gene-only checks after deterministic tests pass.
- **Risks:** Gene-associated condition context must not imply gene-disease validity.
- **Dependencies:** Stage 3.
- **Definition of Done:** MedGen support is available only as its own node and cannot alter GenCC validity semantics.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 6 — VEP-outage field-level annotation composition

- **Objective:** Permit controlled canonical annotation composition when VEP is operationally unavailable or annotation evidence is insufficient.
- **Existing code to reuse:** VEP/VariantValidator/GeneBe/MyVariant normalizers, identifier bundle, conflict auditor, strict EvidenceObject serializer.
- **Files/modules likely affected:** `backend/annotation.py`, `backend/report.py`, `backend/conflict_auditor.py`, `backend/evidence_readiness.py`, persistence/projections, tests.
- **Required changes:** Field-selection policy, per-field provenance, transcript-coherence validation, consequence-semantic validation, retained alternative assertions.
- **Contracts that must not change:** VEP success remains preferred and untouched; exact identity is mandatory; no field may be relabelled as VEP if sourced elsewhere.
- **Schema impact:** Additive, versioned field-composition provenance if persisted in EvidenceObject.
- **Provenance/lineage impact:** Record provider, evidence path, identity validation, selected transcript, selection policy, and disagreement.
- **Readiness impact:** Produces better annotation evidence inputs but does not change the readiness enum in this stage.
- **Persistence/backward-compatibility impact:** Old records receive explicit no-composition/default state; existing source evidence remains intact.
- **Tests:** VEP outage composition, GeneBe representation mismatch, assembly mismatch, transcript conflict, HGVS incoherence, unrecognized consequence semantics, source retention.
- **Live validation:** Verify provider schema only; do not require inducing a live outage.
- **Risks:** Mixing HGVS.c/HGVS.p values across transcripts is structurally unsafe.
- **Dependencies:** Stages 2–5 are independent of composition but precede it by approved order.
- **Definition of Done:** Any promoted canonical field is exactly validated, transcript-coherent where required, provenance-complete, and not falsely attributed to VEP.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 7 — Evidence Coverage Calculator

- **Objective:** Calculate per-variant Evidence Graph coverage separately from provider reachability.
- **Existing code to reuse:** Capability results, lineage/shared-upstream groups, exact identity, readiness assessment.
- **Files/modules likely affected:** Proposed `backend/evidence_coverage.py`, `backend/report.py`, `backend/pipeline.py`, `backend/evidence_readiness.py`, persistence/projections, tests.
- **Required changes:** Versioned coverage records with raw semantic nodes, critical/important/optional fields, retrieval state, composition state, and correlation warnings.
- **Contracts that must not change:** Provider operational statuses remain provider-level facts; no provider count becomes an evidence score.
- **Schema impact:** Add a versioned coverage section only when strict serialization requires persistence.
- **Provenance/lineage impact:** Coverage points to existing evidence paths and never copies provider claims.
- **Readiness impact:** Calculate coverage only; do not change final gating in this stage.
- **Persistence/backward-compatibility impact:** Derive missing coverage for historical records without inventing evidence.
- **Tests:** Direct, composed, support-only, valid no-match, operational failure, not-triggered, and correlated-source matrices.
- **Live validation:** None beyond completed provider-specific validation.
- **Risks:** Collapsing non-equivalent semantic nodes in an umbrella summary.
- **Dependencies:** Stages 2–6.
- **Definition of Done:** Coverage is deterministic, per-variant, provenance-linked, and keeps raw semantic nodes distinct.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 8 — Capability and final-disposition integration

- **Objective:** Integrate coverage into capability presentation, reviewer warning semantics, and derived final interpretation disposition.
- **Existing code to reuse:** `backend/evidence_readiness.py`, `backend/pipeline.py`, Stage 114 source-status presentation, report projections.
- **Files/modules likely affected:** `backend/evidence_readiness.py`, `backend/pipeline.py`, `backend/variant_interpretation.py`, `backend/variant_report.py`, frontend review/preview modules, tests.
- **Required changes:** Derive final disposition from internal readiness after enrichment; render coverage and source-specific limitations.
- **Contracts that must not change:** Existing persisted internal readiness enum; no provider outage alone blocks interpretation; human review remains mandatory.
- **Schema impact:** Prefer runtime/projection derivation. Any persisted enum or schema migration requires separate justification, compatibility benefit, tests, and review.
- **Provenance/lineage impact:** Reviewer notices identify capability, source, retrieval state, composition, and limitations.
- **Readiness impact:** `RESCUE_REQUIRED` remains pre-enrichment; `MINIMUM_IDENTITY_FAILURE` may derive `BLOCKED`; structurally safe sparse cases remain `READY_WITH_LIMITATIONS`.
- **Persistence/backward-compatibility impact:** Preserve current readiness audit records unless a separately approved migration is required.
- **Tests:** Internal-to-derived mapping, post-enrichment reassessment, blocked identity, composed annotation, ERepo/MedGen source notices, persisted recovery.
- **Live validation:** One bounded end-to-end run after deterministic acceptance passes.
- **Risks:** Accidentally turning a display concept into a destructive persisted migration.
- **Dependencies:** Stage 7.
- **Definition of Done:** Reviewer-facing disposition is accurate, derived without premature migration, and source-specific limitations are retained.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 9 — Deterministic resilience and bounded live validation

- **Objective:** Validate every new evidence path, degraded state, migration path, and order/isolation invariant.
- **Existing code to reuse:** Existing fake sessions, `tests/test_provider_resilience.py`, `tests/test_failure_injection.py`, `tests/test_resilience_acceptance.py`, Stage 60/78 runners, live provider validation runner.
- **Files/modules likely affected:** Focused tests and live validation runner only unless a validated defect requires separately approved remediation.
- **Required changes:** Add provider fixtures, failure-injection scenarios, migration snapshots, same-gene reuse cases, and bounded live checkpoints.
- **Contracts that must not change:** Offline tests remain deterministic and live traffic remains opt-in/bounded.
- **Schema impact:** Verify every schema/persistence change introduced by completed prior stages.
- **Provenance/lineage impact:** Assert source labels, shared-upstream groups, and non-independence behavior.
- **Readiness impact:** Assert internal states and derived final disposition separately.
- **Persistence/backward-compatibility impact:** Assert recovery for old and new snapshots.
- **Tests:** Success, no-match, timeout, forbidden, rate limit, malformed payload, schema drift, partial data, exact mismatch, assembly mismatch, conflict, correlation, per-variant isolation, same-gene reuse, readiness, recovery.
- **Live validation:** One provider/query class at a time, progress checkpointing, normalized summary persistence, clean timeout/termination behavior.
- **Risks:** Treating live reachability as deterministic correctness.
- **Dependencies:** Stages 2–8.
- **Definition of Done:** Required deterministic gates pass and live results are recorded only as point-in-time operational evidence.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 10 — Failure-driven input-to-LLM audit

- **Objective:** Trace representative evidence loss or weakness from input through retrieval, normalization, EvidenceObject, coverage/readiness, LLM payload, and LLM result.
- **Existing code to reuse:** Pipeline state, provider diagnostics, sanitized EvidenceObject, interpretation diagnostics, persistence/recovery.
- **Files/modules likely affected:** Audit runner/tests and documentation; production changes only after a separately approved validated defect.
- **Required changes:** Redacted trace artifact and deterministic stage-by-stage assertions.
- **Contracts that must not change:** No prompt/model/provider behavior changes merely because an audit is being run.
- **Schema impact:** None unless a separately approved defect requires it.
- **Provenance/lineage impact:** Verify every retained source and correlation survives the trace.
- **Readiness impact:** Classify whether weakness arose before or after readiness.
- **Persistence/backward-compatibility impact:** Verify persisted EvidenceObject is sufficient for trace/retry.
- **Tests:** Input, retrieval, parser, normalization, serialization, coverage, readiness, LLM payload, and interpretation failure isolation.
- **Live validation:** One bounded trace only after deterministic trace validation.
- **Risks:** Making implementation changes while diagnosing.
- **Dependencies:** Stage 9.
- **Definition of Done:** Each observed loss/limitation has a classified cause; any defect is reported separately and awaits approval.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 11 — Documentation closeout

- **Objective:** Reconcile the handoff, declaration, execution roadmap, configuration documentation, and validation evidence with completed implementation.
- **Existing code to reuse:** Completed stage notes and test/live-validation outputs only.
- **Files/modules likely affected:** `docs/`, `README.md`, `.env.example`, accepted validation records.
- **Required changes:** Document implemented facts, known limitations, exact schema/persistence behavior, and current provider decisions.
- **Contracts that must not change:** Do not represent planned or untested work as implemented.
- **Schema impact:** Document only validated schema changes.
- **Provenance/lineage impact:** Document final source/correlation rules only as implemented and tested.
- **Readiness impact:** Document internal state versus derived disposition behavior.
- **Persistence/backward-compatibility impact:** Document tested migration/recovery guarantees.
- **Tests:** Re-run relevant final deterministic gates; cite exact validation evidence.
- **Live validation:** Record only bounded, completed observations.
- **Risks:** Copying stale handoff claims without reconciliation.
- **Dependencies:** Stages 1–10.
- **Definition of Done:** Documentation accurately reflects code, tests, validation, open issues, and the final provider decision record.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

## 9. Cross-stage test and live-validation plan

Required deterministic coverage across applicable stages:

- provider success, valid no-match, timeout, `403`, `429`, `5xx`, malformed payload, and schema drift;
- exact CPRA mismatch, assembly mismatch, transcript mismatch, and candidate rejection diagnostics;
- ERepo/GeneBe/ClinVar semantic separation and retained disagreement;
- MedGen aggregation provenance and deterministic shared-upstream correlation;
- no MedGen-derived Phen2Gene rank/score or GenCC validity class;
- VEP composition with exact field provenance and transcript coherence;
- provider failure versus valid missingness versus not-triggered evidence;
- same-gene/same-query reuse with separate per-variant EvidenceObjects and stable order;
- provider and variant failure isolation;
- internal readiness state and derived final interpretation disposition;
- historical EvidenceObject/pipeline/database snapshot recovery; and
- sanitized EvidenceObject to LLM-payload trace without raw patient/VCF/genotype data.

Live validation is never a replacement for deterministic tests. It is opt-in, bounded, checkpointed, provider-by-provider, and recorded as an operational observation with normalized safe summaries only.

## 10. File-level implementation map

| Area | Current or proposed files | Planned responsibility |
|---|---|---|
| ERepo | Proposed `backend/erepo.py`; `backend/annotation.py`; `config.py` | Exact curated-context retrieval and integration. |
| MedGen | Proposed `backend/medgen.py`; `backend/mydisease.py`; `backend/phenotype.py`; `backend/pipeline.py` | Bounded role-specific client, enrichment decisions, deduplication, and fan-out. |
| Exact identity | `backend/retrieval_intelligence.py`; `backend/annotation.py` | Reuse validated identifiers and exact allele constraints. |
| Resilience | `backend/provider_resilience.py` | Reuse bounded policy, circuits, provenance, and capability contracts. |
| Evidence serialization | `backend/report.py`; `backend/classification_evidence.py`; `backend/conflict_auditor.py` | Strict evidence nodes, lineage, correlation, conflict retention, and LLM-safe serialization. |
| Coverage/readiness | Proposed `backend/evidence_coverage.py`; `backend/evidence_readiness.py`; `backend/pipeline.py` | Coverage calculation and derived final disposition without premature enum migration. |
| Persistence | `backend/database.py` | Backward-compatible persistence/recovery only where a completed stage requires it. |
| Reviewer/report surfaces | `backend/variant_report.py`, `backend/report_data_projection.py`, frontend review/preview modules | Source-specific coverage, limitation, and disposition presentation. |
| Validation | Existing resilience/provider/pipeline tests and live validation runner | Fixtures, failure injection, acceptance, migration, and bounded live checks. |

## 11. Roadmap maintenance policy

When executing a future stage:

1. Read this roadmap first.
2. Read `docs/AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md`.
3. Perform only the currently approved stage.
4. Update that stage's **Implementation notes** and **Validation evidence** with observed, verified facts.
5. Mark the stage:

   ```text
   Status: COMPLETE
   Review: PENDING
   ```

6. Stop and wait for architecture review.
7. Only after explicit approval may that stage become:

   ```text
   Review: APPROVED
   ```

8. Begin the next stage only after the prior stage is approved.

Never mark a stage `COMPLETE` merely because code was written. Completion requires its Definition of Done and required deterministic tests to pass. Use `CHANGES_REQUESTED` when review identifies unresolved architectural, safety, provenance, compatibility, or validation issues.
