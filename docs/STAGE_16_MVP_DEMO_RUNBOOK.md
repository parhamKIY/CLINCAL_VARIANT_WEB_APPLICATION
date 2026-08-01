# Stage 16 MVP demonstration runbook

## Demonstration goal

Show one complete clinical variant interpretation workflow from a safe public
test input to a downloadable evidence-based report. Do not use real patient
data during the demonstration.

## Before the presentation

From the project directory, verify the environment:

```powershell
cd "C:\university\internship project\clinical_variant_app"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe tests\run_stage16_mvp_acceptance.py
```

Confirm that `.env` contains the intended provider configuration and that the
computer can access international services. Never display `.env` or an API key
during screen sharing.

## Start the application

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Open:

```text
http://127.0.0.1:8501
```

## Primary VCF demonstration

1. Keep the input source set to **VCF upload**.
2. Upload `data/samples/mvp_demo.vcf`.
3. Search for `seizure` in the phenotype search.
4. Select `HP:0001250 - Seizure`.
5. Select **Add phenotype**.
6. Select **Analyze variants**.
7. Show the pipeline progress, filtered input variant, annotation sources,
   phenotype evidence, Evidence Object, and clinical report.
8. Select **Download text** and open the downloaded `.txt` file.

The test input represents the public GRCh38 variant:

```text
1:941284:G:A
```

It contains no patient name, sample column, or genotype.

## Manual-input fallback

If browser upload interaction is unavailable:

1. Change the input source to **Manual table**.
2. Select `1` from the CHROM dropdown and confirm that the configured
   assembly's acceptable POS range appears below the table.
3. Enter `941284`, `G`, and `A` in the POS, REF, and ALT columns.
4. Add `HP:0001250 - Seizure`.
5. Run the analysis and download the report.

## Expected result

- Input validation, filtered VCF processing, annotation, phenotype, evidence, LLM,
  and report stages should finish successfully.
- The annotation stage may finish with a warning when an optional provider has
  no exact record for the variant or gene.
- A `partial` result with zero errors is acceptable when the warning represents
  unavailable optional evidence.
- A timeout, connection error, authentication error, or missing report is not
  an acceptable final demo result.

## Known live baseline

On 2026-07-31, the known-variant live validation completed in 13.706 seconds:

- all required pipeline stages completed;
- the LLM and report stages succeeded;
- the report was saved;
- zero errors were returned;
- UCSC GenCC returned no exact validity claim for SAMD11, producing one
  explicit annotation warning and an acceptable `partial` status.

No analysis identifier, API key, or private payload is recorded in this
runbook.

## Presentation talking points

- The system integrates variant processing, four annotation sources, HPO
  phenotype evidence, structured Evidence Objects, an isolated LLM provider,
  report generation, persistence, and a Streamlit interface.
- The LLM receives validated evidence rather than raw VCF or patient data.
- Missing external evidence remains visible instead of being invented.
- The report is decision support and requires qualified clinical review.
- Filtering and ranking happen upstream; every supplied table row is annotated
  in its original order.
- This is a local MVP, not an authenticated institutional production system.

## Network troubleshooting

If a provider times out:

1. Stop the demonstration run.
2. Confirm which provider failed in the terminal output.
3. Switch to a working international connection.
4. Rerun the same public test case.

Do not change code merely to hide a provider timeout or missing-evidence
warning.
