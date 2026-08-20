# PROJECT_CONTEXT.md

# Clinical Variant Evidence Pipeline

## Purpose

This project is an online evidence-only variant interpretation system.

The system receives pre-filtered variants and collects external evidence sources to generate structured evidence reports.

It is not a diagnostic system.

It does not automatically classify variants.

---

# Main Architecture

## Frontend

Responsible for:

- User interaction
- Variant input
- Displaying evidence
- Showing reports


## Backend

Responsible for:

- Pipeline orchestration
- Provider communication
- Evidence aggregation
- Report generation


---

# Pipeline Flow

Input variants
        |
        ↓
Normalization
        |
        ↓
Annotation
        |
        ↓
Evidence Collection
        |
        ↓
EvidenceObject Creation
        |
        ↓
LLM Report Generation


---

# Evidence Rules

Only sanitized EvidenceObjects can reach the LLM.

Never send:

- raw API responses
- patient information
- unnecessary genomic metadata


---

# Providers

Current evidence sources include:

- ClinVar
- gnomAD
- Ensembl
- VariantValidator
- ClinGen
- GenCC
- MedGen
- Phen2Gene


---

# Fallback Philosophy

External providers can fail.

Failures should:

- be logged
- trigger fallback when available
- not destroy successful evidence

One failed variant should not automatically fail the entire analysis.

---

# Important Constraints

Never:

- perform ACMG automatic classification
- invent missing evidence
- overwrite primary evidence
- hide provider failures

---

# Current Development Priority

When modifying code:

1. Preserve pipeline architecture.
2. Improve reliability.
3. Improve observability.
4. Avoid unnecessary rewrites.