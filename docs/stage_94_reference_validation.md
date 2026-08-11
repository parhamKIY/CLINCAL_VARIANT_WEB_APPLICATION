# Stage 94 - Reference Validation Tests

## Outcome

The Stage 93 human-link policy now has a dedicated deterministic regression matrix.
The tests prove that supported stable identifiers resolve to the intended human
records and that missing, unsupported, or machine-only targets remain unlinked
provenance.

## Deterministic offline coverage

`tests/test_stage94_reference_validation.py` verifies:

- PMID to its canonical PubMed article;
- PMCID to its canonical PubMed Central article;
- DOI to the canonical DOI resolver;
- VCV, RCV, and SCV accessions to human ClinVar targets;
- supported GRCh37 and GRCh38 GeneBe alleles to human variant pages;
- MyVariant.info raw JSON is never exposed as a primary reviewer link;
- missing Ensembl identifiers do not produce fabricated URLs;
- unsupported providers remain visible as provenance only;
- the complete Reference Model contains no machine-readable human links.

The offline gate performs no network calls and is registered under
`stage94_reference_validation`.

## Optional live coverage

`tests/test_stage94_reference_validation_live.py` is separately marked
`live_provider` and skipped unless `RUN_LIVE_PROVIDER_TESTS=1`. It probes a bounded
representative set of generated PubMed, PMC, DOI, ClinVar, and GeneBe targets, checks
for a successful human-page response, and rejects JSON content types. It is not part
of deterministic offline CI.

## Validation

Validated on 2026-08-11:

- Stage 94 gate: `12 passed, 1 skipped`.
- complete repository suite: `1057 passed, 5 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 226 deselected`, `85.15%` coverage.

Stage 95 subsequently added the input-aware user-facing analysis summary without
changing the Stage 94 reference-validation boundary.
