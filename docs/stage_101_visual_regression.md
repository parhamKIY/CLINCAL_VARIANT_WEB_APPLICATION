# Stage 101 - Visual Regression Harness

## Outcome

The professor-family DOCX renderer now has a deterministic regression matrix covering:

- fully populated evidence;
- sparse evidence;
- long interpretation;
- many literature references;
- no supported phenotype association;
- a complex long-reference deletion-insertion.

`tests/fixtures/stage101_visual_scenarios.json` registers the six synthetic cases.
`tools/stage101_visual_regression.py` derives each validated `ReportData V4` record
from the non-PHI Stage 83 fixture, renders it through the production DOCX path, and
checks the committed snapshots in `tests/golden/stage101/snapshots.json`.

## Always-on regression checks

Every deterministic test run verifies:

- exact DOCX SHA-256 and preserved style-part SHA-256;
- US Letter geometry and one-inch margins;
- clinical heading order and required labels;
- five required Word tables and their row/column structure;
- two controlled page breaks with no empty controlled page segment;
- no exact-height rows that could silently clip wrapped content;
- no unresolved template placeholders;
- expected reference and provenance hyperlink density;
- a compact structural layout signature.

The exact binary snapshots make all format changes explicit in code review. A future
template or renderer change cannot silently alter any of the six report artifacts.

## Raster checks

The harness supports real DOCX-to-PNG rendering when LibreOffice and Poppler are
installed. The raster gate checks the scenario-specific page-count range, rejects
blank pages, rejects content touching page edges, and produces average-image hashes
that can be compared with bounded Hamming-distance tolerance.

Run the always-on structural matrix:

```powershell
.\.venv\Scripts\python.exe tools\stage101_visual_regression.py `
  --output-dir tmp\stage101
```

Run structural plus real raster checks on a workstation with LibreOffice and Poppler:

```powershell
.\.venv\Scripts\python.exe tools\stage101_visual_regression.py `
  --output-dir tmp\stage101 --local-office
```

## Validation

Validated on 2026-08-11:

- Stage 101 gate: `11 passed, 1 skipped`;
- all six production DOCX files matched their committed binary and structural
  snapshots;
- synthetic PNG tests verified page-count, blank-page, edge-overflow, and tolerant
  image-hash failure behavior;
- all six generated DOCX files passed accessibility audit with `0` high-severity
  findings and the two expected medium layout-table notices each;
- the optional real DOCX raster matrix was skipped because LibreOffice is not
  installed; no visual-render pass is claimed;
- complete repository suite: `1122 passed, 6 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 292 deselected`, `85.20%`
  coverage.

Stage 102 interpretation acceptance is implemented separately in
`docs/stage_102_interpretation_acceptance.md`.
