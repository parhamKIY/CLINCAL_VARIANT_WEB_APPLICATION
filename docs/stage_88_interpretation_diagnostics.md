# Stage 88 - Interpretation Error Taxonomy and Observability

## Implemented taxonomy

Failed variant interpretations now persist a bounded failure category instead of a
Python exception class name:

```text
request_timeout
connection_error
http_429
http_5xx
authentication_error
invalid_request
empty_response
output_schema_failure
output_parse_failure
safety_or_finish_failure
internal_conversion_failure
unknown_failure
```

The existing `error_type` result field is retained for schema compatibility, but its
value is now one of these stable failure categories. Draft and ReportData projections
therefore retain an actionable category without depending on internal class names.

## Structured diagnostics

Every isolated interpretation failure emits one `variant_interpretation_failed` log
record with:

- assembly-qualified, SHA-256 `variant_id`;
- configured `model` and `prompt_version`;
- final `attempt` number;
- normalized `failure_type`;
- bounded `http_status`, `finish_reason`, and `schema_error` metadata;
- `fallback_used=false` until a later recovery stage implements fallback behavior.

Provider exceptions retain only bounded diagnostic metadata. The structured record
never logs exception messages, prompts, model responses, request headers, API keys, or
other secrets. Unsafe provider metadata is replaced with `unrecognized`.

## User-facing behavior

The report review page shows a concise generic availability message for a failed model
request. Internal exception names, HTTP details, and taxonomy values are not displayed
to the reviewer by default. The collected evidence and report remain reviewable.

## Scope boundary

Stage 88 classifies and exposes the result of the current request policy. It does not
add retries, structured output repair, model fallback, or recovery sequencing; those
belong to Stage 89.

## Acceptance evidence

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m stage88_interpretation_diagnostics
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\run_stage60_acceptance.py
```

Validated on 2026-08-11:

- Stage 88 diagnostics gate: `15 passed`;
- all twelve required failure categories covered;
- structured fields and secret omission verified;
- full repository suite: `1003 passed, 4 skipped`;
- Stage 60 release gate: passed at `85.75%` coverage.

Stage 89 retry and recovery work is not implemented here.
