# Post-Stage 114 System Hardening Roadmap

**Project:** Clinical Variant Interpretation  
**Roadmap status:** Stages 116-117 implemented and verified; Stages 118-123 remain planned
**Roadmap date:** 2026-08-14
**Implemented baseline:** Stages 0-114 and Stages 116-117
**Existing reserved stage:** Stage 115 final Word/LibreOffice visual sign-off  
**Baseline commit:** `6126a72 fix(classification): separate source evidence from system conclusion`  
**Baseline verification:** `1229 passed, 6 skipped`; Stage 60 gate `875 passed, 4 skipped`, `84.97%` coverage

## 1. Purpose

This roadmap converts the post-Stage 114 software and system audit into bounded,
test-first implementation stages. It is written as a future execution handoff: an
agent must read this document and `docs/PROJECT_DECLARATION.md`, inspect the current
Git state, implement one stage only, verify it, report the suggested commit title,
and wait for user approval before starting the next stage.

The roadmap preserves the current product boundary: the application analyzes one to
ten already-filtered germline Mendelian variants as educational/research decision
support. It does not perform sequencing, genome-wide prioritization, diagnosis, or
independent ACMG/AMP adjudication.

## 2. Numbering and execution order

`docs/PROJECT_DECLARATION.md` already reserves Stage 115 for final visual sign-off.
That number must not be reused or silently redefined. Because Stages 116-123 can
change report wording or layout, Stage 115 remains deferred until the hardening
stages are complete.

Required execution order:

1. Stage 116 — call-quality contract and interpretation gate
2. Stage 117 — call-quality propagation and reviewer disclosure
3. Stage 118 — fail-closed ReportData classification invariant
4. Stage 119 — unresolved-interpretation finalization semantics
5. Stage 120 — bounded live-validation harness
6. Stage 121 — lazy Streamlit technical surfaces
7. Stage 122 — dependency-security release gate
8. Stage 123 — classification-product scope decision
9. Stage 115 — final Word/LibreOffice visual sign-off

Stage 123 is a decision gate. Its recommended outcome is to retain the current
evidence-synthesis scope. An expert classification workflow is a separate scope
expansion and must not be inferred from ordinary human review.

## 3. Global implementation rules

- Implement exactly one numbered stage per user-approved iteration.
- Use test-driven development: add the failing regression first, run it, implement
  the minimum production change, and rerun targeted tests.
- Run `\.venv\Scripts\python.exe -m pytest -q` before declaring a stage complete.
- For release-gate changes, also run
  `\.venv\Scripts\python.exe tests\run_stage60_acceptance.py`.
- Preserve original input order, same-gene variant separation, exact allele identity,
  immutable machine evidence, source attribution, and explicit missingness.
- A provider `no_match` remains valid missingness, not an operational failure.
- External classifications remain evidence observations. They must never become an
  application classification through projection, rendering, persistence, recovery,
  or finalization.
- Do not add diagnosis, treatment, testing, or patient-management recommendations.
- Do not add authentication, RBAC, cloud deployment, or regulatory claims inside a
  stage unless Stage 123 explicitly expands the product scope.
- Update `docs/PROJECT_DECLARATION.md` only after the corresponding stage is
  implemented and verified.
- Suggested stage commits must be concise and must not be created unless requested.

## 4. Stage 116 — Call-quality contract and interpretation gate

**Status:** Implemented and verified on 2026-08-13  
**Priority:** Critical  
**Dependency:** Stage 114 complete

### Objective

Prevent variants that failed upstream VCF filters from silently entering clinical
interpretation as if they were ordinary filtered candidates.

### Required behavior

Define one deterministic call-quality state for every normalized input allele:

- `passed`: `FILTER` is exactly `PASS` after trimming and case normalization.
- `not_evaluated`: `FILTER` is absent, blank, or `.`.
- `failed`: any nonempty filter value other than `PASS`; preserve the complete filter
  code string without interpreting provider-specific code semantics.

Policy:

- `passed` variants continue normally.
- `not_evaluated` variants remain reviewable but require explicit reviewer
  acknowledgement before interpretation.
- `failed` variants are blocked before provider calls and LLM interpretation by
  default.
- A failed variant may continue only through an explicit, audited reviewer override
  containing a bounded reason and timestamp. Do not infer an override from upload or
  confirmation.
- `QUAL` is descriptive only. Do not invent a universal numeric threshold because
  quality scales and caller policies vary.

