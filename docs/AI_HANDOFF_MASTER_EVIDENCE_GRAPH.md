# AI HANDOFF MASTER — Clinical Variant Interpretation / Evidence Graph

**Document purpose:** Master handoff for any AI, engineer, reviewer, or coding agent continuing this project.  
**Project:** Clinical Variant Interpretation  
**Primary implementation:** Python + Streamlit  
**Current input scope:** 1–10 already-filtered germline Mendelian variants  
**Current implementation baseline:** Stages 0–114 implemented; evidence-resilience Stages 1–10 COMPLETE / APPROVED; Stage 11 COMPLETE / REVIEW PENDING; Stage 115 final visual sign-off remains separate  
**Handoff status:** Evidence-resilience implementation and documentation closeout completed; no automatic successor  
**Last architecture update:** 2026-08-17 (+03:30)  
**Primary source documents:** `PROJECT_DECLARATION(1).md`, `Evidence Graph Architecture Audit` (2026-08-16), and user-run API probe logs V1–V4

---

# 1. What this project actually is

This project is **not a raw-VCF filtering engine, not a genome-wide prioritization engine, and not an autonomous ACMG classifier**.

The user supplies **1 to 10 variants that have already been filtered/selected upstream**. The system’s job is to collect as much high-quality, source-attributed evidence as possible for each selected allele, normalize that evidence into a structured per-variant Evidence Object / Evidence Pack, send that structured evidence to one selected interpretation LLM, preserve all limitations and conflicts, then present the result for human review and final confirmation.

The correct mental model is:

```text
1–10 already-filtered variants
        ↓
exact identity / normalization
        ↓
multi-provider evidence collection
        ↓
evidence graph expansion
        ↓
structured per-variant Evidence Object
        ↓
readiness + conflict + missingness + provenance
        ↓
one selected interpretation LLM per variant
        ↓
human review / edits / include-exclude decision
        ↓
final report
```

The project is **evidence-centered**. APIs are not the product. Each API is only a provider for one or more evidence nodes.

---

# 2. Why the current resilience/evidence work started

The practical problem observed in real runs was:

- some selected variants received good interpretations;
- some reached the LLM with repeated warnings such as incomplete annotation or missing evidence;
- some variants received weak or no useful interpretation;
- provider reachability dashboards alone did not explain why.

This triggered a deeper architecture review.

The central question is now:

> For every selected variant, can the system still build a sufficiently complete and trustworthy Evidence Pack even when one or more providers are unreachable, return no record, or provide only partial data?

The project should **not** respond to this by simply adding many APIs.

Instead:

1. determine which evidence fields are actually required;
2. determine which current providers already supply those fields;
3. compose evidence at the **field level** when necessary;
4. add a new API only when a real evidence-node gap remains.

---

# 3. Hard project boundaries

## In scope

- 1–10 already-filtered germline Mendelian variants.
- VCF / VCF.GZ / first worksheet of XLSX / manual VCF-style input.
- Explicit GRCh37 or GRCh38.
- SNV/indel normalization and identity checking.
- Multiallelic splitting.
- Preservation of input order.
- Multi-source annotation and clinical evidence collection.
- Population evidence.
- Gene–disease validity.
- Phenotype/HPO context.
- Gene/variant/disease literature.
- External automated classification context.
- Source provenance, shared-upstream tracking, conflict handling.
- Per-variant LLM interpretation.
- Human review and editable reports.
- Final report generation.
- SQLite persistence/recovery.
- Provider resilience and explicit degraded states.

## Explicitly out of scope

- Raw VCF filtering.
- Genome-wide prioritization.
- Variant ranking or Top-N selection.
- Autonomous pathogenicity classification.
- Autonomous diagnosis.
- Automatic execution of ClinGen CSpec rules.
- Treatment recommendations.
- Sending raw VCF rows, genotypes, sample names, or patient columns to the LLM.
- Replacing qualified human genetics review.
- Production-grade clinical regulatory certification.

---

# 4. Safety and interpretation philosophy

The LLM is an **evidence-bounded interpretation layer**, not a source of biological truth.

The system must never pretend that unavailable evidence exists.

Mandatory distinctions:

```text
provider unreachable        != evidence absent
provider timeout            != evidence absent
provider valid no-match     != provider failure
provider no-match           != negative pathogenicity evidence
optional evidence missing   != variant uninterpretable
mirror of same dataset      != independent biological confirmation
aggregator-derived evidence != independent vote
```

Examples:

- MyVariant gnomAD fields, direct gnomAD, and UCSC gnomAD are operationally different routes to overlapping underlying population data, not independent cohorts.
- Direct NCBI ClinVar and ClinVar-derived fields from MyVariant or GeneBe are not independent clinical votes.
- UCSC GenCC is a transport path for GenCC/ClinGen evidence; transport redundancy and biological-evidence independence are different concepts.

---

# 5. Current implementation status

The project declaration records **Stages 0–114 as implemented**.

Important completed work includes:

- input normalization and privacy boundaries;
- VEP, GeneBe, MyVariant, ClinVar, GenCC, CSpec, Phen2Gene, MyDisease, gnomAD, literature integrations;
- Evidence Object V2;
- evidence lineage and shared-source collapsing;
- deterministic conflict auditing;
- provider fallback framework;
- exact allele validation;
- bounded retries/timeouts/circuit breakers;
- no-match rescue;
- cross-provider identifier intelligence;
- exact ClinVar retrieval hardening;
- scoped CSpec applicability;
- deterministic evidence readiness;
- secondary classification recovery;
- interpretation retry/recovery;
- warning semantics;
- provider diagnostics;
- per-variant report lifecycle;
- DOCX report pipeline;
- persistence/recovery;
- large deterministic test/acceptance suite.

Current recorded test baseline:

```text
1133 passed
6 skipped
Stage 59 coverage: 85.83%
```

Pending report-related work remains separate:

- Stage 83 manual Word/LibreOffice visual fidelity comparison.
- Stage 115 final visual sign-off.

Do not confuse those visual/report tasks with the current evidence-resilience workstream.

## Post-declaration resilience work completed on 2026-08-16

