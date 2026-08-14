# Stage 80 Professor Report Template Specification

`docs/TS-Final Report.pdf` is the **authoritative visual and structural reference**;
all four rendered pages were inspected. The recorded SHA-256 is
`41DFE4F49925CE86E42E881897EC8E7A8662DA71EE693101DAB681333390CACC`.

## Artifact boundary

One accepted allele produces one independent report. Two variants in the same gene remain separate reports and retain their original input indexes and allele identities.

## Required anatomy

1. NGS Result Report
2. Clinical Features
3. Variant and source classification context
4. Brief Interpretation(s)
5. Main Finding(s) in Detail
6. Variant interpretation
7. Variant(s) classification
8. Method
9. Comments / scope
10. References
11. Data Sources

## Content rules

- Patient identifiers are excluded from generated artifacts and prompts.
- Phenotype non-concordance is valid and is written as `No supported association`.
- A completed exact lookup without a matching record is written as `No exact record`.
- Literature citations and database/tool provenance remain separate.
- Human-facing links must not use a raw API JSON endpoint.
- The application must not add unsupported prenatal/PGD, diagnostic, treatment, or
  testing directives.
- Missingness, provider failure, unexecuted lookup, and interpretation failure are
  distinct states.

## Presentation rules

The report uses US Letter geometry, Times New Roman, a centered title, a bordered
result block, fixed-width evidence tables, readable section hierarchy, explicit page
breaks, compact references, and editable Word-native content. Exact observed and
target-approximation tokens are recorded in `docs/report_style_spec.yaml`.

## Stage boundary

No Stage 81 schema is defined here. Stage 80 specifies anatomy, content, and visual
behavior only; the renderer-neutral schema, template, renderer, and UI integration
belong to later stages.