### Implemented surface

- `backend/call_quality.py` owns typed state, evaluation, validation, and audited
  override records.
- `backend/vcf_processing.py` preserves bounded FILTER codes and uses the shared
  normalizer.
- `backend/pipeline.py` gates annotation and interpretation before any provider or
  LLM call, while retaining the blocked allele state for persistence.
- `backend/database.py` persists and revalidates the call-quality record with each
  stored candidate and pipeline snapshot.
- `frontend/ui.py` previews per-allele state and obtains acknowledgement or an
  override reason before submission; recovery checkpoints retain those decisions.
- `tests/test_stage116_call_quality_gate.py` is registered as
  `stage116_call_quality` in `pytest.ini`.

### Required tests

- VCF, Excel, and manual `PASS` inputs produce `passed`.
- Blank, missing, and `.` filter values produce `not_evaluated`.
- `q10`, multiple codes, and `VQSRTrancheSNP99.00to99.90` produce `failed`.
- A failed or unacknowledged record causes zero provider and zero LLM calls.
- A validated override preserves the original filter codes, reason, and timestamp.
- Invalid, empty, oversized, or control-character override reasons fail closed.
- Persisted/recovered quality state exactly matches the original state.
- Multiallelic splitting applies the row-level quality state to every split allele.

### Acceptance gate

Verified: VCF, Excel, and manual PASS inputs remain eligible; missing FILTER requires
acknowledgement; non-PASS FILTER values cannot reach annotation or interpretation
without a bounded, timestamped override. The full offline suite passed
`1242 passed, 6 skipped`.

**Suggested commit:** `fix(input): gate interpretation on call quality`

## 5. Stage 117 — Call-quality propagation and reviewer disclosure

**Status:** Implemented and verified
**Priority:** Critical  
**Dependency:** Stage 116

### Objective

Carry `QUAL`, raw `FILTER`, derived call-quality state, and any override through the
entire evidence/report lifecycle so reviewers cannot miss upstream call limitations.

### Required behavior

- Add immutable call-quality data to the Evidence Object and Draft Variant Report.
- Add typed call-quality data to ReportData; do not store it only in prose warnings.
- Display `QUAL`, `FILTER`, quality state, and override status in the HTML preview and
  DOCX main result/method area.
- Use an `ACTION REQUIRED` notice for unacknowledged `not_evaluated` or `failed`
  states and a prominent `PARTIAL` notice for an overridden failed call.
- Final confirmation must replay and validate the quality acknowledgement/override.
- Report selection must not erase quality warnings.

### Planned implementation surface

- Modify `backend/report.py`, `backend/variant_report.py`,
  `backend/report_data.py`, `backend/report_data_projection.py`,
  `backend/report_lifecycle.py`, and `backend/evidence_confirmation.py`.
- Modify `frontend/report_preview.py`, `frontend/variant_status.py`,
  `frontend/warning_semantics.py`, and `frontend/evidence_review.py`.
- Modify `backend/report_docx.py` and, only if required by the accepted visual design,
  `templates/clinical_variant_report_v1.docx` and
  `tools/build_stage82_template.py`.
- Create `tests/test_stage117_call_quality_reporting.py`.
- Update Stage 83 and Stage 101 golden artifacts only after verifying that differences
  are limited to the intended quality disclosure.

### Required tests

- `QUAL` and `FILTER` survive input, evidence, draft, ReportData, persistence,
  recovery, HTML, DOCX, and final package generation.
- Non-`PASS` text is escaped and cannot inject HTML or control characters.
- An override never changes the original filter value or derived failed state.
- Excluded and included variants retain the same immutable quality provenance.
- Existing classification and phenotype behavior is unchanged.

### Acceptance gate

A reviewer can determine, without opening raw provider diagnostics, whether the call
passed filters, was not evaluated, or failed and was explicitly overridden.

**Verification:** `tests/test_stage117_call_quality_reporting.py`; the complete offline
suite passed `1244 passed, 6 skipped` on 2026-08-14.

**Suggested commit:** `feat(report): disclose variant call quality`

## 6. Stage 118 — Fail-closed ReportData classification invariant

**Status:** Planned  
**Priority:** High  
**Dependency:** Stage 117

### Objective

Make the source-versus-system classification boundary a schema invariant rather than
only a projection and renderer convention.

### Required behavior