A separate API/evidence-resilience audit was performed after the Stage 0–114 declaration.

User-run functional probes established the following current-network decisions:

- **ClinGen Evidence Repository (ERepo): selected for integration** as expert-curated variant classification context. It repeatedly returned `FUNCTIONAL_OK / HTTP 200`.
- **NCBI MedGen via E-utilities: selected for integration** for disease/HPO context and for carefully scoped degraded supporting evidence in phenotype-gene-disease and gene-disease nodes. It repeatedly returned `FUNCTIONAL_OK / HTTP 200`.
- **Monarch Initiative:** both tested hosts returned `HTTP 403 FORBIDDEN`.
- **Open Targets GraphQL:** returned `HTTP 403 FORBIDDEN`.
- **Genomics England PanelApp:** returned `HTTP 403 FORBIDDEN`.
- **EMBL-EBI Gene2Phenotype (G2P):** TLS handshake timed out.
- **Orphadata/Orphanet API:** both gene-disease and HPO-disease probes timed out during TLS handshake.
- **Direct ClinGen Gene-Disease Validity feed:** timed out on the tested network.
- The API discovery phase is therefore **closed for the current implementation cycle**. Do not resume broad API hunting unless a concrete evidence-node failure remains after integration.

These probe results are **operational observations on the user's tested network**, not universal claims about provider uptime.

---

# 6. Current end-to-end pipeline

```text
Filtered input variants
    ↓
Validation / minimization / normalization
    ↓
annotate_variants()
    ├─ Ensembl VEP
    ├─ VariantValidator after operational VEP failure
    ├─ GeneBe
    ├─ MyVariant.info
    ├─ normal population continuation via UCSC gnomAD when needed
    ├─ direct NCBI ClinVar
    ├─ UCSC GenCC / ClinGen claims
    └─ ClinGen CSpec metadata
    ↓
Phenotype layer
    ├─ accepted HPO terms
    ├─ local HPO validation
    ├─ Phen2Gene
    └─ MyDisease
    ↓
preliminary EvidenceObject
    ↓
EvidenceReadiness audit
    ↓
conditional enrichment when triggered
    ├─ population verification
    │    direct gnomAD → UCSC gnomAD → Ensembl Variation
    └─ literature
         LitVar2 → Europe PMC → PubMed
    ↓
final EvidenceObject
    ↓
final readiness/conflict state
    ↓
interpret_variants()
    ↓
one LLM interpretation per variant
    ↓
human review / edit / include-exclude / confirmation
    ↓
final report / per-variant DOCX artifacts
```

---

# 7. The Evidence Graph model — target mental model

The project must no longer be thought of as a set of simple API fallback chains.

The correct model is an **Evidence Graph / dependency-aware DAG**.

## 7.1 Core graph

```text
                       ┌──────────────────────┐
                       │  INPUT VARIANT       │
                       │ assembly + CPRA      │
                       └──────────┬───────────┘
                                  │
                                  ▼
                       ┌──────────────────────┐
                       │ Canonical Identity   │
                       │ exact allele / HGVS  │
                       └──────────┬───────────┘
                                  │
             ┌────────────────────┼────────────────────┐
             │                    │                    │
             ▼                    ▼                    ▼
       ANNOTATION            POPULATION           CLINICAL
             │                    │                    │
             ▼                    ▼                    ▼
      gene/transcript        AF/AC/AN            ClinVar
             │                                         │
             ▼                                         │
          GENE NODE                                     │
             │                                         │
       ┌─────┼───────────┬──────────────┐              │
       ▼     ▼           ▼              ▼              │
     GenCC  CSpec     Phenotype       Literature        │
                      /Disease                          │
       │      │           │              │              │
       └──────┴───────────┴──────────────┴──────────────┘
                                  │
                                  ▼
                          STRUCTURED EVIDENCE
                                  │
                                  ▼
                               LLM
```

## 7.2 Entity nodes

- Variant
- Gene
- Transcript
- Disease
- HPO term
- Publication

## 7.3 Capability / evidence nodes

- exact variant identity
- transcript consequence
- HGVS.c / HGVS.p
- population frequency
- direct clinical classification
- automated ACMG context
- gene–disease validity
- phenotype–gene relationship
- disease/HPO context
- functional prediction
- literature
- CSpec metadata
- conflict state
- provenance / lineage

## 7.4 Provider nodes

- Ensembl VEP
- VariantValidator
- GeneBe
- MyVariant.info
- NCBI ClinVar
- UCSC gnomAD
- direct gnomAD
- Ensembl Variation
- UCSC GenCC
- ClinGen CSpec
- Phen2Gene
- MyDisease.info
- LitVar2
- Europe PMC
- PubMed
- configured interpretation LLM

---

# 8. Current Evidence Object / Evidence Pack

The current final Evidence Object is already fairly evidence-centric.

Conceptually it contains:

```text
EvidenceObject
├── stable variant identity
├── gene / transcript / consequence / impact
├── HGVS / protein change
├── population frequency
├── ClinVar evidence
├── ClinGen/GenCC evidence
├── HPO / phenotype context
├── variant_context
├── annotations
│   ├── vep
│   ├── genebe
│   ├── population
│   └── predictors
├── pathogenicity
│   ├── GeneBe automated ACMG context
│   ├── ClinVar
│   ├── GenCC
│   └── CSpec
├── phenotype_relationship
│   ├── accepted HPO information
│   ├── Phen2Gene
│   └── MyDisease
├── provenance / lineage
├── shared-upstream groups
├── conflict audit
├── conditional enrichment
├── capability results
└── missing / degraded status
```

The whole sanitized Evidence Object is serialized to the variant interpretation LLM.

Therefore the project’s current main weakness is **not** simply “the LLM only sees a tiny provider summary.”

The bigger issue is what gets correctly promoted, composed, and represented **inside the Evidence Object before the LLM call**.

---

# 9. Important result from the 2026-08-16 Evidence Graph audit

The current architecture is **MIXED**:

- evidence-centric in final EvidenceObject, provenance, conflict auditing, and LLM payload;
- provider-centric in acquisition, fallback declarations, readiness, and field promotion.

The audit’s most important finding:

