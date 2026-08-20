# Live-Run Stage 3 Provider Resilience Audit

Date: 2026-08-20

Stage: Stage 3 — Provider Failover / Retry Orchestration + Readiness-Aware Fast Fail

Base branch: `master`

Base commit: `38ea9a79983e81d5f3acf0fdb1cf8fa217693eb8`

## Scope and contract review

Stage 1 and Stage 2 were reviewed before this work. Stage 1 established that the
live evidence-construction failure was a serialization-bound failure rather than
unsafe canonical identity. Stage 2 added deterministic construction isolation and
resilience. Stage 3 changes operational request orchestration only.

The provider roles, exact-identity rules, biological evidence semantics, LLM
boundary, human-review contract, and fail-closed validation behavior remain
unchanged. Readiness is operational context, not evidence.

No Stage 4 observability cleanup or later-stage work is included.

## Retry-policy map before Stage 3

| Category | Providers/paths | Pre-Stage-3 behavior |
|---|---|---|
| Shared `provider_resilience` wrapper | gnomAD, VariantValidator, Ensembl Variation, ERepo, MedGen, MyDisease, Phen2Gene | Shared timeout/retry taxonomy, bounded `Retry-After`, normalized status, and analysis-scoped circuit where supplied. |
| Provider-specific retry loops | VEP, GeneBe, MyVariant.info, direct ClinVar, ClinGen/GenCC, CSpec, conditional literature requests, LLM adapter | Each client owned a separate `max_retries + 1` loop. The LLM boundary is out of Stage 3 scope. |
| Nested client plus failed-variant retry | VEP, GeneBe, MyVariant.info, direct ClinVar, ClinGen/GenCC, CSpec | A failed source could be retried by the client and then retried again by `_retry_failed_source_annotations`. |

The live-proven defect was the batch-wide VEP and GeneBe outage path. With
`max_retries=2` and four variants, each provider made three batch attempts and
then two rounds of four single-variant attempts: `3 + (2 * 4) = 11` calls.
Provider-wide failures were therefore multiplied by variant count before VEP
fallback or GeneBe continuation.

The other nested paths remain recorded as an audit finding. They were not
rewritten because Stage 3's reproduced live target and deterministic matrix are
the VEP and GeneBe batch endpoints; broad provider rewrites would change
unproven paths and exceed the approved minimal scope.

## Reproduction and TDD evidence

The Stage 3 tests were added before the orchestration implementation. The RED run
proved both defects:

- readiness context could not be supplied to `annotate_variants`;
- four-variant VEP outage: 11 direct VEP calls;
- four-variant GeneBe outage: 11 direct GeneBe calls.

The tests use queued exceptions, fake responses, fake clocks or fixed timestamps,
attempt counts, and selected timeout tuples. They do not depend on wall-clock
latency.

Mandatory pre-checkpoint review then identified three additional defects. New
RED tests reproduced all three before correction:

- a successful fast confirmation did not consume the fresh-unreachable hint;
- generic HTTP 400 variant rejection opened the VEP/GeneBe provider circuit;
- a provider-wide failure during independent variant isolation allowed later
  outer retry rounds and sleeps.

The corrected tests prove the consumed confirmation state for both VEP and
GeneBe, mixed success/rejection isolation for both providers, and immediate
outer-loop termination when a circuit opens during isolation.

Final review also required preserving the normal bounded shared policy for an
isolated call. Deterministic tests now prove timeout-then-success recovery for
both providers. Only persistent operational failure after that bounded policy
opens the circuit and aborts remaining outer rounds.

## Readiness snapshot contract

`ProviderReadinessResult` now records a per-provider UTC `checked_at` value.
`ProviderReadinessSnapshot` is an immutable tuple of detached, secret-free
results containing only provider, operational state, check time, latency/status
metadata, probe kind, and a safe normalized failure category.

The frontend captures the current sidebar results at submission and passes the
snapshot through `execute_analysis`, `run_analysis`, and annotation orchestration.
Backend provider code does not read Streamlit session state.

