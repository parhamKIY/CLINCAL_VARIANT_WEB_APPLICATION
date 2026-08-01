# Stage 16 MVP release checklist

## Release decision

**READY FOR PROFESSOR DEMONSTRATION**

This decision applies to the local educational MVP using public or synthetic
test data. It is not approval for institutional deployment or real patient
data.

## Acceptance evidence

- All roadmap MVP capabilities are implemented.
- The deterministic MVP workflow passes.
- The complete regression suite passes.
- Unit coverage exceeds the required 80% threshold.
- The Stage 15 security controls pass.
- The repository secrets audit passes.
- Installed Python dependencies are internally consistent.
- The public known-variant live workflow completed all required stages, saved
  a report, and returned zero errors.
- The optional UCSC GenCC source returned no exact SAMD11 claim; the application
  preserved this as an explicit missing-evidence warning.
- A safe one-variant demonstration VCF and presentation runbook are available.

## Final acceptance command

```powershell
.\.venv\Scripts\python.exe tests\run_stage16_final_acceptance.py
```

The final line must be:

```text
Stage 16 MVP release acceptance: PASSED
```

This command is offline. It does not test current international connectivity
or provider availability.

## Demonstration assets

- `data/samples/mvp_demo.vcf`
- `docs/STAGE_16_MVP_DEMO_RUNBOOK.md`
- Manual fallback variant: `1:941284:G:A`
- Demonstration phenotype: `HP:0001250 - Seizure`

## Pre-presentation checklist

- [ ] Pull or check out the intended Git commit.
- [ ] Confirm `.env` is present and not tracked.
- [ ] Run the final acceptance command.
- [ ] Confirm international provider connectivity with the public known
      variant.
- [ ] Start Streamlit and open `http://127.0.0.1:8501`.
- [ ] Use only the supplied public or synthetic demonstration data.
- [ ] Confirm the generated report can be downloaded.
- [ ] Keep the clinical decision-support disclaimer visible.

## Accepted MVP limitations

- External services may be unavailable or return incomplete evidence.
- The application may return `partial` with explicit warnings when optional
  evidence is unavailable.
- Input filtering, optimization, normalization, and ranking must be completed
  upstream; the application accepts only 1–5 already-filtered VCF rows.
- The manual editor accepts standard primary chromosomes `1`–`22`, `X`, `Y`,
  and `MT`. It rejects POS values outside the configured GRCh37/GRCh38
  chromosome length; alternate and unplaced contigs are not manual-editor
  options.
- The system is single-user and local.
- Authentication, user roles, PostgreSQL, background jobs, Docker, monitoring,
  and institutional deployment remain post-MVP work.

## Production restriction

Do not use real patient data until the production blockers in
`docs/STAGE_15_SECURITY_ACCEPTANCE.md` are resolved and the institution has
approved privacy, access-control, retention, deployment, and clinical-safety
policies.