> Some evidence already exists under provider-specific subtrees but is not allowed to satisfy the canonical evidence field used by readiness or higher-level interpretation logic.

This is especially important for VEP outage behavior.

---

# 10. Variant annotation — the most important composition gap

Current primary:

```text
Ensembl VEP
```

Current explicit fallback:

```text
VariantValidator
```

But VariantValidator intentionally does not provide the full VEP consequence model.

The audit found that when VEP is unavailable, the combination of:

```text
VariantValidator
+ GeneBe
+ MyVariant.info
```

already retains approximately **5 of 6 critical annotation fields (~83%)** in the audit model:

```text
exact CPRA + assembly
gene
transcript
HGVS.c
HGVS.p
consequence
```

The key problem:

- GeneBe may already contain a valid consequence.
- But GeneBe’s consequence remains under provider-specific evidence.
- The top-level canonical consequence is still effectively owned by VEP.
- Therefore readiness may report annotation/consequence as missing even when equivalent usable evidence already exists.

This is a **field-composition problem before it is an API problem**.

## Current decision

Do **not** add another VEP-like API yet.

First implement a controlled, provenance-preserving annotation composition policy.

Target behavior:

```text
If VEP succeeds:
    canonical annotation ← VEP

If VEP operationally fails:
    identity/HGVS ← VariantValidator where exact
    consequence/transcript/HGVS ← GeneBe where exact and valid
    identifiers/context ← MyVariant where exact
    canonical composed annotation ← validated field-level composition
```

Rules:

- never silently pretend GeneBe is VEP;
- retain exact source per field;
- retain assembly/REF/ALT identity checks;
- do not create unsupported fields;
- do not overwrite valid primary evidence;
- expose composed evidence status separately from provider status.

---

# 11. Population evidence — currently considered sufficiently resilient

Normal population frequency:

```text
MyVariant.info
    ↓ if no usable exact AF / operational gap
UCSC gnomAD exact allele
```

Conditional population verification:

```text
direct gnomAD
    ↓
UCSC gnomAD
    ↓
Ensembl Variation
```

Important semantic rule:

- UCSC gnomAD is an operational access path/mirror of gnomAD, not an independent population cohort.
- MyVariant may aggregate gnomAD plus additional exact-ALT sources; source provenance must remain visible.
- Missing frequency must never be converted to zero.

## Current decision

Do **not** prioritize adding another population API now.

---

# 12. Direct clinical evidence / ClinVar

Primary:

```text
NCBI ClinVar
```

Operational recovery:

```text
MyVariant ClinVar-derived fields
```

Important:

- this is not independent clinical confirmation;
- both share ClinVar upstream evidence;
- direct ClinVar remains the preferred source;
- no-match is valid missingness, not negative evidence.

Current implementation already includes:

- exact retrieval hardening;
- SPDI / HGVS / stable-identifier strategies;
- exact allele validation;
- no-match rescue logic;
- secondary classification recovery.

---

# 13. GeneBe automated ACMG context

GeneBe supplies automated ACMG classification context, criteria, score, annotation context, predictors, population context, and some derived clinical context.

Important project rule:

> GeneBe is evidence/context. It is not the application's final classification.

If GeneBe is unavailable, interpretation must not automatically become impossible.

## Selected backup: ClinGen Evidence Repository (ERepo)

**Decision: integrate ERepo.**

User-run connectivity/function probes on 2026-08-16 repeatedly returned:

```text
ClinGen Evidence Repository
HTTP 200
FUNCTIONAL_OK
```

The tested ERepo interpretation record returned structured fields including `@id`, `uuid`, `variant`, `condition`, `statementOutcome`, `assertionMethod`, and `metadata`.

ERepo is expert-curated, variant-level, and high-value when a ClinGen/VCEP curation exists. It is **not** a universal automated classifier, not a drop-in GeneBe clone, and not guaranteed to have a record for every arbitrary variant.

Target behavior:

```text
GeneBe available
    ↓
retain GeneBe automated ACMG context

GeneBe unavailable or no usable result
    ↓
query ClinGen ERepo exact variant
    ↓
if curated record exists:
    retain expert-curated classification/evidence context
else:
    valid no_match
    continue with ClinVar + GenCC + CSpec + population + phenotype + literature
```

If GeneBe is unavailable but ERepo or other clinical evidence exists, automated ACMG context remains **DEGRADED/non-equivalent**. Do not fabricate a GeneBe-equivalent score and do not block interpretation solely for GeneBe outage.

---

# 14. Gene–disease validity

Current primary:

```text
UCSC GenCC track
    ↓
ClinGen/GenCC-family gene-disease validity claims
```

This remains the preferred source for actual validity-classification semantics.

## Online fallback decision after testing

No second tested provider was both operationally accessible **and** semantically equivalent to ClinGen/GenCC validity on the user's current network.

```text
PanelApp       → HTTP 403 FORBIDDEN
Monarch        → HTTP 403 FORBIDDEN
Open Targets   → HTTP 403 FORBIDDEN
G2P            → TLS handshake TIMEOUT
Direct ClinGen GDV feed → TIMEOUT
```

For this implementation cycle:

```text
UCSC GenCC available
    ↓
gene_disease_validity = FULL

UCSC GenCC unavailable
    ↓
NCBI MedGen gene-disease supporting context
    ↓
gene_disease_support = AVAILABLE
gene_disease_validity = DEGRADED
```

V4 gene-only MedGen probes returned usable disease/condition summaries for all three public genes:

```text
SCN1A → 5 matching MedGen records
FBN1  → 9 matching MedGen records
CFTR  → 22 matching MedGen records
```

ESummary JSON parsing succeeded. This proves that MedGen can keep gene↔disease supporting context populated when GenCC is unavailable, but it does **not** supply ClinGen Definitive/Strong/Moderate/Limited classes, an equivalent GCEP curation, or an independent validity classification.

Never transform a MedGen match into a ClinGen/GenCC validity class.

---

# 15. Phenotype-gene evidence

Current primary remote provider:

```text
Phen2Gene
```

Phen2Gene provides phenotype-to-gene ranking, gene score, and provider rank.

