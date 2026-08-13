# Stage 82 Authoritative DOCX Template Contract

The editable Word-native template at `templates/clinical_variant_report_v1.docx`
represents one accepted allele. It contains no image-only report pages and keeps
variable clinical content in validated placeholders.

Each artifact uses `variant_{input_index+1:03d}_report.docx`; the first accepted
allele therefore produces `variant_001_report.docx`. Same-gene alleles never share an
artifact.

The template owns Stage 80 page geometry, typography, result emphasis, fixed table
structure, pagination behavior, literature references, and Data Sources. Valid
missingness is rendered explicitly, including `No exact record` and
`No supported association`.

Patient identifiers, unsupported clinical directives, and raw provider payloads are
not template content. Stage 83 populates this template and verifies deterministic
structure and fidelity.
