# Stage 41 — Failure handling and resilience

Status: complete and offline verified

Date: 2026-08-08

## Provider resilience

Every external annotation provider now has an independent configurable timeout.
Annotation, Phen2Gene, MyDisease, conditional-enrichment, and LLM calls use
bounded retries with exponential backoff; authentication, configuration,
validation, and malformed LLM responses are not retried.

The annotation cache is process-local, bounded, and time-limited. Its key
contains the genome assembly, normalized alleles, provider endpoints, and known
API-version context. Cached objects are deep-copied, caller-specific variant
fields are restored, and failed provider results are never cached. Phen2Gene
and MyDisease retain their existing bounded normalized caches.

Operational provider outcomes distinguish usable success, partial results,
unavailable services, and invalid responses. Scientific missingness remains
separate (`not_found` or `no_association`) and is never converted into negative
evidence.

## Missingness rules

- No ClinVar record does not mean Benign.
- No Phen2Gene hit does not mean unrelated.
- No Monarch evidence does not mean negative evidence.
- A timeout does not mean negative evidence.
- A missing predictor remains missing and is not assigned zero.
- Unknown classification does not mean Benign.

## Review and LLM recovery

Provider failures cannot mutate the immutable machine report or discard saved
review drafts. LLM failures remain isolated per variant and preserve every
confirmed Reviewed Evidence Package plus all successful interpretations.

Completed Output B reports with failures expose `Retry failed interpretations`.
Only failed variants are routed again through their deterministic low-cost or
strong-model path. Successful results are retained. The old in-memory result is
left unchanged if retry setup fails, and every retry outcome is persisted as a
new validated pipeline snapshot when an analysis ID exists.

## Verification

- Provider-specific deadlines reach their corresponding HTTP calls.
- Transient LLM failures retry with bounded exponential backoff.
- Authentication failures are not retried.
- The normalized annotation cache avoids duplicate provider calls and does not
  retain caller-specific genotype data.
- Successful interpretations remain unchanged while failed variants retry.
- Confirmed evidence remains intact after both successful and failed retries.
- Existing missingness regression tests preserve null, empty, `not_found`, and
  `no_association` values without benign, unrelated, negative, or zero defaults.
- The complete offline suite passes with network boundaries mocked.

## Completion condition

No single provider or LLM failure causes collected evidence, reviewer edits,
confirmed packages, or successful interpretations to be lost.