The snapshot is request-local and in-memory only. It is absent from
`PipelineResult`, SQLite, recovery requests, annotations, EvidenceObjects, LLM
payloads, reports, and evidence scoring/coverage.

## Freshness and confirmation policy

The readiness TTL is exactly 300 seconds (five minutes):

- fresh reachable: normal bounded primary request;
- fresh unreachable: one confirmation request with a 1-second connect timeout
  and 3-second read timeout;
- stale, missing, malformed-time, or future-time result: normal bounded primary
  request;
- no readiness snapshot: normal bounded primary request.

A failed fresh-unreachable confirmation opens the provider's analysis-scoped
circuit immediately. It does not spend the normal multi-attempt budget. A
successful confirmation consumes the unreachable hint for that provider in
that analysis and preserves the primary provider under the normal bounded
policy for later batches and isolated retries.

Normal VEP and GeneBe requests now use the shared `ProviderRetryPolicy`, with
`max_retries + 1` attempts capped by the existing shared maximum of three. The
normal request timeout is `(min(5 seconds, provider read timeout), provider read
timeout)`. Shared `Retry-After`, 403 non-retry, 429, 5xx, connection, timeout, and
invalid-response taxonomy remains in force.

## Provider-wide and variant-level distinction

Provider-wide operational failures include batch connection/timeout failures,
HTTP 401/403/408/429, fixed-endpoint/client-contract failures such as
404/405/410, 5xx failures, and malformed batch-wide responses. They are
retained as normalized `primary_failure` plus
`failure_scope="provider_wide"`. A terminal provider-wide failure suppresses
`_retry_failed_source_annotations` for that provider. If the circuit opens
during independent isolation, remaining variants are marked from the local
open-circuit result without network calls, later retry rounds and sleeps stop,
and fallback or continuation begins immediately.

HTTP 400/409/413/422 responses are request/variant-level rejection, not proof of
a provider-wide outage. They do not open the circuit or activate VEP
operational fallback. A rejected batch is independently isolated within the
existing outer variant budget, so valid siblings can still succeed.

A valid VEP response with no record remains `not_found`, has no primary
operational failure, is not retried as an outage, and does not activate fallback.
A GeneBe record that is invalid only for one variant is marked
`failure_scope="variant_level"` and remains eligible for the existing independent
variant retry.

## VEP behavior and provenance

VEP remains the preferred primary annotation provider. A terminal VEP
provider-wide failure opens the analysis-scoped VEP circuit; later VEP batches in
that analysis are skipped. VariantValidator fallback starts immediately for each
affected variant.

Successful fallback retains:

- primary provider: Ensembl VEP;
- normalized primary operational failure;
- fallback provider: VariantValidator;
- fallback role and source URL;
- exact assembly/allele mapping provenance;
- approved gene, transcript, HGVS.c, and HGVS.p mapping only.

VariantValidator does not fabricate VEP consequence, impact, MANE, canonical, or
predictor fields.

## GeneBe behavior

GeneBe now uses the same bounded operational policy and an analysis-scoped
circuit. A terminal provider-wide failure marks GeneBe automated ACMG context
unavailable and the remaining provider pipeline continues.

GeneBe has no semantically equivalent fallback. ERepo remains separately
attributed expert-curated context; it is never relabeled as GeneBe, and no
automated classification or ACMG criteria are fabricated.

## Deterministic attempt proofs

