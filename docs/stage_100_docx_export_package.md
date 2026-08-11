# Stage 100 - DOCX Export and Final Package

## Outcome

Word is the authoritative editable deliverable for each reviewer-approved variant.
The review surface provides `Download editable DOCX` for every report, regenerated
deterministically from that report's current validated `ReportData V4`.

## Selected final package

After every variant is confirmed and the analysis is finalized, the application
provides `Download selected Word report package`. The ZIP contains:

- only reports selected for Final Report inclusion;
- one independent professor-template DOCX per selected variant;
- filenames in stable original input order;
- a deterministic `manifest.json` with analysis, report, allele, version, digest,
  template, confirmation, and finalization identity.

The package does not merge same-gene alleles or reorder variants. Excluded reports
remain in persisted pipeline state but are absent from the ZIP. Zero selection is
represented by the existing final-summary output and does not create an empty Word
package.

The existing combined text, PDF, and Word summary exports remain auxiliary. The ZIP
of individual professor-template DOCX files is the authoritative editable final
package because it preserves the one-report-per-allele contract without lossy document
merging.

## Export integrity

Package creation accepts only completed pipeline state with one valid finalized
lifecycle record per input variant. Selected indexes must match the integrity-checked
Final Clinical Report. Every regenerated DOCX must match its persisted byte count and
SHA-256 artifact identity before it enters the package.

Export reads reviewer-approved persisted `ReportData`; it does not invoke annotation,
interpretation, or any LLM. ZIP entry timestamps, ordering, permissions, compression,
and JSON serialization are deterministic.

## Validation

Validated on 2026-08-11:

- Stage 100 gate: `5 passed`;
- complete repository suite: `1111 passed, 5 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 280 deselected`, `85.20%`
  coverage;
- two selected DOCX files extracted from the production ZIP and passed structural
  accessibility audit with `0` high-severity findings; each retained the two expected
  medium findings for non-semantic layout tables;
- visual rendering was unavailable because LibreOffice is not installed, so no new
  visual-render claim is made.

Stage 101 subsequently added deterministic scenario snapshots and an optional real
DOCX raster matrix without changing the Stage 100 export contract.
