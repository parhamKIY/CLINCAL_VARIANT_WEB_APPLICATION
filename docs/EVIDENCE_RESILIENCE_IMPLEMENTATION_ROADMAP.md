# Evidence Resilience Implementation Roadmap

**Document role:** Executable, review-gated plan for the current evidence-resilience workstream.

**Status:** Active execution roadmap. Stages 1–3 are COMPLETE / APPROVED.

**Current completed stage:** Stage 3 — Shared MedGen client plus disease/HPO role.

**Current implementation permission:** NONE — Stage 3 is approved; Stage 4 requires explicit authorization.

**Next eligible stage:** Stage 4 — MedGen phenotype-gene supporting evidence, only after Stage 3 review approval and explicit authorization.

**Next automatic stage:** NONE — every stage requires explicit review approval.

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
8. [Rollback and regression safety policy](#8-rollback-and-regression-safety-policy)
9. [Execution stages](#9-execution-stages)
10. [Cross-stage test and live-validation plan](#10-cross-stage-test-and-live-validation-plan)
11. [File-level implementation map](#11-file-level-implementation-map)
12. [Roadmap maintenance policy](#12-roadmap-maintenance-policy)

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

## 8. Rollback and regression safety policy

This workstream must be implemented so that failure in a later stage never requires discarding previously approved work or losing the last known-good project state. The default recovery unit is the current stage, not the entire workstream.

### 8.1 Dedicated branch and immutable baseline

Before any Stage 2 production change:

- create or confirm a dedicated evidence-resilience feature branch;
- capture a clean, named baseline commit representing the last known-good pre-Stage-2 implementation;
- create a durable tag for that baseline;
- record the exact commit hash, branch name, tag, database/schema version, EvidenceObject version, pipeline version, and deterministic test baseline in this roadmap's Stage 2 validation notes before implementation begins; and
- preserve a recoverable backup/snapshot of any mutable local persistence used for compatibility testing.

Recommended naming, subject to repository conventions:

```text
branch: feature/evidence-resilience
tag:    evidence-resilience-baseline-2026-08-16
```

If the repository already has an equivalent protected branch/tag strategy, reuse it rather than creating a competing convention.

### 8.2 One stage = one reviewable change set

- Each implementation stage must be independently reviewable and independently revertible.
- Do not combine multiple roadmap stages into one implementation commit/change set.
- A later stage must not rewrite or silently invalidate an earlier APPROVED stage.
- If a stage fails review, revert or amend that stage only; do not discard earlier approved stages.
- Do not begin the next stage until the current stage is `COMPLETE / APPROVED`.

### 8.3 Existing behavior is the regression baseline

Before each implementation stage, record the relevant deterministic baseline tests and representative outputs. After the stage:

- all previously required deterministic tests must remain green unless an explicitly approved contract change requires a documented expectation update;
- new stage-specific tests must pass;
- historical persisted analyses required by the current compatibility contract must still hydrate/read successfully; and
- unexpected changes in variant order, cardinality, canonical identity, existing provider evidence, readiness, LLM sanitization, or report projection are regressions until proven otherwise.

A stage cannot be marked `COMPLETE` while an unexplained regression remains.

### 8.4 Golden-case regression set

Before Stage 2 implementation, freeze a small deterministic Golden Case set representing the current pipeline. Prefer 5–7 public/non-PHI variants or already-approved synthetic fixtures that exercise materially different paths. At minimum the set should cover, where existing fixtures allow:

- normal VEP-success annotation;
- ClinVar-present and ClinVar-no-match behavior;
- population evidence;
- phenotype/disease context;
- same-gene multiple variants and stable original order; and
- at least one currently limited/degraded evidence path.

For each Golden Case, retain bounded machine-comparable snapshots of the relevant normalized outputs, not raw patient data. Compare pre/post-stage outputs and classify every difference as:

```text
EXPECTED_IMPROVEMENT
EXPECTED_CONTRACT_CHANGE
UNEXPECTED_REGRESSION
```

Any `UNEXPECTED_REGRESSION` blocks approval until resolved or explicitly accepted by architecture review. Golden snapshots must follow existing privacy and repository fixture rules.

### 8.5 Additive-first integration

Wherever possible, new evidence paths must initially be additive:

- ERepo adds `expert_curated_variant_context`; it does not replace GeneBe, ClinVar, or CSpec.
- MedGen adds source-separated support/context; it does not overwrite MyDisease, Phen2Gene, or GenCC semantics.
- Coverage calculation initially observes existing evidence; it must not itself rewrite evidence.
- Derived final disposition should remain a projection until persistence is separately justified.

Removal/replacement of existing behavior requires an explicit, reviewed contract change.

### 8.6 Shadow/observation mode for high-risk behavior changes

High-risk stages must prove their proposed output before they are allowed to alter gating or canonical behavior.

**Stage 6 — VEP field composition:** first compute composed canonical candidates and provenance in shadow/observation mode for deterministic tests/Golden Cases. Compare them against existing provider evidence. Promotion into canonical fields/readiness must occur only after transcript coherence, exact identity, consequence semantics, and regression comparisons pass.

**Stage 7 — Evidence Coverage Calculator:** initially calculate/report coverage without changing readiness or LLM gating.

**Stage 8 — Capability/final disposition/UI integration:** prefer runtime/projection behavior first. Do not perform a destructive readiness/schema migration unless separately justified and approved.

### 8.7 Persistence and migration safety

- Schema changes must be additive/backward-compatible whenever possible.
- Every migration that becomes necessary must have a deterministic recovery/compatibility test before approval.
- Historical readiness states and EvidenceObjects must remain readable under the documented compatibility contract.
- No destructive migration is allowed solely to simplify new code.
- If rollback would require data conversion, the rollback/recovery procedure must be documented and tested before the migration is approved.

### 8.8 Provider failure must remain isolated

A newly integrated provider must never become a single point of failure for the existing pipeline. Timeout, `403`, `429`, malformed payload, schema drift, or provider outage must degrade only the evidence node(s) that depend on that provider and must preserve existing valid evidence, ordering, persistence, and reviewer access.

### 8.9 Pre-Stage-2 safety gate

Stage 2 is not authorized until the following are recorded and reviewed:

```text
[ ] dedicated feature branch confirmed
[ ] last-known-good baseline commit recorded
[ ] rollback tag recorded
[ ] current schema / EvidenceObject / pipeline versions recorded
[ ] deterministic baseline test result recorded
[ ] Golden Case set frozen
[ ] Golden Case baseline snapshots generated
[ ] mutable persistence backup/snapshot prepared where applicable
[ ] working tree status reviewed so unrelated pre-existing changes are not overwritten
```

This safety gate does not create a new numbered implementation stage. It is a mandatory entry condition for Stage 2 and remains the rollback model for all later stages.

### 8.10 Pre-Stage-2 baseline record (2026-08-16)

- **Branch:** `feature/evidence-resilience`.
- **Baseline checkpoint:** `36d27ba9e8b6090eb227ab5340237ea420aa516f` (`docs: establish pre-Stage-2 safety baseline`) on `feature/evidence-resilience`; lightweight tag `evidence-resilience-baseline-2026-08-16` resolves to the same commit. It includes Stage 1's approved contract and this safety gate, but no Stage 2 production behavior.
- **Versions:** SQLite schema `4`; EvidenceObject `2.5`; pipeline `3.2`; readiness audit schema `1.0` with persisted states `READY`, `READY_WITH_LIMITATIONS`, `RESCUE_REQUIRED`, and `MINIMUM_IDENTITY_FAILURE`.
- **Deterministic baseline:** `python tests/run_stage78_resilience_acceptance.py` compiled successfully, passed the secrets audit, and passed its Stage 78 scenario (`1 passed, 1276 deselected in 5.64s`). Its full offline suite established two pre-existing failures: `tests/test_pipeline.py::TestFrontendFoundation::test_manual_table_executes_pipeline` (expected six Streamlit dataframes, observed eight) and `tests/test_stage79_acceptance_assets.py::test_acceptance_registry_freezes_all_stage79_defects` (missing tracked `docs/acceptance_failures_v1.md`). The suite result was `1269 passed, 2 failed, 6 skipped in 144.62s`. The established Testing V3 runner also recorded the first failure as pre-existing: `878 passed, 1 failed, 4 skipped, 395 deselected in 154.12s` at `84.18%` coverage. These failures are baseline observations, not Stage 2 work.
- **Golden baseline:** `tests/golden/evidence_resilience/pre_stage2_v1.json` freezes seven non-PHI synthetic cases (`ERC-01` through `ERC-07`) generated from the existing Stage 105 fixture. `tests/test_stage105_professor_testcase.py::test_pre_stage2_golden_cases_match_stable_pre_llm_projection` compares only normalized identity, gene, transcript, consequence, source-presence states, and capability states. It intentionally excludes timestamps, raw payloads, provider metadata, ordering outside input order, and LLM prose. `ERC-04` additionally reuses the existing sparse-but-valid phenotype non-concordance overlay. The existing Stage 78 fixture remains the degradation overlay for retained fallback provenance and persistence.
- **Golden diff policy:** every future difference must be marked `EXPECTED_IMPROVEMENT`, `EXPECTED_CONTRACT_CHANGE`, or `UNEXPECTED_REGRESSION`. The last category blocks Stage review. Loss of existing source evidence, identity/order changes, unapproved readiness restriction, inability to load historical persisted state, or any unclassified difference is an unexpected regression.
- **Persistence/recovery baseline:** `tests/test_stage99_persistence_recovery_v4.py` verifies schema-4 normalized recovery state, edit/selection/finalization round trips, no interpretation regeneration during recovery, schema-3-to-4 recovery-row backfill, and fail-closed tamper detection. `tests/test_pipeline.py` retains legacy schema compatibility, bounded schema-2 migration, and recovery-request isolation coverage. Historical persisted analyses remain readable unless a separately approved, tested migration changes that contract.
- **Rollback:** revert only a future stage by reverting its isolated stage commit on this branch, run its pre-stage baseline plus Golden and recovery tests, and retain earlier approved commits. Return the entire workstream to the pre-Stage-2 state by checking out `evidence-resilience-baseline-2026-08-16` in a separate recovery branch/worktree; never reset or overwrite unrelated work. If a later migration has occurred, follow its approved recovery procedure before deploying the baseline.

## 9. Execution stages

### Current stage status summary

| Stage | Status | Review | Authorization |
|---|---|---|---|
| 1 — ERepo contract freeze | COMPLETE | APPROVED | CLOSED |
| 2 — ERepo integration | COMPLETE | APPROVED | CLOSED |
| 3 — Shared MedGen disease/HPO role | COMPLETE | APPROVED | CLOSED |
| 4–11 | NOT_STARTED | NOT_REQUIRED | LOCKED until explicit Stage 4 authorization |

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
- **Review:** APPROVED
- **Implementation notes:** Contract frozen on 2026-08-16. Discovery identity is explicitly separated from exact acceptance identity. Approved bounded discovery strategies are validated assembly-specific genomic HGVS, validated ClinVar Variation ID, and validated transcript HGVS; CAID remains future-only until stored as a validated allele-scope identifier. Exact acceptance supports `EXACT_MATCH` and deterministically proven `EXACT_MATCH_EQUIVALENT_REPRESENTATION`. Strategy-level no-match is preserved separately; final provider-level `no_match` requires exhaustion of all eligible approved strategies without an accepted exact record. Detailed document UUID and `data.metadata.version` must match the accepted summary UUID/docVersion.
- **Validation evidence:** Frozen deterministic fixtures cover exact GRCh38 success, detailed-document UUID/version verification and mismatch, per-strategy no-match versus final no-match, CPRA/assembly mismatch, deterministic padded-indel equivalent representation, and malformed/identity-incomplete input. Stage 2 entry criteria additionally require deterministic timeout, `403`, `429`, and UUID/version mismatch coverage before production acceptance. Bounded live verification on 2026-08-16 confirmed: genomic HGVS exact success for `NC_000012.12:g.102894804T>A`, transcript HGVS exact success for `NM_000277.3:c.283A>T`, ClinVar Variation ID exact discovery for `102645`, structured 404 no-match for `NC_000001.11:g.1A>G`, and detail `data.metadata.version=1.0.0` for UUID `c61fa227-893e-4be0-9e02-2d7c1494af20`. Stage 1 changed the contract/fixture specification only and did not authorize Stage 2 production behavior.

**STAGE CLOSED — APPROVED. Stage 2 remains gated by Section 8.9 and explicit authorization.**

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
- **Dependencies:** Stage 1 APPROVED plus completion/review of the pre-Stage-2 safety gate in Section 8.9.
- **Definition of Done:** Exact ERepo context is source-attributed, serializable, persistent, non-equivalent to GeneBe, covered by deterministic tests, and introduces no unexplained regression in the frozen Golden Cases or pre-Stage-2 baseline.
- **Status:** COMPLETE
- **Review:** APPROVED
- **Implementation notes:** Approved on 2026-08-16 at corrective checkpoint `2e90246dff5ecf38233550aa5b9961360217bac9`. Added the bounded `backend/erepo.py` client behind `EREPO_BASE_URL`, `EREPO_TIMEOUT`, `EREPO_MAX_RETRIES`, and `ENABLE_EREPO`. It executes only approved exact genomic-HGVS, ClinVar Variation ID, and transcript-HGVS discovery strategies. Corrective review changes independently derive the accepted genomic HGVS from the declared build/CPRA through the shared deterministic normalizer before querying; distinguish assembly, coordinate, reference, alternate, generic HGVS, and unresolved alternative-indel rejection; and retain the exact normalized CPRA, normalizer/provenance, and representation basis for deterministic padded-VCF equivalence only. Detailed UUID/document-version verification remains mandatory. The additive `schema_version: "1.0"` `expert_curated_variant_context` remains source-attributed to `clingen_erepo`, compact, serializable, lineage-tracked, reference-model projected, and separately conflict-audited. It does not populate GeneBe automated ACMG fields, replace direct ClinVar, or change readiness. Historical EvidenceObjects without the optional context remain valid and project an empty context.
- **Validation evidence:** Corrective deterministic coverage in `tests/test_erepo.py` and `tests/test_erepo_orchestration.py` passed (`21 passed in 0.19s`): exact, ClinVar-ID, and transcript-HGVS discovery; no-match; timeout; `403`; `429`; malformed/schema-drift; retracted; assembly/HGVS/coordinate/reference/alternate mismatch; unresolved indel representation; equivalent normalized-identity retention; UUID/docVersion mismatch; and multiple condition-separated records. The enabled mocked orchestration path reached identifier construction through EvidenceObject serialization, and verifies both ERepo no-match and provider failure are non-blocking. Stage 105 ERC-01..ERC-07 projection passed (`1 passed in 6.03s`); Stage 99 persistence recovery passed (`8 passed in 6.38s`). The focused annotation/ERepo regression run passed `877` tests with `4` skipped and retained only the pre-existing manual-table Streamlit dataframe-count failure. `python tests/run_stage78_resilience_acceptance.py` passed compilation, secrets, and its Stage-78 scenario, then ran `1291 passed, 2 failed, 6 skipped in 153.56s`; the same two recorded pre-existing failures remain: manual-table Streamlit dataframe count and missing `docs/acceptance_failures_v1.md` registry. Bounded live verification was not repeated because the corrective changes are deterministic identity checks only; the prior bounded exact/no-match observations remain recorded above.

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
- **Status:** COMPLETE
- **Review:** APPROVED
- **Implementation notes:** The bounded shared `backend/medgen.py` NCBI E-utilities client remains behind `MEDGEN_BASE_URL`, `MEDGEN_TIMEOUT`, `MEDGEN_MAX_RETRIES`, and `ENABLE_MEDGEN`. Its `GENE[All Fields]` ESearch result is discovery-only: an ESummary candidate is accepted only when ConceptMeta `AssociatedGenes` deterministically contains the exact normalized query symbol. Candidates with missing association metadata or a non-matching symbol are bounded diagnostics, never accepted disease evidence. Structured ConceptMeta source metadata and deterministic HPO-ID feature overlaps are retained when exposed; absent accepted HPO input keeps the result explicitly gene-only. It performs one normalized same-gene query after the source-labelled MyDisease disease/HPO node is insufficient and fans immutable additive context back to input-ordered variants. Historical EvidenceObject `2.5` records remain readable without MedGen context; the MedGen context schema is additive `1.1` and its legacy `1.0` representation remains readable. MedGen cannot overwrite MyDisease, populate Phen2Gene rank/score, populate GenCC validity, or make a causal/pathogenicity claim. No SQLite or pipeline schema migration was made.
- **Validation evidence:** `tests/test_medgen.py` passed (`17 passed in 0.13s`) for exact association acceptance/rejection, structured ConceptMeta provenance, accepted/no accepted HPO semantics, primary no-match and partial/operational evidence-gap triggers, prerequisite non-triggering, deduplication/fan-out, timeout/`403`/`429`, malformed/schema-drift responses, EvidenceObject serialization, semantic isolation, shared-upstream retention, and legacy MedGen-context readability. The focused Stage 2/3, persistence, and ERC suite passed (`50 passed in 24.61s`); compilation passed. `python tests/run_stage78_resilience_acceptance.py` completed with `1308 passed, 2 failed, 6 skipped in 151.94s`; the only failures remain the manual-table Streamlit dataframe count and missing `docs/acceptance_failures_v1.md`. Bounded public live validation before the correction used only `SCN1A` and `FBN1`; raw provider responses and private data were not retained.

**STAGE CLOSED — APPROVED. Stage 4 requires explicit authorization.**

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
- **Status:** COMPLETE
- **Review:** APPROVED
- **Implementation notes:** Added `enrich_with_medgen_phenotype_gene()` in `backend/medgen.py` to provide source-separated supporting evidence when the `phenotype_gene_support` semantic node is insufficient. Evaluates target-node sufficiency before querying: if semantically valid local phenotype-gene support is already sufficient (via local direct HPO-gene fallback matches, non-empty matched patient HPO terms, or positive local phenotype score), MedGen is marked `not_needed` with `local_support_sufficient`. Explicitly distinguishes Phen2Gene primary states: usable (`available`), operational failure (`unavailable`), valid no-match (`no_match`, `not_found`, `no_association`), and partial/insufficient (`partial`). A valid Phen2Gene no-match is recognized as an evidence-gap trigger rather than negative evidence. Semantic missingness reports `required_fields_missing: ["phenotype_gene_support"]` preserving strict semantic node separation. Queries MedGen with `GENE[All Fields] AND "HPO label"[Clinical Features]` deduplicated per `(gene, HPO)` tuple using canonical accepted HPO terms with non-empty labels. Reuses NCBI E-utilities client resilience (timeouts, retries, circuit breaker) and ConceptMeta verification (`AssociatedGenes` exact match and `ClinicalFeatures` HPO matching via standard MedGen `SDUI` attribute with multi-root XML wrapping). Attaches immutable, source-separated `medgen_phenotype_gene_context` (`schema_version: "1.0"`) per variant with preserved input order and cardinality, without modifying Phen2Gene rank, score, or weight fields, and without altering automated ACMG classifications. Added validation (`_validate_medgen_phenotype_gene_context`) and compaction (`_compact_medgen_phenotype_gene_context`) in `backend/report.py`, retaining provenance, lineage records, and shared upstream groups. Integrated non-blocking fallback in `_run_phenotype_stage` in `backend/pipeline.py`. Historical EvidenceObjects without the optional context remain fully valid.
- **Validation evidence:** `tests/test_medgen_phenotype_gene.py` passed (`23 passed in 0.18s`): multi-root ConceptMeta XML parsing and `SDUI` attribute extraction, Phen2Gene valid no-match trigger, Phen2Gene operational failure trigger, Phen2Gene partial trigger, Phen2Gene usable suppression, Phen2Gene unavailable + local support sufficient suppression (`not_needed`), Phen2Gene unavailable + local support insufficient trigger, Phen2Gene valid no-match + local support insufficient trigger, Phen2Gene fallback match sufficiency without rank/score fabrication, semantic missingness reporting `["phenotype_gene_support"]`, same-query `(gene, HPO)` deduplication across multiple variants with preserved input order and cardinality (4 variants, 2 unique tuples queried), prerequisite not met (no accepted HPO with label), missing gene and provider disabled, exact gene-association requirement and mismatch/unverified candidate diagnostics, SCN1A/seizure support match, FBN1/ectopia lentis multi-HPO support, CFTR/malabsorption valid no-match, timeouts/403/429 status distinctions, malformed responses/schema drift, EvidenceObject serialization round-trip, semantic isolation (no Phen2Gene rank/score modification), lineage/shared upstream tracking, and historical EvidenceObject backward compatibility. `tests/test_medgen.py` passed (`17 passed in 0.25s`). Golden ERC-01 through ERC-07 in `tests/test_stage105_professor_testcase.py` and persistence recovery in `tests/test_stage99_persistence_recovery_v4.py` passed (`52 passed in 25.76s`). Bounded live validation completed with verified live positive controls: `SCN1A + Seizure` yielded accepted record UID 400655 (`C1864987`, *Migraine, familial hemiplegic, 3* with exact gene and `HP:0001250`); `FBN1 + Ectopia lentis` yielded accepted record UID 346932 (`C1858556`, *MASS syndrome* with exact gene and `HP:0001083`); `CFTR + Malabsorption` returned verified clean `no_match` (0 search IDs). `tests/run_stage78_resilience_acceptance.py` passed compilation, secrets audit, and multi-variant resilience, with 1331 passed tests and only the 2 pre-existing baseline failures (manual table dataframe count and missing `docs/acceptance_failures_v1.md`). Approved checkpoint `cf10b20`.

**STAGE CLOSED — APPROVED. Stage 5 requires explicit authorization.**

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
- **Definition of Done:** Shadow/observation comparisons pass first; any subsequently promoted canonical field is exactly validated, transcript-coherent where required, provenance-complete, not falsely attributed to VEP, and introduces no unexplained Golden Case regression.
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
- **Definition of Done:** Coverage is deterministic, per-variant, provenance-linked, keeps raw semantic nodes distinct, and initially remains observational without changing readiness/LLM gating.
- **Status:** NOT_STARTED
- **Review:** NOT_REQUIRED
- **Implementation notes:** None.
- **Validation evidence:** None.

**STOP FOR REVIEW.**

### Stage 8 — Capability and final-disposition integration

- **Objective:** Integrate coverage into capability presentation, reviewer warning semantics, and derived final interpretation disposition.
- **Existing code to reuse:** `backend/evidence_readiness.py`, `backend/pipeline.py`, Stage 114 source-status presentation, report projections.
- **Files/modules likely affected:** `backend/evidence_readiness.py`, `backend/pipeline.py`, `backend/variant_interpretation.py`, `backend/variant_report.py`, frontend review/preview modules, tests.
- **Required changes:** Derive final disposition from internal readiness after enrichment; render coverage and source-specific limitations. Preserve the existing technical provider diagnostics while explicitly separating (1) provider reachability/operational readiness, (2) per-analysis provider query result, and (3) final per-variant evidence capability coverage. The primary reviewer surface must emphasize evidence capability rather than raw provider availability; a red/unreachable provider must not automatically render the corresponding evidence capability unavailable when equivalent/composed evidence remains. Add ERepo and MedGen to the collapsed technical diagnostics with privacy-safe status, attempts/latency where already supported, query/enrichment use, and failure category semantics.
- **Contracts that must not change:** Existing persisted internal readiness enum; no provider outage alone blocks interpretation; human review remains mandatory.
- **Schema impact:** Prefer runtime/projection derivation. Any persisted enum or schema migration requires separate justification, compatibility benefit, tests, and review.
- **Provenance/lineage impact:** Reviewer notices identify capability, source, retrieval state, composition, and limitations.
- **Readiness impact:** `RESCUE_REQUIRED` remains pre-enrichment; `MINIMUM_IDENTITY_FAILURE` may derive `BLOCKED`; structurally safe sparse cases remain `READY_WITH_LIMITATIONS`.
- **Persistence/backward-compatibility impact:** Preserve current readiness audit records unless a separately approved migration is required.
- **Tests:** Internal-to-derived mapping, post-enrichment reassessment, blocked identity, composed annotation, ERepo/MedGen source notices, persisted recovery.
- **Live validation:** One bounded end-to-end run after deterministic acceptance passes.
- **Risks:** Accidentally turning a display concept into a destructive persisted migration.
- **Dependencies:** Stage 7.
- **Definition of Done:** Reviewer-facing disposition is accurate, derived without premature migration, source-specific limitations are retained, and provider reachability/query state remains distinct from final evidence capability state.
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

## 10. Cross-stage test and live-validation plan

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

## 11. File-level implementation map

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

## 12. Roadmap maintenance policy

When executing a future stage:

1. Read this roadmap first.
2. Read `docs/AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md`.
3. Confirm the current authorized stage and its safety/dependency gates.
4. Confirm the working branch, last approved checkpoint, and that unrelated pre-existing working-tree changes will not be overwritten.
5. Perform only the currently approved stage.
6. Run the required pre-existing deterministic regression baseline plus stage-specific tests and Golden Case comparisons.
7. Update that stage's **Implementation notes** and **Validation evidence** with observed, verified facts, including baseline/Golden Case differences and rollback checkpoint where applicable.
8. Mark the stage:

   ```text
   Status: COMPLETE
   Review: PENDING
   ```

9. Stop and wait for architecture review.
10. Only after explicit approval may that stage become:

   ```text
   Review: APPROVED
   ```

11. Create/retain an independently revertible approved-stage checkpoint according to repository conventions.
12. Begin the next stage only after the prior stage is approved.

Never mark a stage `COMPLETE` merely because code was written. Completion requires its Definition of Done, required deterministic tests, compatibility checks, and applicable Golden Case comparisons to pass. Use `CHANGES_REQUESTED` when review identifies unresolved architectural, safety, provenance, regression, compatibility, or validation issues. An unexplained regression blocks approval. A later-stage failure must be recovered by reverting/amending that stage rather than discarding earlier approved stages.