## Selected degraded online fallback: NCBI MedGen composite evidence

No tested public provider supplied an accessible second Phen2Gene-equivalent ranking service on the current network. The accepted fallback is therefore **not another ranker**.

```text
accepted patient HPO term
    ↓
canonical HPO label
    +
already-known variant gene
    ↓
MedGen query:
GENE[gene] AND "phenotype label"[clinical features]
    ↓
matching disease/condition records
    ↓
phenotype↔gene↔disease supporting evidence
```

### V4 empirical results

```text
SCN1A + seizure       → FUNCTIONAL_OK, count=1
FBN1 + ectopia lentis → FUNCTIONAL_OK, count=5
CFTR + malabsorption  → FUNCTIONAL_NO_MATCH, count=0
```

V4 suitability decision:

```text
PROMISING_AS_DEGRADED_SUPPORT
```

MedGen is **not** Phen2Gene. Never normalize MedGen output into `phen2gene_score` or `phen2gene_rank`. Store it as separately attributed phenotype-gene supporting evidence.

A valid no-match is missingness, not evidence against pathogenicity or proof of phenotype incompatibility.

Target behavior:

```text
Phen2Gene usable
→ phenotype_gene = FULL

Phen2Gene unavailable + MedGen supporting matches
→ phenotype_gene = DEGRADED

Phen2Gene unavailable + MedGen valid no_match
→ phenotype_gene = DEGRADED/sparse
→ interpretation may still proceed as READY_WITH_LIMITATIONS if minimum evidence is otherwise safe
```

---

# 16. Disease/HPO context

Current primary:

```text
MyDisease.info
```

## Selected online fallback: NCBI MedGen via E-utilities

**Decision: integrate MedGen.**

User-run probes repeatedly demonstrated `DNS=OK`, `HTTP=200`, and functional ESearch/ESummary retrieval. MedGen successfully supported disease/condition search, gene-associated disease records, clinical-feature queries, ESearch, ESummary JSON parsing, and normalized disease/condition summary extraction.

Target:

```text
MyDisease usable
    ↓
use MyDisease disease/HPO context

MyDisease unavailable / unusable
    ↓
NCBI MedGen
    ↓
disease/condition + phenotype + gene supporting context
```

MedGen can return broad disease/condition records associated with a gene. A returned record is candidate/supporting context, not automatic proof that the patient's variant causes that disease.

Normalize/filter by exact gene context, condition identifiers, disease title, phenotype/clinical-feature overlap, source metadata where available, inheritance/mechanism context where available elsewhere, and cross-provider agreement/conflict. Preserve provider and role explicitly.

---

# 17. Literature evidence

Current chain:

```text
LitVar2
    ↓ operational/invalid failure
Europe PMC
    ↓ operational/invalid failure
PubMed
```

Current behavior is primarily a **resilience chain**, not always a complementary literature aggregator.

A valid `no_match` from an earlier source can terminate that branch.

Because the project only processes 1–10 already-filtered variants, future design may choose to use complementary literature aggregation for high-value cases such as VUS, conflicts, or sparse evidence.

Do not change this blindly; first define when complementary search adds meaningful evidence without unnecessary duplication.

---

# 18. Provider reachability is not evidence availability

Current readiness UI is historically provider-centric.

Example:

```text
VEP = unreachable
GeneBe = reachable
VariantValidator = reachable
MyVariant = reachable
```

This should not automatically render as:

```text
Variant annotation = unavailable
```

If critical annotation evidence can be composed from other validated sources, capability can still be:

```text
Variant annotation = FULL
```

Target future UI model should have two layers:

```text
Provider status
---------------
VEP              UNREACHABLE
GeneBe           REACHABLE
VariantValidator REACHABLE
MyVariant        REACHABLE

Evidence capability status
--------------------------
Identity             FULL
Annotation           FULL
Population           FULL
Clinical             FULL
Gene-disease         PARTIAL/FULL
Phenotype            PARTIAL
Automated ACMG ctx   DEGRADED
```

---

# 19. Evidence readiness semantics

Current Stage 111 contract:

```text
READY
READY_WITH_LIMITATIONS
BLOCKED
```

Interpretation should proceed for:

```text
READY
READY_WITH_LIMITATIONS
```

`BLOCKED` should be reserved for true minimum-evidence or structural/safety failure.

Therefore:

> Missing optional or non-critical evidence must not automatically suppress interpretation.

A later targeted failure-driven audit should distinguish:

- evidence truly absent;
- provider unreachable;
- parser did not retain data;
- normalized evidence not promoted;
- readiness incorrectly marked degraded;
- LLM payload missing data;
- LLM output failure;
- prompt/gating too strict.

---

# 20. Existing resilience framework — do not rebuild it from scratch

The project already contains:

- normalized provider operational status taxonomy;
- bounded retry/backoff;
- separate connect/read deadlines;
- analysis-scoped circuits;
- fallback provenance;
- exact allele validation;
- no-match semantics;
- evidence rescue;
- identifier intelligence;
- source correlation/lineage;
- conflict auditor;
- readiness audit;
- per-variant failure isolation;
- provider diagnostics;
- reviewer-safe warnings;
- persisted evidence and interpretation recovery.

Any new provider must integrate into these existing abstractions.

Do not bolt new ad-hoc JSON blobs directly into the LLM prompt.

---

# 21. Observed network-resilience context

Manual user-run tests on 2026-08-16 demonstrated material provider/network differences. These are **operational observations from the tested connection**, not permanent availability claims.

## Probe V1

```text
Monarch current host    → 403 FORBIDDEN
Monarch api-v3 host     → 403 FORBIDDEN
Open Targets GraphQL    → 403 FORBIDDEN
PanelApp REST           → 403 FORBIDDEN
ClinGen ERepo           → 200 FUNCTIONAL_OK
NCBI MedGen             → 200 FUNCTIONAL_OK
```

## Probe V2

```text
EMBL-EBI Gene2Phenotype → TLS handshake TIMEOUT
Orphadata gene-disease  → TLS handshake TIMEOUT
Orphadata HPO-disease   → TLS handshake TIMEOUT
ClinGen ERepo           → 200 FUNCTIONAL_OK
NCBI MedGen             → 200 FUNCTIONAL_OK
```

