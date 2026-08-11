# Stage 90 - Phenotype Non-Concordance Contract

## Implemented outcome contract

Variant Interpretation prompt `variant-interpretation-v1.2` requires exactly one
phenotype conclusion:

```text
supported
partially supported
no supported association found
phenotype evidence unavailable
```

The conclusion is checked against the deterministic phenotype status already present
in the sanitized Evidence Object. Exact local support may be reported conservatively
as supported or partially supported. Partial support, no exact match, and unavailable
phenotype evidence require their corresponding conclusion.

Prompt version `v1.1` remains valid for persisted historical interpretation results;
new calls use `v1.2`. The Variant Interpretation Result field shape remains schema
`1.1`, so no pipeline or database migration is required. The canonical first-class
phenotype state continues to persist in `ReportData V4.phenotype_summary.concordance`.

## Non-concordance behavior

For valid unrelated phenotype evidence, including the acceptance fixture
`HP:0002027` abdominal pain:

- interpretation continues using all non-phenotype evidence;
- the result remains successful and the allele remains in the workflow;
- the narrative begins with `Phenotype conclusion: no supported association found.`;
- no phenotype-to-disease association is invented;
- phenotype mismatch does not modify source-attributed pathogenicity evidence or
  become benign/negative pathogenicity evidence.

Absent phenotype evidence is also valid and produces the explicit
`phenotype evidence unavailable` conclusion. A model response that claims phenotype
support against deterministic no-match evidence is rejected as an output-schema
failure and enters the bounded Stage 89 repair/recovery policy.

## Prompt safety

The prompt explicitly instructs the model to state unsupported association, continue
from remaining evidence, avoid forced disease matching, and keep phenotype mismatch
separate from pathogenicity. The response remains strict JSON and does not add a new
semantic model-routing branch.

## Acceptance evidence

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m stage90_phenotype_non_concordance
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\run_stage60_acceptance.py
```

Validated on 2026-08-11:

- Stage 90 non-concordance gate: `7 passed`;
- unrelated abdominal-pain fixture succeeds with an explicit no-association statement;
- ReportData retains `no_supported_association` without reclassification;
- hallucinated support is rejected;
- supported, partially supported, and unavailable outcomes remain valid;
- full repository suite: `1021 passed, 4 skipped`;
- Stage 60 release gate: passed at `85.70%` coverage.

Stage 91 subsequently added the interpretation-quality benchmark and evidence gate.
