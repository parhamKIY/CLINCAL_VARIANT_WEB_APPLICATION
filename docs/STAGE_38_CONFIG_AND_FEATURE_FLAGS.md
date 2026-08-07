# Stage 38 — Config and Feature Flags Expansion

Status: complete and offline verified

Date: 2026-08-08

## Central configuration path

Runtime configuration follows one path:

`.env` → `config.py` → provider and orchestration modules

Provider modules do not call `os.getenv`, access `os.environ`, or load dotenv
files directly. A source-level regression check enforces this boundary.

## Centralized settings

The configuration now includes:

- `PHEN2GENE_BASE_URL`
- `MONARCH_BASE_URL`
- `GNOMAD_BASE_URL`
- `GNOMAD_DATASET_GRCH37`
- `GNOMAD_DATASET_GRCH38`
- `LITVAR_BASE_URL`
- `EUROPE_PMC_BASE_URL`
- `PUBMED_BASE_URL`
- `LLM_MODEL_LIGHT`
- `LLM_MODEL_STRONG`
- `ENABLE_GNOMAD_DEEP_LOOKUP`
- `ENABLE_LITERATURE_ENRICHMENT`

All URLs require credential-free HTTPS. The Monarch default targets the
official v3 API base. The two LLM role settings retain their Stage 35 fallback
to `LLM_MODEL`.

## Feature flags

Boolean environment values accept `true/false`, `1/0`, `yes/no`, and `on/off`
case-insensitively. Missing flags default to enabled to preserve existing
behavior; ambiguous values fail configuration loading.

When a triggered lookup is disabled:

- no request is sent to that provider;
- the enrichment remains `not_triggered`;
- `failure_reason` is `disabled_by_configuration`;
- a visible bounded warning explains which feature is disabled;
- variant order and all other evidence remain unchanged.

## Secret handling

Provider code receives keys only through `settings`. Central logging redacts
both `LLM_API_KEY` and the optional `GENEBE_API_KEY`, including interpolated
arguments. Provider URLs, credentials, request payloads, and clinical values
are not emitted as configuration telemetry.

## Verification

- Strict boolean parsing accepts documented values and rejects ambiguity.
- Unsafe Monarch and existing provider URLs are rejected.
- Backend provider source files contain no direct environment access.
- Disabling both enrichment flags makes zero gnomAD or literature requests.
- Disabled triggered lookups retain explicit status and warnings.
- Configured GeneBe credentials are redacted from logs.
- The full regression suite passes with mocked providers.

No live provider calls were required for this configuration stage.

## Completion condition

All Stage 38 provider settings and feature flags are centrally configurable,
provider modules consume only validated settings, and secrets remain excluded
from logs.
