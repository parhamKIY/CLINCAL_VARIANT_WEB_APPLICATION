# Stage 62 Demo and Professor Review Guide

**Status:** Repository preparation complete; professor feedback pending  
**Prepared:** 2026-08-09  
**Demo input:** `data/samples/stage62_demo_variants.xlsx`

## Purpose

This guide demonstrates the implemented V3 workflow without presenting it as a
diagnostic device. The input variants are public examples supplied for software
demonstration. The workbook contains no real patient data.

## Pre-demo checks

1. Confirm `.env` contains the intended `GRCh38` assembly, provider settings, and
   separate phenotype-extraction and variant-interpretation model choices.
2. Confirm the coordinated local HPO files exist under `data/hpo/`.
3. Run the offline release gate:

   ```powershell
   .\.venv\Scripts\python.exe tests\run_stage60_acceptance.py
   ```

4. Run the point-in-time live gate when external connectivity is part of the demo:

   ```powershell
   .\.venv\Scripts\python.exe tests\run_live_provider_validation.py
   ```

5. Start the application:

   ```powershell
   .\run_app.bat
   ```

## Demo workbook

Worksheet 1, `Variants`, contains five ordered public GRCh38 variants using the
required `CHROM`, `POS`, `REF`, and `ALT` columns plus optional `QUAL` and `FILTER`.

Worksheet 2, `Ignored_Demo_Data`, contains conspicuous demo-only markers, including
`THIS_SHEET_MUST_NOT_BE_PROCESSED`. Open the workbook before the application demo to
show both worksheets. The application must process only worksheet 1; later-sheet
markers must not appear in analysis state, model requests, reports, logs, database
records, or exports.

## V3 demonstration sequence

1. Open `data/samples/stage62_demo_variants.xlsx` and show that it contains two
   worksheets.
2. Start the application and identify the non-diagnostic safety notice.
3. In **Task-specific models**, choose a **Phenotype Extraction Model** and a
   **Variant Interpretation Model** independently.
4. Choose file upload, select the demo workbook, and point out the UI statement that
   only worksheet 1 is read and that the input limit is ten variants.
5. Enter this de-identified Persian example in **Persian clinical description**:

   `کودک دچار حملات تشنج و تاخیر تکاملی است.`

6. Select **Extract HPO candidates**. Review the locally validated suggestions and
   show any rejected invalid suggestion as missingness, not clinical evidence.
7. Edit or remove an unwanted suggestion, add/correct a term through local HPO
   search if needed, then select **Accept HPO candidates**. Retain at least two
   accepted terms when the model suggestions support them.
8. Confirm the two selected model roles, then select **Analyze variants**.
9. Show that all five variants remain in workbook order and inspect provider status,
   conflict context, interpretation provenance, and each Draft Variant Report.
10. Edit at least one reviewer-controlled report field and save it. Compare the
    reviewed report with its immutable machine original and show the audit entry.
11. Clear **Include this variant in Final Report** for at least one variant. Explain
    that exclusion is a human reporting choice, not ranking, and that excluded
    variants remain persisted and recoverable.
12. Complete the privacy attestation and **Confirm evidence** for every variant, then
    select **Finalize review**. Show that finalization makes no new LLM request.
13. Verify that the Final Clinical Report contains only included reports, download
    text, PDF, and Word versions, and open at least one validated literature or
    canonical provider link. If the live probe returned no match, show the explicit
    unavailable-link state instead of inventing a URL.

## Expected checkpoints

| Checkpoint | Expected result |
|---|---|
| Excel boundary | Five worksheet-1 variants only; no later-sheet marker downstream |
| Phenotype workflow | Persian suggestions are local-HPO validated, editable, and explicitly accepted |
| Model roles | Phenotype extraction and variant interpretation have independent selectors |
| Interpretation | One selected interpretation model is used across conflict states |
| Draft reports | Every variant retains evidence, interpretation or explicit failure, and references |
| Human review | Whitelisted edits and inclusion decisions have append-only audit history |
| Final report | Only included, confirmed reviewed reports appear in original input order |
| Export | Text, PDF, and Word match the confirmed reviewed state |
| References | Validated exact links are clickable; non-navigable/unavailable links use fallback text |

## Provider-failure handling during a demo

External services are point-in-time dependencies. Do not hide, rewrite, or convert an
unavailable/no-match response into negative clinical evidence. Explain the displayed
state, use the Stage 60 deterministic gate to demonstrate the application contract,
and rerun the bounded Stage 61 live check only when appropriate.

## Professor review checkpoint

Record feedback against these questions:

- [ ] Is the Final Clinical Report useful for its stated decision-support scope?
- [ ] Is the report visually organized and easy to scan by variant?
- [ ] Is interpretation wording appropriately cautious and evidence-bound?
- [ ] Are Persian phenotype suggestions useful?
- [ ] Is HPO correction and explicit acceptance understandable?
- [ ] Is variant inclusion/exclusion clearly a reporting decision rather than ranking?
- [ ] Are reference labels, exact links, and unavailable-link fallbacks sufficient?
- [ ] Are additional bounded clinical fields required?

### Review record

| Field | Entry |
|---|---|
| Review date | Pending |
| Reviewer | Pending |
| Outcome | Pending |
| Required changes | Pending |
| Deferred ideas | Pending |
| Approval/sign-off | Pending |

Professor feedback is an external checkpoint. This repository records the prepared
demo and review questions but does not claim approval before the review occurs.
