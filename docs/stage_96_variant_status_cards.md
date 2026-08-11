# Stage 96 - Variant-First Status Cards

## Outcome

The completed-analysis review now begins with one bordered status card for every
input variant. Each card states the allele, report state, annotation, population,
ClinVar, phenotype, and interpretation outcome before the full report preview.

## Stable status contract

`frontend/variant_status.py` exposes only these primary statuses:

- `Report ready`;
- `Report ready with partial evidence`;
- `Interpretation requires attention`;
- `Input requires attention`.

Internal exception and failure-category names are never primary card labels.
Legitimate source missingness, including a ClinVar no-match or unsupported phenotype
association, produces a partial-evidence report rather than an operational-failure
label. Optional capabilities that were not assessed do not create false partial
status.

## Presentation and warning attribution

`frontend/evidence_review.py` renders the cards with native bordered Streamlit
containers. All user-facing warnings are rendered inside their owning card and begin
with the variant number. Provider, capability, status, and method details remain
available in a `Technical provider details` expander that is collapsed by default.
No new custom CSS was introduced.

## Validation

Validated on 2026-08-11:

- Stage 96 gate: `8 passed`;
- complete repository suite: `1075 passed, 5 skipped`;
- Stage 60 acceptance: `832 passed, 4 skipped, 244 deselected`, `85.26%`
  coverage.

Stage 97 warning semantics V2 is not implemented here.
