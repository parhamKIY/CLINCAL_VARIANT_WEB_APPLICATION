# Persian clinical entity extraction

## Scope

The Persian clinical-text workflow extracts explicit case-context mentions for
human review. It is an extraction and routing boundary, not a diagnostic engine.
Clinical entities never become variant evidence merely because the user entered
them.

The earlier schema `2.0` could retain disease phrases transiently, but only
accepted HPO identifiers crossed the frontend boundary. Schema `3.0` preserves
both phenotype/finding and disease/syndrome mentions through explicit review,
pipeline context, persistence, and recovery.

## Entity contract

Each bounded `ClinicalEntity` contains:

- `original_text`: sanitized explicit source phrase;
- `normalized_text`: deterministic surface normalization;
- `entity_type`: `PHENOTYPE` or `DISEASE`;
- `assertion`: `PRESENT`, `SUSPECTED`, `NEGATED`, or `HISTORICAL`.

`DISEASE` includes explicitly named syndromes. The extraction model cannot add a
disease that is not grounded in the supplied narrative. Resolver identifiers and
preferred labels are never trusted from model output.

## Persian normalization

Normalization applies Unicode NFKC, Arabic-to-Persian Yeh/Kaf mapping, removes
tatweel and Arabic diacritics, converts zero-width joiners to spaces, and collapses
whitespace. The original phrase remains separately retained. Normalization does
not translate, fuzzily rewrite, or infer a concept.

## Routing

Reviewed `PHENOTYPE/PRESENT` HPO candidates continue through local HPO validation
and the existing explicit HPO acceptance flow. Accepted HPO terms remain the only
patient-observed inputs to phenotype matching and Phen2Gene. Non-present phenotype
assertions do not become positive phenotype evidence.

Reviewed `DISEASE` entities use the deterministic local HPO disease-annotation
resolver. Resolution is exact-only and returns `RESOLVED`, `UNRESOLVED`,
`AMBIGUOUS`, or `UNAVAILABLE` with source/version and limitations. Disease names
are never forced into HPO fields. The current resolver does not perform Persian-to-
English translation or fuzzy matching; unresolved and ambiguous mentions remain
explicit.

Disease-associated HPO terms, if introduced by a future approved route, must remain
disease-derived external context. They must not be relabeled as directly observed
patient phenotypes or submitted to Phen2Gene as observations.

## Circularity and evidence boundaries

A disease mention is a case assertion, not proof of disease. External disease
knowledge, gene-disease evidence, ACMG context, variant evidence, model
interpretation, and the final human conclusion remain separate. Symptoms cannot
create a disease entity, and user-entered disease text cannot inflate pathogenicity
or independently support an ACMG criterion.

Only the sanitized Persian narrative reaches the extraction model. Structured
output is schema-validated and phrase-grounded; raw reasoning and model responses
are not stored. `clinical_entities` and `disease_resolutions` are excluded from
EvidenceObjects, interpretation prompts, ACMG reasoning, and pathogenicity report
sections.

## Reviewer workflow

After extraction, the UI separates **Observed findings** from
**Disease/context mentions**. Each row shows type and assertion. A reviewer may edit
the bounded source text or assertion and may reject a row by clearing its inclusion
checkbox. Entity type remains read-only. Edited rows are re-normalized and the
complete retained collection is revalidated before acceptance.

Extraction does not automatically accept entities. Analysis cannot start while an
entity draft awaits review. HPO suggestions linked to rejected or non-present
phenotype entities are removed before the separate HPO acceptance action.

The results-review surface displays reviewer-confirmed entities and disease
resolution state in a clearly labeled clinical-context panel. It explicitly states
that the content is not variant evidence, a diagnosis, or an ACMG classification.

## Persistence and recovery

Pipeline schema `3.5` introduced nullable `clinical_entities`; schema `3.6` added
nullable `disease_resolutions`. New analyses use a reviewed list, including `[]`
when no entity was retained. Historical snapshots migrate missing fields to `null`,
which means not captured rather than a fabricated negative history.

The canonical pipeline JSON snapshot persists reviewed entities and resolution
context. Recovery request schema `4` carries reviewed entities for interrupted,
unpersisted work. SQLite schema `4` remains unchanged because the canonical snapshot
already provides validated, bounded, atomic persistence; normalized SQLite
projections do not need these fields for report recovery. Save, load, refresh, and
restart recovery revalidate the existing contracts.

Pending editor drafts are frontend-only. Presence in a submitted/persisted analysis
is the durable reviewed state; rejected rows are not submitted or stored as clinical
context.

## Current limitations

- The deterministic disease resolver is exact-only and depends on installed local
  disease annotations.
- Persian disease phrases commonly remain unresolved unless extraction supplies a
  safely matching normalized label; no translation or fuzzy guess is performed.
- Reviewer edits are bounded to existing extracted rows; this stage does not create
  a patient-chart subsystem or a separate rejected-entity audit ledger.
- Clinical context is intentionally not added to pathogenicity report sections.
- Human review remains mandatory for all extracted entities, HPO selections,
  interpretations, and reports.