While `classification_summary.independent_acmg_adjudication` is `False`:

- `conclusive_result.classification` must be `null`.
- `conclusive_result.classification_source` must be `null`.
- `conclusive_result.status` must be `not_assessed`.
- Direct ClinVar, GeneBe automated, and MyVariant-derived classifications remain only
  in `main_findings.classifications` and `classification_summary`.
- Classification conflict is a source-context conflict, never a conclusive system
  classification.
- Persistence and recovery must reject unsafe legacy/current ReportData instead of
  silently rendering it as safe.

### Planned implementation surface

- Modify `backend/report_data.py` to enforce the cross-field invariant.
- Simplify `backend/report_data_projection.py` so it cannot calculate a conclusive
  provider result for later assignment.
- Verify `backend/report_docx.py`, `frontend/report_preview.py`,
  `backend/database.py`, and `backend/report_lifecycle.py` fail closed.
- Create `tests/test_stage118_classification_invariant.py`.
- Update synthetic fixtures whose conclusive results still contain provider values.

### Required tests

- ClinVar-only, GeneBe-only, MyVariant-only, and conflicting-source records validate
  only when `conclusive_result` is unassessed.
- Manually constructed or recovered provider-derived conclusive results are rejected.
- Provider observations and review statuses remain visible after validation.
- HTML and DOCX use `System classification: Not independently determined`.
- External labels such as `Uncertain_significance` display as
  `Uncertain significance (VUS)` without mutating raw provenance.

### Acceptance gate

No supported construction, persistence, recovery, or rendering path can represent an
external provider assertion as the application's classification.

**Suggested commit:** `fix(schema): enforce source-only classifications`

## 7. Stage 119 — Unresolved-interpretation finalization semantics

**Status:** Planned  
**Priority:** High  
**Dependency:** Stage 118

### Objective

Prevent the word `finalized` from implying that every selected variant has a complete
interpretation.

### Required behavior

- Successful interpretation and failed/exhausted interpretation remain distinct.
- A failed interpretation is excluded from the selected final package by default.
- A reviewer may explicitly include it only after acknowledging that interpretation
  remains unresolved; record timestamp and bounded reason.
- If any unresolved variant is included, use the product state
  `Finalized with unresolved variants` in UI, ReportData lifecycle, final report, and
  package manifest.
- Never fabricate interpretation text to satisfy finalization.
- Retrying interpretation invalidates the unresolved acknowledgement if the result
  changes.

### Planned implementation surface

- Modify `backend/pipeline.py`, `backend/report_lifecycle.py`,
  `backend/final_clinical_report.py`, and `backend/final_docx_package.py`.
- Modify `frontend/evidence_review.py`, `frontend/analysis_summary.py`,
  `frontend/variant_status.py`, and `frontend/warning_semantics.py`.
- Create `tests/test_stage119_unresolved_finalization.py`.

### Required tests

- Exhausted interpretation is deselected by default.
- Finalization rejects selected unresolved variants without acknowledgement.
- Acknowledged inclusion preserves failure category without internal exception text.
- Package manifest and UI use the unresolved finalization label.
- A successful retry removes the unresolved state and supports ordinary finalization.
- No additional LLM call occurs during finalization.

### Acceptance gate

Every selected unresolved interpretation is an explicit, audited reviewer decision,
and no ordinary `Finalized` state contains an unresolved interpretation.

**Suggested commit:** `fix(review): distinguish unresolved finalization`

## 8. Stage 120 — Bounded live-validation harness

**Status:** Planned  
**Priority:** Medium  
**Dependency:** Stage 119

### Objective

Make live-provider validation observable, cancellable, bounded, and useful when one
provider stalls.

### Required behavior

- Add a configurable overall harness deadline with a safe default no greater than
  180 seconds for `--skip-llm`.
- Emit a concise start/result line for each provider as it completes.
- Atomically checkpoint normalized, non-clinical JSON after every provider.
- Record `not_started`, `running`, `completed`, `unavailable`, and
  `overall_deadline_exceeded` without exposing raw payloads or secrets.
- On interruption or deadline, close sessions, write the partial summary, and exit
  nonzero without orphan Python processes.
- Keep each production client's existing provider-specific timeout classification.

### Planned implementation surface

- Modify `tests/run_live_provider_validation.py` or move reusable orchestration into
  a new `tools/live_provider_validation.py` while retaining the current command.
