# Stage 82 Authoritative DOCX Template Contract

## Delivered artifact

`templates/clinical_variant_report_v1.docx` is the editable Word-native template for
one accepted allele. Its visual system follows the Stage 80 specification derived
from the authoritative four-page professor report.

The reproducible source is `tools/build_stage82_template.py`. Rebuilding the template
must not require a network call or patient data.

## Artifact identity

Every instantiated report is an independent file. The renderer must use:

`variant_{input_index+1:03d}_report.docx`

Examples are `variant_001_report.docx`, `variant_002_report.docx`, and
`variant_003_report.docx`. A shared mutable draft is not the only report artifact.

## Template-owned presentation

The template owns US Letter portrait page geometry, one-inch margins, Times New Roman
styles, title and heading hierarchy, paragraph spacing, result-block borders and
colors, fixed-width evidence tables, cell padding, repeating table headers, row-split
controls, page breaks, reference typography, and blank running headers and footers.

The principal visual tokens are the Stage 80 values: red `#C00000` result border,
blue `#2F5496` allele emphasis, gold `#FFC000` classification emphasis, gray
`#D9D9D9` evidence values, and gray `#A5A5A5` table borders.

## Placeholder contract

The visible structure includes report identity, clinical features, method/scope,
conclusive result, brief interpretation, detailed findings, long interpretation,
classification summary, references, and source provenance. The two structured table
anchors are stored as Word table captions: `{{MAIN_FINDINGS_TABLE}}` and
`{{DATA_SOURCES}}`. Other content remains directly editable as `{{...}}` text.

`{{REFERENCES}}` is literature-only. Provider provenance belongs in
`{{DATA_SOURCES}}`. A renderer must use explicit truth-preserving labels such as
`Unavailable`, `No exact record`, `No supported association`, or `Not reported`
instead of inventing evidence or leaving ambiguous blanks.

## Variable-length behavior

The result block has content padding and no fixed row height, so long HGVS text can
wrap without clipping. Detailed interpretations may continue to another page without
repeating their heading. Evidence tables use fixed DXA widths, may repeat header rows,
and keep value rows together where practical. References use compact hanging indents.

Missing optional values do not remove the surrounding semantic section. The Stage 83
renderer will map validated ReportData V4 missingness into the explicit labels above.

## Safety boundary

The template contains no real patient identifier, sample or laboratory identifier,
signature block, diagnosis, treatment recommendation, testing directive, carrier
aggregation, or independent ACMG adjudication. It is a decision-support report layout,
not an autonomous clinical conclusion.

## Stage boundary

Stage 82 creates and validates the authoritative DOCX template only. Stage 83 owns the
deterministic ReportData V4 renderer and golden DOCX fidelity gate. Preview, editing,
finalization, lifecycle, and user-interface integration remain later stages.

## Verification status

On 2026-08-11, the Stage 82 tests passed, the complete offline suite passed with
`964 passed, 4 skipped`, the privacy scrub completed, and the table-geometry audit
reported matching DXA table widths, indents, grids, and cell widths. The accessibility
audit reported no high-severity findings and two medium findings because the metadata
strip and bordered result block are layout tables without semantic header rows.

LibreOffice is not installed in the current environment, and a bounded Microsoft Word
automation export timed out. No visual-render pass is claimed. The final Stage 82
acceptance action is a manual open check in Word or LibreOffice before Stage 83 begins.
