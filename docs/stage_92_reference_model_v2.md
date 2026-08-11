# Stage 92 - Reference Model V2

## Outcome

Reference Model V2 separates scientific literature from database and tool
provenance. The two collections are validated independently and are never rendered
as one numbered bibliography.

## Literature References

Only article identifiers are accepted:

- PubMed PMID;
- PubMed Central PMCID;
- DOI;

Literature references use contiguous report-local `R1`, `R2`, and subsequent IDs.
The Variant Interpretation Model receives only this literature catalog, without
URLs, and may cite only those IDs. Prompt version
`variant-interpretation-v1.3` records the changed catalog boundary; persisted `v1.1`
and `v1.2` results remain supported.

## Data Sources

Database and tool provenance is unnumbered. Each record retains source, capability,
availability, operational status, provider role, method, stable record identifier or
dataset when present, fallback state, primary failure, retrieval time, and link
status. The builder covers:

- ClinVar;
- Ensembl and VariantValidator;
- GeneBe;
- MyVariant.info;
- ClinGen / GenCC;
- CSpec;
- Phen2Gene or the local HPO-Gene fallback;
- MyDisease or local HPO disease context;
- the active population source.

Literature-search providers remain retrieval provenance and do not become numbered
scientific citations by themselves.

## Integration

- `backend/reference_model.py` owns schema `2.0`, deterministic construction, and
  strict validation.
- Draft Variant Report schema `2.2` stores `literature_references` and
  `data_sources` separately.
- `ReportData V4`, HTML preview, DOCX projection, Streamlit review, persistence, and
  Final Clinical Report output preserve the separation.
- Final Markdown labels the numbered section `Literature References` and renders an
  independent unnumbered `Data Sources` section.

Stage 93 canonical human-link resolver behavior is not implemented here.

## Validation

Validated on 2026-08-11:

- Stage 92 gate: `7 passed`;
- complete repository suite: `1037 passed, 4 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 205 deselected`, `85.13%` coverage.