- Use `backend/provider_resilience.py` status vocabulary where applicable.
- Create `tests/test_stage120_live_validation_harness.py` with deterministic fake
  providers and no internet access.

### Required tests

- A hung fake provider is bounded by the overall deadline.
- Earlier provider results remain in the checkpoint after a later timeout.
- Cancellation produces a valid partial JSON summary.
- Secrets, URLs containing credentials, raw responses, and clinical input are absent.
- `--skip-llm` never initializes either LLM task.
- A complete fake-provider run exits zero and preserves ordered results.

### Acceptance gate

The harness always produces an incremental summary and terminates within its declared
deadline, including when a provider never responds.

**Suggested commit:** `fix(validation): bound live provider audit`

## 9. Stage 121 — Lazy Streamlit technical surfaces

**Status:** Planned  
**Priority:** Medium  
**Dependency:** Stage 120

### Objective

Avoid computing hidden report, evidence, JSON, and provider-detail surfaces on every
Streamlit rerun.

### Required behavior

- Use dynamic tabs/expanders or an explicit segmented view so only the visible heavy
  surface renders.
- Keep the clinical report review as the default view.
- Do not defer lightweight status cards or action-required warnings.
- Keep widget keys and review state stable across view changes.
- Cache only immutable/expensive source transformations with bounded TTL or
  `max_entries`; do not cache reviewer-mutated session state.
- Preserve current accessibility labels and native Streamlit behavior.

### Planned implementation surface

- Modify `frontend/ui.py`, `frontend/evidence_review.py`,
  `frontend/results.py`, and `frontend/report_viewer.py`.
- Create `tests/test_stage121_streamlit_lazy_surfaces.py` using `AppTest` and counted
  real render helpers rather than assertions against mocked UI markup.

### Required tests

- Initial completed-analysis render does not execute the hidden provider dashboard.
- Opening technical details executes it once and displays the same content.
- Hidden original-evidence/history surfaces do not serialize large JSON.
- Changing views does not reset selected variant, draft edits, inclusion, or
  confirmation state.
- No deprecated `use_container_width` or `st.components.v1` usage is introduced.

### Acceptance gate

The default review rerun performs only report-facing work while every technical
surface remains available on demand with unchanged state.

**Suggested commit:** `perf(ui): render technical views on demand`

## 10. Stage 122 — Dependency-security release gate

**Status:** Planned  
**Priority:** Medium  
**Dependency:** Stage 121

### Objective

Extend the existing dependency-consistency and secrets checks with deterministic
dependency vulnerability auditing and explicit release evidence.

### Required behavior

- Add a pinned development-only vulnerability scanner, preferably `pip-audit`.
- Audit the resolved environment/requirements without sending project or clinical
  data to third parties.
- Fail CI on known vulnerable installed dependencies unless a repository-tracked,
  time-bounded exception records advisory ID, affected package/version, rationale,
  owner, and expiry date.
- Keep ordinary outdated-package notices informational; do not upgrade dependencies
  merely because a newer version exists.
- Run compilation, `pip check`, secrets audit, vulnerability audit, deterministic
  tests, and Stage 60 acceptance in the release gate.

### Planned implementation surface

- Modify `requirements-dev.txt` and `.github/workflows/*.yml`.
- Create `tools/run_dependency_audit.py` if exception validation is required.
- Create `docs/dependency_audit_exceptions.json` only when a real, reviewed exception
  exists; do not create an empty bypass mechanism by default.
- Create `tests/test_stage122_dependency_security_gate.py`.

### Required tests

- The gate invokes the scanner against the intended environment.
- A synthetic unexpired exception is accepted only for its exact advisory/package.
- Expired, unknown, broad, or malformed exceptions fail closed.
- The audit output contains no environment secrets.
- CI retains the deterministic Stage 60 gate.

### Acceptance gate

CI provides explicit evidence for compilation, dependency consistency, secret
scanning, vulnerability status, and the deterministic release suite.

**Suggested commit:** `ci: add dependency vulnerability gate`

## 11. Stage 123 — Classification-product scope decision

**Status:** Planned decision gate  
**Priority:** Architectural  
**Dependency:** Stage 122

### Objective

Resolve whether the product remains evidence-synthesis software or expands into an
expert ACMG/AMP adjudication system. No implementation may assume the expanded scope.

### Recommended decision: retain evidence-synthesis scope

Under the recommended decision:

