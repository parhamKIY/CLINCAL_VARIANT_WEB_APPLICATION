# Stage 11.2 Analysis Lifecycle Stability Audit

## Scope and repository state

This audit traces the current production path before Stage 11.2 changes. It
does not reopen EvidenceObject, provider-evidence, AI-classification,
interpretation-contract, execution-trace, or report-rendering semantics.

The working tree already contains the approved in-progress Stage 11.1
field-level interpretation-validation changes. Stage 11.2 must preserve those
changes and remain additive.

## Current lifecycle

```text
Streamlit submission
  -> frontend.ui._start_submission
  -> recovery request + AnalysisJob registry entry
  -> AnalysisJob daemon worker
  -> frontend.execution.execute_analysis
  -> backend.pipeline.run_analysis
  -> input validation / canonical variant processing
  -> annotation providers under provider resilience policy
  -> conditional enrichment and EvidenceObject construction
  -> sequential per-variant LLM interpretation
     -> bounded HTTP retry
     -> bounded structured-output repair when eligible
     -> optional configured model fallback when eligible
  -> draft report construction
  -> terminal analysis persistence transaction
  -> resumable pipeline-state persistence
  -> AnalysisJob completed/error/cancelled state
  -> Streamlit fragment poll
  -> final session state and report/review rendering
```

The backend pipeline converts expected stage exceptions into a validated
terminal `PipelineResult`. Per-variant typed LLM failures become explicit
failed interpretation records and do not erase successful siblings. SQLite
analysis insertion is transactional, and its busy timeout is bounded.

## Failure map

### P0 — No whole-analysis runtime deadline

- Location: `frontend/execution.py`, `AnalysisJob`; registry pruning in
  `_prune_analysis_job_registry`; polling in `frontend/ui.py`.
- Current behavior: the worker changes state only when its runner returns,
  raises an ordinary exception, or reaches a progress callback after a
  cancellation request. There is no maximum runtime. Registry pruning removes
  terminal jobs only.
- Risk: an uncooperative adapter, operating-system I/O stall, or any blocking
  dependency that fails to honor its local timeout can leave the UI in
  `Analysis in progress` or `Cancelling analysis` indefinitely. Such jobs can
  permanently consume the bounded 32-job registry. Their recovery request can
  also expire after one hour while the job still appears active.
- Recommended fix: add one configurable, bounded `AnalysisJob` deadline with
  an independent watchdog. Deadline expiry must atomically publish a terminal
  error, request cooperative cancellation, prevent late worker completion from
  overwriting the terminal state, and preserve safe cleanup behavior.

### P1 — Retry latency is locally bounded but not globally budgeted

- Location: `backend/provider_resilience.py::call_provider_with_policy`,
  `backend/llm.py::call_llm`, and
  `backend/variant_interpretation.py::_execute_interpretation_request`.
- Current behavior: provider attempts, backoff, LLM attempts, structured
  repair, and configured model fallback are individually bounded. Variant LLM
  work is sequential. Blocking retry sleeps are not cancellation-aware.
- Risk: legal combinations of provider retries, LLM retry, repair, fallback,
  and up to ten variants can produce long aggregate latency. The local bounds
  do not protect the complete lifecycle when an implementation ignores a
  request timeout.
- Recommended fix: retain local policies and cap their aggregate operational
  effect with the P0 analysis deadline. A future stage may propagate a shared
  remaining-time budget into retries; that is not required for the smallest
  P0 remediation.

### P1 — Unexpected per-variant interpretation exceptions are analysis-wide

- Location: `backend/variant_interpretation.py::interpret_variants`.
- Current behavior: `LLMError` and `VariantInterpretationError` are isolated
  into a failed variant result. An unexpected exception escapes the loop and
  is converted by the pipeline into an analysis-level failure.
- Risk: an implementation defect in one variant can prevent later siblings
  from being interpreted, even though expected provider/model failures are
  correctly isolated.