## Probe V3

```text
Direct ClinGen GDV feed → TIMEOUT
MedGen gene + clinical feature → ESearch HTTP 200, count=1
MedGen gene + HPO Source-ID → HTTP 200, valid no_match
```

The initial V3 ESummary failure was a **test-script parser issue**, not an API-connectivity failure. The HPO `Source ID` query form was also not appropriate for the intended phenotype-gene compatibility use.

## Probe V4 — corrected MedGen evidence-graph test

Phenotype-gene-disease support:

```text
SCN1A + seizure       → FUNCTIONAL_OK, count=1
FBN1 + ectopia lentis → FUNCTIONAL_OK, count=5
CFTR + malabsorption  → FUNCTIONAL_NO_MATCH, count=0
```

Suitability: `PROMISING_AS_DEGRADED_SUPPORT`.

Gene-disease supporting context:

```text
SCN1A → FUNCTIONAL_OK, count=5
FBN1  → FUNCTIONAL_OK, count=9
CFTR  → FUNCTIONAL_OK, count=22
```

Suitability: `PROMISING_AS_DEGRADED_GENE_DISEASE_CONTEXT`. ESummary JSON parsing succeeded.

## Provider-selection consequence

Selected for integration:

```text
ClinGen ERepo
NCBI MedGen
```

Not selected for this cycle due tested network behavior:

```text
Monarch
Open Targets
PanelApp
Gene2Phenotype
Orphadata
Direct ClinGen GDV feed
```

If a second network is available later, the same probes may be rerun to document operational diversity, but this should not reopen broad provider accumulation unless a concrete evidence gap remains.

---

# 22. Multi-variant execution model

The system accepts at most 10 variants.

Because variants are already filtered, prioritize **evidence completeness and scientific quality over extreme throughput optimization**.

Current good batching/reuse:

- VEP batched.
- GeneBe batched.
- Phen2Gene queried once for the analysis-wide target-gene set.
- MyDisease reused per unique gene.

Current optimization opportunities:

- deduplicate GenCC by gene;
- deduplicate CSpec by gene + disease scope;
- deduplicate repeated ClinVar / MyVariant identity queries;
- deduplicate identical literature queries;
- reuse gene-level evidence across variants in the same gene while preserving per-variant provenance;
- potentially parallelize bounded independent work later.

Performance optimization is secondary to evidence correctness.

---

# 23. Historical pre-closeout gaps and prioritization (2026-08-16)

## Gap 1 — VEP outage field composition

**Priority:** very high  
**Status:** OPEN — implementation required  
**New API needed:** NO

```text
VEP unavailable
    ↓
VariantValidator + GeneBe + MyVariant
    ↓
controlled field-level composition
    ↓
canonical annotation with per-field provenance
```

This remains the highest-priority architecture fix.

## Gap 2 — gene-disease validity exact online redundancy

**Status:** PARTIALLY MITIGATED

No accessible equivalent validity-classification provider was identified on the current network. Accepted degraded fallback:

```text
GenCC unavailable
    ↓
MedGen gene-disease supporting context
    ↓
gene_disease_validity = DEGRADED
```

## Gap 3 — GeneBe automated ACMG context redundancy

**Status:** MITIGATED, NOT EQUIVALENT

```text
GeneBe
    ↓
ClinGen ERepo exact curated evidence
```

ERepo is not a universal automated ACMG replacement.

## Gap 4 — Phen2Gene online fallback

**Status:** MITIGATED WITH DEGRADED SUPPORT

```text
Phen2Gene unavailable
    ↓
MedGen known-gene + accepted-phenotype query
    ↓
phenotype-gene-disease supporting context
```

Never create a fake Phen2Gene rank/score.

## Gap 5 — MyDisease online fallback

**Status:** MITIGATED

```text
MyDisease
    ↓
NCBI MedGen
```

## Overall conclusion

The broad API-discovery phase is **closed for the current implementation cycle**. Do not add additional providers unless implementation/testing demonstrates a specific field or evidence-node gap that cannot be filled by the selected architecture.

---

# 24. Historical pre-closeout candidate API audit (2026-08-16)

The candidate-provider audit has been completed for the current network.

| Provider | Tested role | User-network result | Current decision |
|---|---|---:|---|
| ClinGen Evidence Repository | curated variant classification context | `200 FUNCTIONAL_OK`, repeated | **SELECT / INTEGRATE** |
| NCBI MedGen | disease/HPO context | `200 FUNCTIONAL_OK`, repeated | **SELECT / INTEGRATE** |
| NCBI MedGen | phenotype-gene-disease support | V4: 2/3 functional matches, 1 valid no-match | **SELECT as DEGRADED SUPPORT** |
| NCBI MedGen | gene-disease supporting context | V4: 3/3 functional | **SELECT as DEGRADED SUPPORT** |
| Monarch Initiative | gene-disease / phenotype | `403 FORBIDDEN` on both tested hosts | **DO NOT INTEGRATE NOW** |
| Open Targets | target/gene-disease context | `403 FORBIDDEN` | **DO NOT INTEGRATE NOW** |
| PanelApp | gene-disease/panel evidence | `403 FORBIDDEN` | **DO NOT INTEGRATE NOW** |
| EMBL-EBI Gene2Phenotype | monogenic gene-disease models | TLS handshake timeout | **DO NOT INTEGRATE NOW** |
| Orphadata | gene-disease + disease/HPO | TLS handshake timeout | **DO NOT INTEGRATE NOW** |
| Direct ClinGen GDV feed | direct validity access path | timeout | **DO NOT USE AS CURRENT FALLBACK** |

### ERepo semantics

Use for expert-curated variant classification/evidence. Do not use as a universal GeneBe automated ACMG clone.

### MedGen semantics

Use for disease/HPO context, gene-disease supporting context, and phenotype-gene-disease supporting compatibility. Do not use as a Phen2Gene rank/score, ClinGen/GenCC validity class, or independent pathogenicity vote.

## Provider selection is frozen

