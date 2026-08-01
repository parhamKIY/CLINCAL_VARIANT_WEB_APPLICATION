# Stage 0 — ACMG/AMP Requirements and Output Contract

**Stage status:** In progress

**Stage purpose:** Freeze the clinical scope, terminology, and exact
professor-approved output before ACMG/AMP implementation begins.

**Approval authority:** Project owner and professor

## Stage steps

| Step | Decision | Status |
|---:|---|---|
| 1 | Confirm germline Mendelian scope | Complete — awaiting review |
| 2 | Confirm supported variants: SNVs and small indels | Not started |
| 3 | Confirm the one-to-five filtered-row input boundary | Not started |
| 4 | Define disease-selection behavior | Not started |
| 5 | Decide whether genotype, zygosity, trio, phase, and pedigree data are allowed | Not started |
| 6 | Select the ACMG/AMP combination framework | Not started |
| 7 | Define ACMG/AMP evidence points | Not started |
| 8 | Define clinical priority | Not started |
| 9 | Define evidence-confidence levels | Not started |
| 10 | Approve graph sources, algorithm boundaries, and explainable-path output | Not started |
| 11 | Define human-review requirements | Not started |
| 12 | Create concordant and conflicting reference reports | Not started |
| 13 | Update requirements and handoff documentation | Not started |

## Step 1 — Germline Mendelian scope

### Proposed initial scope for approval

The initial ACMG/AMP interpretation workflow is limited to **germline
variants evaluated for Mendelian disease**.

Each future ACMG/AMP result must represent a specific
variant–gene–disease–inheritance assertion. A result must not be presented as
a context-free classification of a gene or variant.

The workflow may evaluate inherited or de novo germline findings when the
required case evidence is available. The allowed case-data inputs and
missing-data behavior remain deferred to Stage 0 Step 5.

### Biological purpose

Germline variants can be inherited or arise de novo and may contribute to
single-gene disorders. Their interpretation depends on the disease mechanism
and inheritance model, so the same variant may require different conclusions
for different disease contexts.

Restricting the first release to Mendelian disease gives the deterministic
engine a coherent evidence model for population frequency, segregation,
de novo occurrence, functional evidence, phenotype specificity, and
gene–disease validity.

### Explicitly outside this initial clinical scope

- somatic cancer-variant interpretation;
- tumor diagnosis, prognosis, or therapy selection;
- pharmacogenomic interpretation;
- polygenic risk scoring;
- complex or multifactorial disease-risk prediction;
- direct diagnosis or treatment recommendations.

These categories require separate standards, evidence models, validation
sets, and reporting language. They must not reuse the germline ACMG/AMP
workflow without a separately approved roadmap.

### Decisions intentionally deferred

- supported molecular variant classes — Step 2;
- input-count and filtering boundary — Step 3;
- disease-selection behavior — Step 4;
- genotype, zygosity, trio, phase, segregation, and pedigree inputs — Step 5;
- classification-combination and scoring frameworks — Steps 6–9;
- graph, review, and reference-report policies — Steps 10–12.

### Acceptance checks

- The initial clinical domain is explicitly germline Mendelian disease.
- Somatic, pharmacogenomic, polygenic, and multifactorial interpretations are
  explicitly excluded.
- Every classification is bound to a disease and inheritance context.
- The application remains clinical decision support, not a diagnosis or
  treatment system.
- Later Stage 0 decisions are not preempted by this scope decision.

### Authoritative basis

- Richards et al., 2015 ACMG/AMP sequence-variant interpretation guidelines:
  <https://pubmed.ncbi.nlm.nih.gov/25741868/>
- ClinGen Variant Classification Guidance:
  <https://www.clinicalgenome.org/tools/clingen-variant-classification-guidance/>
- ClinGen Variant Curation Standard Operating Procedure:
  <https://clinicalgenome.org/docs/variant-curation-standard-operating-procedure/>
