# Selected-Variant Input Stabilization — Stage 2B

## Scope

Stage 2B closes the ANNOVAR-like zero-allele identity gap at the selected-input
boundary. It does not add selection, ranking, quality gating, provider-specific
input handling, or Evidence Graph semantics.

## Source and canonical forms

The first worksheet remains the only worksheet read. Each retained row is classified
without mutation as `STANDARD_ALLELE`, `ANNOVAR_DELETION`,
`ANNOVAR_INSERTION`, or `UNSUPPORTED_OR_AMBIGUOUS`.

`ALT=0` and `REF=0` remain source provenance tokens. They are never canonical alleles
and never reach VEP, VariantValidator, ClinVar, MyVariant, ERepo, GeneBe, evidence,
or LLM payloads. A successful adaptation emits non-empty GRCh38 `CHROM/POS/REF/ALT`
and then uses the existing deterministic identity normalizer.

## Reference-aware adaptation

`backend.reference_sequence.fetch_grch38_reference_sequence()` is narrow identity
infrastructure, not an evidence provider. It requests an exact GRCh38 Ensembl
sequence interval with normalized chromosome naming, a five-second per-attempt
timeout, at most two attempts, explicit source provenance, and fail-closed output.

For a deletion-style row, the adapter requires inclusive `Start`/`End` length to
match the supplied deleted sequence, verifies that exact interval against GRCh38,
then fetches the preceding GRCh38 base. It emits the anchored VCF candidate
`POS=Start-1`, `REF=anchor+deleted`, `ALT=anchor` only after both checks pass.

For an insertion-style row, the adapter requires `Start == End`, fetches the GRCh38
base at that locus, and emits `POS=Start`, `REF=anchor`, `ALT=anchor+inserted`.
Missing or contradictory interval information is not guessed.

## Selected-input outcomes

The Stage 2A `InputPreprocessingResult` remains the only selected-input outcome
record.

- `ACCEPTED_DIRECT`: a valid ordinary non-zero allele was structurally compatible.
- `NORMALIZED_AND_ACCEPTED`: a zero-allele source representation was reference
  verified, anchored, and accepted by the existing canonical normalizer.
- `IDENTITY_UNRESOLVED`: no canonical identity was emitted. Precise reasons include
  `REFERENCE_LOOKUP_UNAVAILABLE`, `REFERENCE_MISMATCH`, `INVALID_INTERVAL`,
  `UNSUPPORTED_REPRESENTATION`, `NORMALIZATION_FAILED`, and
  `IDENTITY_NOT_PROVEN`.

Every selected row remains in `input_preprocessing_results`. An unresolved row has
no canonical link and therefore no annotation, Evidence Object, or LLM input.
`FILTER`, `QUAL`, `DP`, `AD`, and `GQ` are provenance only and do not affect this
decision.

## Recovery and compatibility

Recovery request version remains `3`. For an XLSX containing a zero-allele source
form, the private bounded checkpoint additionally retains first-sheet source records
so a restart can repeat identity verification without manufacturing a symbolic
allele. Existing version-3 checkpoints without that optional field remain readable.
Pipeline schema remains `3.3`, SQLite remains `4`, and EvidenceObject remains `2.5`.

The real professor workbook was read locally without modification. Its three ordinary
sheet-0 SNVs are direct candidates. Its ERCC5 `GTGC>0` and `C>0` rows are routed to
the reference-aware adapter; a bounded live GRCh38 lookup during this Stage was
operationally unavailable, so no live canonical identity was asserted or committed.
Deterministic fixtures prove the corresponding verified single-base, multi-base, and
insertion transformations.