For the next implementation cycle, provider selection is intentionally frozen to prevent API accumulation. Reopen discovery only if ERepo/MedGen integration fails scientifically or operationally, a critical Evidence Graph field remains genuinely unpopulated, or a new provider demonstrably fills that specific field with acceptable semantics and access.

---

# 25. Historical pre-closeout implementation plan (2026-08-16)

The project is no longer in broad API-discovery mode. The next phase is **implementation and evidence-composition hardening**.

## Step 1 — integrate ClinGen ERepo

Requirements: exact variant identity matching, valid no-match semantics, provenance, expert-curated classification/evidence extraction, no relabeling as GeneBe, no autonomous ACMG conclusion, deterministic mocked tests, and bounded live tests.

## Step 2 — integrate NCBI MedGen in three explicitly separated roles

```text
Role A: MyDisease fallback → disease/HPO context
Role B: Phen2Gene unavailable → degraded phenotype-gene-disease support
Role C: GenCC unavailable → degraded gene-disease supporting context
```

Never expose Role B as `phen2gene_score/rank`. Never expose Role C as a ClinGen validity class.

## Step 3 — implement VEP-outage annotation field-level composition

Use VariantValidator + GeneBe + MyVariant where exact/valid, with per-field provenance, conflict preservation, and no silent relabeling as VEP.

## Step 4 — implement an Evidence Coverage Calculator

Per variant:

```text
Identity                FULL
Annotation              FULL / DEGRADED / UNAVAILABLE
Population              FULL / DEGRADED / UNAVAILABLE
Clinical                FULL / DEGRADED / UNAVAILABLE
Gene-disease            FULL / DEGRADED / UNAVAILABLE
Phenotype-gene          FULL / DEGRADED / UNAVAILABLE
Disease/HPO             FULL / DEGRADED / UNAVAILABLE
Automated ACMG context  FULL / DEGRADED / UNAVAILABLE
Literature              FULL / DEGRADED / NOT_TRIGGERED
```

Also track critical/important/optional field coverage, correlated-source warnings, operational failure vs valid no-match vs true absence, composed evidence, and provenance completeness.

## Step 5 — update readiness semantics around degraded support

```text
GenCC unavailable + MedGen support
→ gene-disease = DEGRADED, not automatically BLOCKED

Phen2Gene unavailable + MedGen support
→ phenotype-gene = DEGRADED
→ READY_WITH_LIMITATIONS may remain appropriate

GeneBe unavailable + ERepo curated record
→ classification context remains available but non-equivalent
```

## Step 6 — deterministic and live tests

Test success, no-match, timeout, forbidden/rate-limit, malformed payload, parser/schema drift, duplicate/correlated evidence, and per-variant failure isolation. If a second real internet connection is available, rerun live probes to document operational diversity.

## Step 7 — failure-driven interpretation audit

Trace input → normalization → provider calls → raw evidence → parser → normalized evidence → EvidenceObject → coverage/readiness → LLM payload → LLM output. Distinguish provider failure, no data, identity mismatch, parser/drop errors, readiness/gating errors, truncation, and true evidence absence.

Only after this should LLM warning/gating behavior be changed.

---

# 26. Definition of Done for the current resilience workstream

The current workstream should be considered complete when:

1. Every critical Evidence Graph node has either:
   - at least two useful online provider paths, or
   - a documented reason why a second provider does not exist / is not scientifically justified.

2. VEP outage does not create false missing annotation when equivalent validated fields already exist in GeneBe / VariantValidator / MyVariant.

3. Provider availability is displayed separately from evidence capability availability.

4. Evidence coverage is computed per variant.

5. Missingness is categorized as:
   - operational failure,
   - no-match,
   - not queried,
   - true evidence absence,
   - partial/composed evidence.

6. Correlated sources are never counted as independent confirmation.

7. Each provider’s evidence retains:
   - value,
   - provider,
   - underlying dataset where known,
   - assembly,
   - exact allele identity where applicable,
   - retrieval status,
   - method,
   - release/version where available.

8. All selected backup providers pass:
   - deterministic mocked tests,
   - live schema checks,
   - network reachability checks on the user’s target connections.

9. Sparse but valid evidence remains interpretable as `READY_WITH_LIMITATIONS`.

10. No provider outage alone suppresses interpretation if minimum valid evidence remains.

11. Warning wording reflects actual reviewer consequence rather than raw provider status.

---

# 27. Rules future AIs must follow

## Architecture rules

- Think **Evidence Graph**, not API chain.
- Think **field-level evidence coverage**, not merely provider status.
- Do not add a provider just because another endpoint exists.
- Do not assume one provider must exactly clone another if several providers can jointly reconstruct required evidence.
- Preserve dependencies: Variant → Gene → Disease/HPO/CSpec/Literature.

## Scientific rules

- Exact REF/ALT and assembly matching is mandatory wherever variant identity matters.
- Missing data is never zero.
- No-match is not negative pathogenicity evidence.
- Phenotype non-concordance is not benign/pathogenic evidence by itself.
- CSpec metadata is context-only; do not execute or imply rule application unless a future explicit rule engine is built and validated.
- GeneBe classification is external evidence, not the application’s final verdict.
- ClinVar-derived copies are not independent ClinVar votes.
- gnomAD mirrors are not independent cohorts.
- PanelApp / Monarch / Open Targets semantics must not be relabeled as ClinGen validity classes.

## LLM rules

- Do not send raw VCF/sample/genotype/patient columns to the interpretation model.
- Do not send unnormalized raw API payloads directly as a substitute for EvidenceObject.
- The interpretation model receives the normalized sanitized Evidence Object.
- One selected interpretation model handles all variants; conflict changes prompt context, not selected model.
- Human review remains mandatory.

## Resilience rules

- Target architecture for new redundancy work is online/API-driven.
- Existing local fallbacks may remain documented but should not be counted as satisfying online redundancy goals.
- Preserve bounded retry and circuit-breaker semantics.
- Preserve provider-specific source labels.
- Never silently relabel fallback data as primary-provider data.

## Product rules

