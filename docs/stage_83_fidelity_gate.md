# Stage 83 Golden DOCX Fidelity Gate

## Production path

`backend/report_docx.py` validates one `ReportData V4` record, opens the authoritative
`templates/clinical_variant_report_v1.docx`, populates its Word-native slots, and emits
one independent `variant_{input_index+1:03d}_report.docx` artifact. Rendering is
deterministic and makes no provider or model request.

The path preserves the template styles, theme, page geometry, result block, table
grids, paragraph hierarchy, page breaks, reference style, and blank running page
furniture. It dynamically expands the literature paragraphs and Data Sources rows,
keeps provider links allowlisted, and fails when the requested template version or
required slots drift.

## Golden synthetic case

`tests/fixtures/stage83_golden_report_data_v4.json` is a non-PHI, schema-valid allele
containing all major fields: two accepted HPO terms, partial phenotype concordance,
three population slots, a disease/inheritance record, three computational predictors,
a stable identifier, direct and automated classification context, long interpretation,
three literature references, eight source-provenance records, warnings, and confirmed
review state.

The production renderer generates
`tests/golden/stage83/variant_001_report.docx`. The committed file is the deterministic
golden artifact used by the automated fidelity gate. Its SHA-256 is
`D073921A1984B4553112FA50D9E7C12A8C3E6EF7FAAA8C4E1F874A030286F940`.

## Automated inspection

`tests/test_stage83_docx_fidelity.py` checks:

- heading hierarchy and required clinical-report section order;
- result emphasis and classification presentation;
- table structure, fixed DXA geometry, row expansion, and source separation;
- Times New Roman typography and preserved template styles/theme;
- paragraph flow anchors and two controlled page breaks;
- compact numbered reference formatting and validated hyperlinks;
- overall density from rich synthetic evidence and eight provenance rows;
- editable Word-native content without placeholders or image-only pages;
- rejection of template-version and required-slot drift;
- deterministic equality with the committed golden artifact.

These checks reject a generic Streamlit export, JSON dump, Markdown document,
unstyled `python-docx` output, form printout, or developer report at the structural
and package level.

On 2026-08-11, all seven Stage 83 tests passed and the complete offline suite passed
with `971 passed, 4 skipped`. The table-geometry audit reported exact matching DXA
widths, indents, grids, and cells. The privacy scrub found no metadata or revision IDs
to remove. The accessibility audit reported no high-severity findings and two medium
layout-table notices for the metadata strip and result block.

## Manual visual comparison checklist

The generated report must be inspected beside all four pages of
`docs/TS-Final Report.pdf` for:

```text
heading hierarchy
result emphasis
table structure
typography
paragraph flow
page breaks
reference formatting
overall density
clinical-document feel
```

LibreOffice is unavailable in the current environment. Microsoft Word automation
also timed out during the Stage 82 and Stage 83 render attempts. Therefore manual
visual acceptance remains pending,
and Stage 83 is not recorded as visually passed. Open the golden DOCX in Microsoft
Word or LibreOffice and compare it with the professor PDF before beginning Stage 84.

## Stage boundary

Stage 83 adds the production per-allele renderer and golden fidelity gate only. It
does not add the in-application preview, editing controls, lifecycle changes, or UI
work owned by Stage 84 and later stages.