- Keep `System classification: Not independently determined`.
- Keep external classifications immutable and source attributed.
- Rename any misleading `Conclusive Result(s)` template heading to
  `Variant and source classification context`.
- Treat reviewer confirmation as confirmation of evidence review and report wording,
  not confirmation of a pathogenicity classification.
- Keep finalization as an audited report disposition, not a laboratory sign-out.

Planned files under this decision:

- `templates/clinical_variant_report_v1.docx`
- `tools/build_stage82_template.py`
- `backend/report_docx.py`
- `frontend/report_preview.py`
- `frontend/evidence_review.py`
- `backend/final_clinical_report.py`
- `tests/test_stage123_evidence_synthesis_scope.py`

### Alternative decision: expert adjudication scope expansion

Do not implement this as a small extension. First create and approve a separate
architecture/specification covering:

- authenticated reviewer identity and role authorization;
- gene/disease/inheritance scope;
- every ACMG/AMP/ClinGen code, strength, evidence item, and rationale;
- evidence independence and double-counting prevention;
- criteria combination/Bayesian logic and versioned rule sets;
- call-quality and assay-quality prerequisites;
- draft, second-review, sign-out, amendment, and reanalysis workflows;
- immutable audit records and regulatory boundaries;
- qualified professional validation and external governance.

Until that separate specification is approved, the alternative branch has no
authorized production-code changes.

### Required tests for the recommended decision

- No UI, ReportData, DOCX, final report, or package calls external evidence a system
  classification.
- Confirmation/finalization wording does not imply laboratory sign-out.
- Source classifications, conflicts, limitations, and human-review requirements stay
  visible.
- The heading change is covered by HTML, DOCX, golden, and visual-regression tests.

### Acceptance gate

The user explicitly approves one product boundary. The default and recommended
accepted state is evidence synthesis; silence is not approval for expert
adjudication.

**Suggested commit for recommended decision:**
`refactor(report): clarify evidence synthesis scope`

## 12. Stage 115 — Final Word/LibreOffice visual sign-off

**Status:** Pending and deferred  
**Priority:** Final release gate  
**Dependency:** Stages 116-123 complete

### Objective

Complete the already-reserved visual fidelity review only after all report-affecting
hardening work is stable.

### Required behavior

- Render every Stage 101 scenario through Word and/or LibreOffice plus PDF raster.
- Inspect result emphasis, quality disclosure, classification wording, overflow,
  blank pages, tables, references, page breaks, and long-allele wrapping.
- Compare the accepted template family with the professor reference without adding
  confidential reference material to Git.
- Record renderer versions, scenario results, accepted deviations, reviewer, and date.
- Update golden binary/raster hashes only after manual review proves the new output is
  intentional.

### Planned implementation surface

- `tools/stage101_visual_regression.py`
- `tests/test_stage101_visual_regression.py`
- `tests/golden/stage101/`
- `docs/stage_83_fidelity_gate.md`
- `docs/PROJECT_DECLARATION.md`

### Acceptance gate

All deterministic structural tests pass, real rendered scenarios contain no clipping,
overflow, unintended blank pages, or misleading clinical wording, and external visual
sign-off is recorded truthfully.

**Suggested commit:** `docs: record final report visual sign-off`

## 13. Deferred production-scale concerns

The following are not defects in the declared bounded single-application educational
scope. They become mandatory before multi-user or clinical deployment and require a
separate approved roadmap:

- authentication, reviewer identity, RBAC, and session isolation;
- encrypted secrets and database-at-rest controls;
- PostgreSQL or equivalent multi-user persistence and migrations;
- durable distributed jobs, cancellation, leases, and idempotency;
- deployment monitoring, backup/restore, retention, and incident response;
- validated laboratory workflow, regulatory quality system, and clinical governance.

Do not represent completion of Stages 115-123 as satisfying these production or
regulatory requirements.

## 14. Completion definition

This roadmap is complete only when:

- Stages 116-123 are implemented in the declared order with passing targeted and full
  regression suites;
- Stage 123 records an explicit product-scope decision;
- the deferred Stage 115 real visual sign-off is completed afterward;
- `docs/PROJECT_DECLARATION.md` is reconciled with implemented facts rather than plans;
- live provider status is reported as point-in-time evidence, not a permanent claim;
- no remaining report path promotes external evidence into an application
  classification or hides unresolved input/interpretation quality.