- Input variants are already filtered.
- Do not reintroduce ranking, prioritization, or Top-N selection.
- Same-gene variants remain separate variants and separate report records.
- Input order must remain stable.
- One variant failure must not delete another variant’s evidence or interpretation.
- Finalization must not rerun interpretation.

---

# 28. Historical pre-closeout API map (2026-08-16)

## Variant annotation

```text
Primary: Ensembl VEP
Operational mapping/HGVS fallback: VariantValidator
Complementary: GeneBe + MyVariant.info
Next need: field-level composition, not a new provider
```

## Variant context

```text
Primary: MyVariant.info
Fallback: Ensembl Variation exact overlap
```

## Population

```text
Normal: MyVariant → UCSC gnomAD
Conditional verification: direct gnomAD → UCSC gnomAD → Ensembl Variation
```

These are operationally diverse access paths to overlapping datasets, not independent biological cohorts.

## Clinical classification

```text
Primary: NCBI ClinVar
Derived recovery: MyVariant ClinVar-derived
Automated context: GeneBe
Selected curated backup/context: ClinGen ERepo
```

## Gene-disease validity

```text
Primary validity source: UCSC GenCC / ClinGen-family
If unavailable: NCBI MedGen gene-disease supporting context
FULL when GenCC validity available
DEGRADED when only MedGen support remains
```

Not selected now due tested network behavior: PanelApp (403), Monarch (403), Open Targets (403), G2P (TLS timeout), Direct ClinGen GDV feed (timeout).

## CSpec

```text
ClinGen CSpec Registry
context metadata only
no automatic rule execution
```

## Phenotype-gene

```text
Primary: Phen2Gene
Selected online degraded support: NCBI MedGen
method: known gene + accepted HPO canonical label via Clinical Features
```

MedGen is not a Phen2Gene ranker; never create a Phen2Gene score/rank from MedGen.

## Disease/HPO context

```text
Primary: MyDisease.info
Selected online fallback: NCBI MedGen
```

## Literature

```text
LitVar2 → Europe PMC → PubMed
```

## Interpretation model

```text
Selected primary model → eligible operational fallback model
```

---

# 29. Historical conceptual Evidence Pack shape (2026-08-16)

The exact production schema should evolve carefully, but the conceptual target is:

```json
{
  "variant_identity": {},
  "annotation": {},
  "population": {},
  "clinical_evidence": {},
  "classification_context": {},
  "gene_disease": {},
  "phenotype": {},
  "functional_predictions": {},
  "literature": [],
  "cspec_context": {},
  "coverage": {},
  "conflicts": [],
  "missing_evidence": [],
  "provenance": {},
  "capability_results": {}
}
```

This is a conceptual target, not permission to overwrite the current strict schema without migration and tests.

---

# 30. Historical pre-closeout decision record (2026-08-16)

The API-discovery decision is sufficiently closed for the current implementation cycle.

Selected new integrations:

```text
1. ClinGen Evidence Repository (ERepo)
2. NCBI MedGen
```

Selected MedGen roles:

```text
A. disease/HPO fallback
B. phenotype-gene-disease DEGRADED supporting evidence
C. gene-disease DEGRADED supporting context
```

Not selected now: Monarch, Open Targets, PanelApp, Gene2Phenotype, Orphadata, Direct ClinGen GDV feed. The reason is tested operational accessibility, not necessarily scientific invalidity.

> Do not search for more APIs now.

Proceed with ERepo integration → MedGen integration → VEP field-level composition → Evidence Coverage Calculator → capability/readiness logic → failure-driven interpretation audit.

Only reopen provider discovery if a concrete critical evidence node remains unfilled after these changes.

---

# 31. If a future AI receives only this file

The AI should understand that the project is already mature and heavily tested.

Do not restart architecture from scratch.

Do not simplify away provenance, readiness, conflict handling, source-correlation logic, persistence, or report lifecycle.

Do not reintroduce raw-VCF filtering, ranking/Top-N, old Stage 44 two-model routing, autonomous classification, or automatic CSpec rule execution.

Do not assume a green provider row means variant-specific evidence exists.

Do not assume a red provider row means an evidence capability is unavailable.

Do not propose local/offline databases as satisfying the target online redundancy workstream.

The API-discovery phase for the current cycle is **already completed**. The selected new online providers are:

```text
ClinGen ERepo
NCBI MedGen
```

The correct continuation path is:

```text
1. integrate ClinGen ERepo through existing resilience/provenance contracts
2. integrate NCBI MedGen with three separated evidence roles
3. fix VEP-outage field-level annotation composition
4. add per-variant Evidence Coverage Calculator
5. implement FULL / DEGRADED / UNAVAILABLE capability logic
6. run deterministic + bounded live resilience tests
7. run failure-driven interpretation trace from input to LLM output
8. tune LLM warnings/gating only if evidence flow is proven correct
9. update master documentation and close resilience workstream
10. complete report visual sign-off separately
```

Only reopen provider discovery if a concrete critical evidence field remains unfilled after the selected integration/composition work.

---

# 32. One-sentence project definition

> A resilient, evidence-centered clinical variant interpretation support system for 1–10 already-filtered germline Mendelian variants that validates exact allele identity, aggregates source-attributed multi-provider evidence into a structured Evidence Object, preserves missingness/conflicts/correlation, performs one evidence-bounded LLM interpretation per variant, and requires human review before final reporting.

---

# 33. Current status summary

```text
Project architecture: evidence-centered and provider-neutral at the semantic-node layer
Input model: 1–10 already-filtered variants
Evidence Object: schema 2.5 implemented
LLM interpretation: implemented with sanitized semantic projection
Human review/reporting: implemented and mandatory
Provider resilience framework: implemented
Exact ClinVar/identifier rescue: implemented
Stage-6 composition/promotion: implemented with field-level gating
Stage-7 coverage: runtime-only schema 1.0 implemented
Stage-8 disposition: runtime-only schema 1.0 implemented
Stage-9 deterministic/bounded live validation: completed
Stage-10 failure-driven audit and LLM-boundary correction: completed
Stage-11 documentation closeout: COMPLETE / REVIEW PENDING
Final report visual sign-off: separate Stage 115 work
```

### Implemented provider and Evidence Graph contract