- Recommended fix: keep this fail-closed boundary unless a bounded safe
  diagnostic can be constructed for unexpected defects. Do not broadly swallow
  programming errors as ordinary clinical missingness. No change in the P0
  patch.

### P1 — Preliminary EvidenceObject failure stops later stages

- Location: `backend/pipeline.py::_build_evidence_and_report`.
- Current behavior: isolated construction preserves successful EvidenceObjects
  and per-variant outcomes, but the first preliminary or final construction
  failure stops LLM/report processing for the batch.
- Risk: usable siblings remain stored in the partial result but do not progress
  to interpretation/reporting.
- Recommended fix: handle only under the approved EvidenceObject/cardinality
  contract. It is outside Stage 11.2 P0 lifecycle remediation and must not be
  bypassed here.

### P2 — Worker-level failures lose the latest result in the final UI

- Location: `frontend/execution.py::AnalysisJob._run` and
  `frontend/ui.py::_finish_analysis_job`.
- Current behavior: an exception escaping the backend runner becomes a safe
  worker error, but the UI clears `pipeline_result`; only the safe notice and
  runtime trace snapshot remain.
- Risk: the last known stage/provider statuses are not retained in the normal
  result view for an analysis-level crash.
- Recommended fix: keep the current safe failure message for Stage 11.2. A
  later additive UI/diagnostic change could render the detached last progress
  snapshot without treating it as a valid terminal `PipelineResult`.

### P2 — Frontend deadline/cancellation is not represented by backend trace

- Location: runtime-only `AnalysisExecutionTrace` is bound inside
  `backend.pipeline.run_analysis`; the frontend job controller is outside that
  context.
- Current behavior: backend success/abort events close the trace, but a
  frontend-enforced stop can leave the trace snapshot marked active until the
  worker reaches backend cleanup.
- Risk: transient trace/UI status disagreement during cooperative unwind.
- Recommended fix: do not change the Stage 10 trace architecture in this
  stage. The job state remains the authoritative UI terminal state; document
  this residual limitation.

## Existing safety controls verified

- Provider HTTP requests in the production backend use explicit bounded
  timeouts.
- Shared provider retry attempts are capped at three, with bounded backoff and
  analysis-scoped circuit state.
- `no_match` does not activate operational fallback.
- Variant interpretation uses at most one configured HTTP retry per generation
  path, one structured repair generation when eligible, and an optional
  distinct model fallback only for eligible operational failures.
- Per-variant typed LLM failures remain explicit and preserve successful
  sibling interpretations.
- Pipeline stage exceptions are mapped to bounded safe user messages and
  terminal workflow state.
- SQLite complete-analysis writes are transactional; pipeline-state persistence
  failure degrades the result rather than leaving the frontend running.

## Stage 11.2 implementation decision

Implement only the P0 whole-analysis deadline at the frontend job boundary.
The deadline is an operational safety bound, is configurable, and does not
alter clinical data, provider meaning, interpretation contracts, or report
rendering. Existing provider and LLM timeout/retry behavior remains unchanged.

## Implemented P0 remediation

- `ANALYSIS_MAX_RUNTIME_SECONDS` defaults to 1,800 seconds and is constrained
  to a positive value no greater than 7,200 seconds.
- Each started `AnalysisJob` owns an independent daemon watchdog.
- Deadline expiry atomically publishes a terminal error, emits bounded safe
  telemetry, requests cooperative cancellation, and prevents a late runner
  result from changing the job back to completed or cancelled.
- Streamlit presents a fixed actionable timeout message without exposing an
  internal exception or payload.
- Existing provider, LLM, pipeline, persistence, EvidenceObject, interpretation,
  execution-trace, and report contracts are unchanged.

Python cannot safely force-terminate an arbitrary running thread. The watchdog
therefore guarantees a terminal user-visible job state immediately while the
worker unwinds at its next safe progress boundary. All production HTTP calls
retain explicit local timeouts; an adapter that ignores cancellation can leave
a daemon thread alive even though it can no longer overwrite the terminal job
state. Shared deadline propagation into every blocking operation remains a
future hardening option.