| Scenario | Before | After |
|---|---:|---:|
| Four variants, fresh-unreachable VEP, four batches | 11 VEP calls | 1 short VEP confirmation, then 4/4 VariantValidator rescue attempts |
| Four variants, unknown VEP outage | 11 VEP calls | 3 bounded VEP attempts total, then 4/4 VariantValidator rescue attempts |
| Stale VEP readiness with transient timeout | N/A | 2 normal attempts; primary recovers without fallback |
| Fresh-reachable VEP | N/A | 1 normal primary call |
| Valid VEP no-match | N/A | 1 call; no retry and no fallback |
| Four variants, fresh-unreachable GeneBe | 11 GeneBe calls | 1 short GeneBe confirmation; pipeline continues |
| Four variants, unknown GeneBe outage | 11 GeneBe calls | 3 bounded GeneBe attempts total; pipeline continues |
| GeneBe variant-level invalid record then recovery | N/A | 1 batch call plus 1 independent variant retry |
| VEP/GeneBe successful fast confirmation, later transient timeout | N/A | 1 short confirmation; later batch receives the normal bounded policy and recovers |
| VEP/GeneBe batch HTTP 400 with mixed isolated results | N/A | 1 rejected batch plus 2 isolated calls; valid sibling succeeds, rejected sibling remains explicit, circuit stays closed |
| VEP 404 / GeneBe 405 fixed-endpoint failure | N/A | 1 provider call; circuit opens without per-variant fanout |
| VEP/GeneBe isolated transient timeout then success | N/A | normal two-attempt shared policy recovers; circuit stays closed |
| VEP/GeneBe persistent failure during two-variant isolation | N/A | isolated call exhausts 3 bounded attempts; remaining variant is marked locally and later outer rounds/sleeps are suppressed |
| Two separate analyses with fresh-unreachable VEP | N/A | 1 confirmation in each analysis (`[1, 1]`), proving circuit isolation |

The gnomAD direct/fallback implementation was not changed. Its existing 403
non-retry, circuit-open, later-call suppression, fallback eligibility, and
correlated-mirror semantics remain covered by regression tests.

## Schema and semantic status

- PipelineResult schema: unchanged at `3.4`.
- SQLite schema: unchanged at `4`.
- Recovery schema: unchanged at `3`.
- EvidenceObject schema: unchanged at `2.5`.
- Evidence construction outcome schema: unchanged at `1.0`.
- `failure_scope` is intentional flexible provider-failure provenance within
  annotation payloads; it does not carry readiness/circuit state or alter a
  versioned schema contract.
- Biological evidence semantics: unchanged.
- Provider roles and fallback meanings: unchanged.
- LLM and clinical interpretation logic: unchanged.

## Verification

- Stage 3 plus provider/VEP/GeneBe/readiness/gnomAD/fallback focused gate:
  `259 passed`.
- Stage 6A/6B, Stage 7/8, Stage 9/10, selected-input, and Stage 1/2 evidence
  gate: `109 passed`.
- Full offline regression: `1510 passed, 6 skipped`.
- Testing V3: `1038 passed, 4 skipped, 474 deselected`; coverage
  `82.95%`, above the required 80%.
- Stage 78: compilation passed; secrets audit passed; multi-variant scenario
  `1 passed, 1515 deselected`; full regression `1510 passed, 6 skipped`;
  Testing V3 passed; final Stage 78 gate passed.
- Standalone final secrets audit: passed.

## Files changed

- `backend/annotation.py`
- `backend/pipeline.py`
- `backend/provider_readiness.py`
- `frontend/execution.py`
- `frontend/provider_readiness.py`
- `frontend/ui.py`
- `tests/test_pipeline.py`
- `tests/test_erepo_orchestration.py`
- `tests/test_live_run_stage3_provider_orchestration.py`
- `docs/live_run_stage3_provider_resilience_audit.md`

## Remaining risks and limitations

- A readiness result can become stale immediately after snapshot capture; the
  short confirmation and five-minute TTL deliberately prevent readiness from
  becoming permanent truth.
- Provider-specific retry plus outer variant-retry structure still exists in
  MyVariant.info, direct ClinVar, ClinGen/GenCC, and CSpec. No equivalent live
  multiplication was reproduced for those per-variant paths in Stage 3, so they
  were documented without an unrelated rewrite.
- Readiness is optional. Recovery executions without an in-memory snapshot use
  the normal bounded provider policy.
- No live-provider availability claim is made by deterministic offline tests.