| Semantic node | Implemented provider role |
|---|---|
| `annotation` | Ensembl VEP preferred; VariantValidator exact validated fallback/composition; GeneBe separate automated/supporting context; MyVariant approved identifier/aggregate roles. |
| `automated_acmg_context` | GeneBe only; context only, never final ACMG. |
| `expert_curated_variant_context` | ClinGen ERepo only. |
| `clinvar_clinical_evidence` | Direct NCBI ClinVar; MyVariant-derived ClinVar is a correlated rescue when applicable. |
| `cspec_context` | Registry/context only; no automatic rule execution. |
| `gene_disease_validity` | GenCC only. |
| `gene_disease_support` | MedGen only, as supporting context. |
| `phenotype_gene_ranking` | Phen2Gene only. |
| `phenotype_gene_support` | MedGen/local support only; never a rank or score. |
| `disease_hpo_context` | Source-separated MyDisease and MedGen context. |
| `population_evidence` | Direct gnomAD with approved MyVariant aggregate and configured UCSC mirror routes where applicable; shared upstream is retained. |
| `literature_evidence` | LitVar2, then Europe PMC, then PubMed. |

Provider names are not Evidence Graph node names. Correlation is provenance and
reviewer context only: direct ClinVar/MyVariant-derived ClinVar and direct/mirror
gnomAD are not independent biological confirmations, votes, independence scores, or
pathogenicity multipliers.

### Stage-6 annotation contract

VEP remains preferred and a usable VEP result is unchanged. Under an operational
failure or insufficiency, the gated path may promote only `gene`, `transcript`,
`HGVS.c`, and `HGVS.p`. Promotion requires exact allele/build identity, exact
versioned transcript coherence, and HGVS.c/HGVS.p from the same transcript context;
each field retains its actual source and proof. Consequence, impact, MANE, and
canonical status are intentionally not promoted. `shadow_composition` is
observational and excluded from LLM input. The legacy direct VariantValidator
canonical projection is superseded, and no composed field is falsely attributed to
VEP.

### Stage-7 coverage and Stage-8 disposition contracts

Coverage schema `1.0` is runtime-only, per-variant, provider-neutral, unpersisted,
and not a score. It references existing normalized evidence paths rather than copying
provider claims. Each semantic record has `critical_fields`, `important_fields`,
`optional_fields`, `retrieval_states`, composition/provenance context, limitations,
and missing targets. `FULL` requires all applicable critical fields; `DEGRADED`
means usable but incomplete critical/important coverage; `UNAVAILABLE` has no usable
applicable semantic evidence; `NOT_TRIGGERED` was not queried by applicable policy;
and `NOT_APPLICABLE` does not apply to this input. Retrieval is independent:
`no_match` is not `operational_failure`, not `no_verified_gene_association`, and not
either not-triggered or not-applicable.

Internal readiness remains `READY`, `READY_WITH_LIMITATIONS`, `RESCUE_REQUIRED`, or
`MINIMUM_IDENTITY_FAILURE`. Runtime final-disposition schema `1.0` maps the first
two to `READY`/`READY_WITH_LIMITATIONS`, maps minimum identity failure to `BLOCKED`,
and reassesses `RESCUE_REQUIRED` after enrichment so it is never final. Structural
identity unsafety blocks; safe sparse evidence and provider outage alone do not.
Reviewer interpretation keeps provider operational status, analysis/query retrieval,
and per-variant semantic capability separate. For example, unavailable GenCC plus
accepted MedGen support yields unavailable `gene_disease_validity` and full
`gene_disease_support`; support never becomes validity.

### LLM and persistence boundary

The LLM receives sanitized normalized semantic evidence only: no raw API responses,
raw VCF, genotypes, sample identifiers, PHI, shadow composition, wholesale coverage,
Stage-8 technical diagnostics, or rejected `candidate_diagnostics`. Stage 10 proved
that rejected `candidate_diagnostics` previously crossed this boundary. The scoped
fix makes `shadow_free_evidence_for_llm()` recursively remove them from the LLM
projection while retaining the upstream reviewer/provenance trace. EvidenceObject
`2.5`, SQLite `4`, and pipeline `3.2` are unchanged; coverage/disposition/trace are
runtime or audit projections and add no persistence migration.

### Current limitations

- The frozen full-suite baseline still has the manual Streamlit dataframe mismatch
  (expected 6, observed 8) and the missing historical
  `docs/acceptance_failures_v1.md` registry failure.
- Provider availability, rate limits, and schemas remain operational risks; Stage-9
  observations are point-in-time only.
- Consequence, impact, MANE, and canonical status are intentionally non-promoted.

Semantic state:

```text
GeneBe unavailable:
ERepo can preserve curated context if a record exists,
but automated ACMG context remains non-equivalent/degraded.

GenCC unavailable:
MedGen can preserve gene-disease support,
but true validity classification remains DEGRADED.

Phen2Gene unavailable:
MedGen can preserve phenotype-gene-disease support,
but no rank/score is recreated.

MyDisease unavailable:
MedGen is the selected online fallback.
```

---

# 34. Handoff instruction after closeout

Before proposing a separately approved change, the next AI should:

1. read this file;
2. read `PROJECT_DECLARATION(1).md`;
3. read the latest Evidence Graph Architecture Audit;
4. inspect current code before assuming documentation is exact;
5. preserve all safety, provenance, exact-identity, no-match, and source-correlation semantics;
6. treat provider selection as **closed for the current cycle** unless a concrete unresolved evidence gap is demonstrated.

There is no automatic implementation order after Stage 11:

```text
No Stage 12 is authorized.
Post-closeout stabilization and Stage 115 visual sign-off are separate review-gated
workstreams.
```

Do not restart broad API hunting; do not turn MedGen matches into GenCC validity classes; do not turn MedGen phenotype evidence into Phen2Gene ranks/scores; do not treat ERepo as a universal GeneBe clone; do not count correlated upstream sources as independent biological confirmation; and do not block interpretation merely because one provider is unavailable if minimum safe evidence remains.

Report visual-signoff work remains a separate track.

---

**End of AI Handoff Master — evidence-resilience closeout updated 2026-08-17.**
