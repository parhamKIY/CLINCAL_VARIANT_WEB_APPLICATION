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
| Upload a VCF | `frontend/ui.py::_render_variant_input` and `frontend/execution.py::execute_analysis` | Verified offline |
| Process the VCF | `backend/vcf_processing.py::process_vcf` and VCF validation, gzip, multi-allelic, and boundary tests | Verified offline |
| Select candidate variants | `backend/prioritization.py::prioritize_variants` and deterministic bounded-selection tests | Verified offline |
| Obtain at least one annotation | `backend/annotation.py::annotate_variants` and unified VEP, MyVariant.info, ClinVar, and UCSC GenCC tests | Verified offline; live validation pending |
| Enter phenotypes | Local HPO search and selection controls in `frontend/ui.py` | Verified offline |
| Build an Evidence Object | `backend/report.py::build_evidence_object` and Stage 7 contract tests | Verified offline |
| Call the LLM | Provider-neutral boundary in `backend/llm.py` and report interpretation tests | Verified offline with a fake provider; live validation pending |
| Generate a report | `backend/report.py::build_clinical_report` | Verified offline |
| Display and download the report | `frontend/report_viewer.py::render_report_viewer` and report-viewer tests | Verified offline |
| Save the report | `backend/report.py::save_clinical_report` plus database report-reference persistence | Verified offline |
| Handle major errors | Central error mapping, partial-result behavior, safe frontend messages, logging, and security acceptance tests | Verified offline |

## Audit result

All Stage 16 MVP capabilities are implemented. No missing core feature was
found. The remaining work is acceptance and release preparation:

1. Add one dedicated deterministic MVP acceptance gate that proves the complete
   workflow in a single command.
2. Run a fresh live analysis against annotation and LLM providers using only
   public or synthetic data.
3. Prepare a concise professor-facing demo runbook and known-good input.
4. Run the final MVP acceptance gate and record the release decision.

## Explicit MVP boundaries

The MVP is clinical decision support, not an autonomous diagnosis system. It
does not include authentication, multiple users, PostgreSQL, background jobs,
Docker, cloud deployment, or institutional production approval. These remain
post-MVP work.

## Network boundary

This audit did not call any external service. Live annotation and LLM outcomes
must be reported separately so network failures are not confused with code
failures.
