# Stage 42 — Privacy, security, and audit upgrade

Status: complete and offline verified

Date: 2026-08-08

## Data-minimization boundary

The active Pipeline V2 workflow sends each external service only the identifiers
required for its operation:

- VEP and GeneBe receive normalized chromosome, position, reference, and
  alternate allele values;
- MyVariant, ClinVar, ClinGen, CSpec, gnomAD, and Ensembl Variation receive
  bounded public allele, gene, disease, or public-identifier context;
- Phen2Gene receives canonical HPO identifiers and its weight model;
- MyDisease receives a bounded gene query and selected public fields; patient
  HPO matching remains local;
- literature providers receive public variant, gene, or publication queries.

Sample columns, genotypes, original filenames, and raw VCF content are excluded
before these calls. Stored filenames remain analysis-scoped aliases.

## LLM boundary

Pipeline V2 does not call an LLM during evidence collection. After explicit
confirmation, Stage 35 validates, size-bounds, and serializes exactly one
Reviewed Evidence Package. Raw VCF content, sample fields, and provider raw
payloads are rejected before routing. Prompts and responses are never written
to application logs.

The older standalone report helper remains for backward-compatible offline
tests, but it is not connected to the Streamlit or Pipeline V2 execution path.
All active clinical interpretation uses the confirmation-gated Stage 35 route.

## Human-review privacy

User-controlled reviewed evidence, reviewer notes, and edit-history values are
validated before a Draft can be returned or persisted. The validator rejects:

- patient, sample, medical-record, contact, birth-date, and genotype fields;
- labelled identifiers and email addresses inside free text;
- raw VCF headers or content;
- prohibited JSON-pointer paths inside a supplied edit history.

Rejected content does not mutate the preceding Draft. Pipeline validation also
applies the structural privacy boundary to both editable review reports and
confirmed packages, providing defense in depth during database save and load.

## Logging safety

Application logging continues to record only bounded operational telemetry,
generated run identifiers, statuses, counts, timings, and exception types. The
central logging filter now additionally redacts labelled clinical identifiers,
email addresses, patient/sample file paths, raw VCF header lines, and sensitive
fields inside nested logging arguments. Secret redaction and private file modes
remain unchanged.

## Audit provenance

Machine and human provenance remain structurally distinct and integrity-bound:

- `original_machine_report` preserves the validated source evidence and
  provider lineage;
- `reviewed_user_report` contains the human-reviewed state;
- `edit_history` records bounded append-only paths, old/new values, timestamps,
  and whether evidence was user-added;
- `user_added_evidence` is derived from and must match that history;
- `package_id` covers the complete confirmed package, so tampering is rejected.

Authentication is not implemented, so the application does not invent or store
a reviewer identity. When authentication is introduced, its immutable reviewer
identifier must be added through a versioned schema migration and included in
the package integrity hash and audit trail.

## Verification

- Sensitive review keys, notes, emails, and audit paths are rejected before
  storage without mutating the original Draft.
- Pipeline validation covers review reports and confirmed packages.
- Mocked annotation calls contain no genotype or sample identity.
- The LLM adapter receives the exact bounded confirmed package and no raw VCF.
- Direct log attempts containing PHI, nested PHI, clinical paths, email, and raw
  VCF headers are redacted.
- Source provenance and human edits remain separate and tamper-evident.
- The complete offline test suite passes with live services blocked.

## Completion condition

The active API-first workflow minimizes outbound data, prevents PHI and raw VCF
from reaching LLM or logs, rejects unnecessary sensitive review data before
storage, and preserves auditable source and human-edit provenance.
