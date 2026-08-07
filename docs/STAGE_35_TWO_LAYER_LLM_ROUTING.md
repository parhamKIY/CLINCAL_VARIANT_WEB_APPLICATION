# Stage 35 — Two-Layer LLM Routing

Status: complete and offline verified

Date: 2026-08-08

## Scope

Stage 35 preserves the provider-neutral LLM client and adds two logical roles
that operate only on confirmed Stage 34 Reviewed Evidence Packages.

## Deterministic routing

- `LLM-1` handles `none` or `minor` post-review conflict severity.
- `LLM-2` handles `moderate`, `major`, or `critical` severity.
- Routing is deterministic and cannot be selected by model output.
- No LLM request is permitted without confirmed reviewed evidence.

## LLM-1

The light model synthesizes classification context, phenotype relationship,
source agreement, relevant human-added evidence, uncertainty, and evidence
limitations. Its `resolution_status` is always `not_applicable`.

## LLM-2

The strong model analyzes source disagreement, review quality, freshness,
condition/transcript context, lineage, and human-added evidence. It may return
`resolved` or `unresolved`; it is never forced to resolve a conflict.

## Safety and output contract

- Both roles receive the bounded full Reviewed Evidence Package.
- Privacy validation runs before every request.
- JSON values are treated as data, not instructions.
- Models may not invent ACMG criteria, diseases, phenotype associations,
  sources, or classifications.
- Missing evidence is not treated as benign evidence.
- Responses must follow a strict bounded JSON contract containing final
  interpretation text, resolution status, and warnings.
- Invalid responses and provider failures remain isolated per variant.
- Confirmed evidence remains available for retry after an LLM failure.

## Configuration and provenance

- `LLM_MODEL_LIGHT` configures `LLM-1`.
- `LLM_MODEL_STRONG` configures `LLM-2`.
- Both default to the existing `LLM_MODEL` for backward compatibility.
- Provider protocol, configured model, response model, prompt version,
  timestamp, and token usage are recorded without storing prompts in logs.

Prompt versions:

- `LLM-1`: `35.1.0`
- `LLM-2`: `35.2.0`

## Verification

- No-conflict packages route only to `LLM-1`.
- Meaningful conflicts route only to `LLM-2`.
- `unresolved` is accepted for `LLM-2`.
- Invalid JSON and provider timeouts are isolated.
- Failure of one variant does not remove successful results for other variants.
- Reconfirmation invalidates only the affected prior routing result.
- Full regression suite passes with mocked providers.

Live-provider generation was not run because it would consume configured
provider quota; the provider-neutral boundary is verified offline.

## Completion condition

Every confirmed package is routed by deterministic post-review conflict
severity to the correct logical model, produces a bounded validated result or
an isolated failure record, and preserves model/provider provenance.
