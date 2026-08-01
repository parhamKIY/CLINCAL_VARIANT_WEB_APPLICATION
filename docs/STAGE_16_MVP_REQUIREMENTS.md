# Stage 16 MVP requirements audit

## Purpose

This audit maps the Stage 16 roadmap requirements to the current application
before the dedicated MVP acceptance workflow is added. “Verified offline”
means that deterministic automated tests exercise the behavior without calling
international services. “Live validation pending” means that the feature is
implemented, but a fresh request to the configured external providers is still
required.

## Requirement matrix

| MVP requirement | Implementation evidence | Current status |
| --- | --- | --- |
| Start the Streamlit application | `app.py`, `frontend/ui.py::render_app`, and the Streamlit shell regression test | Verified offline |
| Upload a filtered VCF | `frontend/ui.py::_render_variant_input` and `frontend/execution.py::execute_analysis` | Verified offline |
| Enter a filtered table manually | Fixed five-row editor with a standard primary CHROM dropdown and assembly-aware POS guidance in `frontend/ui.py`; authoritative chromosome and coordinate validation in `backend/vcf_processing.py::parse_manual_variants` | Verified offline |
| Validate and process 1–5 rows | `backend/vcf_processing.py::process_vcf` and VCF validation, gzip, multi-allelic, and row-boundary tests | Verified offline |
| Annotate every supplied variant | Direct filtered-input handoff in `backend/pipeline.py::run_variant_processing` | Verified offline |
| Obtain at least one annotation | `backend/annotation.py::annotate_variants` and unified VEP, MyVariant.info, ClinVar, and UCSC GenCC tests | Verified offline and live |
| Enter phenotypes | Local HPO search and selection controls in `frontend/ui.py` | Verified offline |
| Build an Evidence Object | `backend/report.py::build_evidence_object` and Stage 7 contract tests | Verified offline |
| Call the LLM | Provider-neutral boundary in `backend/llm.py` and report interpretation tests | Verified offline and live |
| Generate a report | `backend/report.py::build_clinical_report` | Verified offline |
| Display and download the report | `frontend/report_viewer.py::render_report_viewer` and report-viewer tests | Verified offline |
| Save the report | `backend/report.py::save_clinical_report` plus database report-reference persistence | Verified offline |
| Handle major errors | Central error mapping, partial-result behavior, safe frontend messages, logging, and security acceptance tests | Verified offline |

## Audit result

All Stage 16 MVP capabilities are implemented. No missing core feature was
found. The remaining work is acceptance and release preparation:

1. Run the dedicated deterministic MVP acceptance gate in
   `tests/run_stage16_mvp_acceptance.py`.
2. Use the verified live baseline recorded in
   `docs/STAGE_16_MVP_DEMO_RUNBOOK.md`.
3. Use the professor-facing runbook and `data/samples/mvp_demo.vcf` for the
   presentation.
4. Run `tests/run_stage16_final_acceptance.py` and use the release decision in
   `docs/STAGE_16_MVP_RELEASE_CHECKLIST.md`.

## Explicit MVP boundaries

The MVP is clinical decision support, not an autonomous diagnosis system.
Filtering, optimization, normalization, and clinical ranking are upstream
responsibilities; this application expects a professor-approved table of one
to five rows. It does not include authentication, multiple users, PostgreSQL,
Docker, cloud deployment, or institutional production approval. These remain
post-MVP work.

## Network boundary

This audit did not call any external service. Live annotation and LLM outcomes
must be reported separately so network failures are not confused with code
failures.

## Deterministic acceptance command

```powershell
.\.venv\Scripts\python.exe tests\run_stage16_mvp_acceptance.py
```

This offline gate compiles the application and runs the marked backend
end-to-end, VCF-upload boundary, Streamlit shell, manual-analysis, phenotype
selection, error-handling, and report-download checks.
