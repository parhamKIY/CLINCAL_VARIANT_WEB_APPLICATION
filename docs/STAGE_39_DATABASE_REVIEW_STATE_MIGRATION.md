# Stage 39 — Database / Cache / Review-State Migration

Status: complete and offline verified

Date: 2026-08-08

## Schema audit and migration

The prior SQLite schema stored only terminal analysis metadata, candidates,
Evidence Objects, and a report reference. It could not preserve the editable
Stage 33 reports or resume the Stage 37 two-phase workflow.

Database schema version `2` adds one normalized `pipeline_states` table. The
only supported migration is the exact version `1` schema to version `2`; it
creates the new table transactionally and preserves all existing analysis
rows. Unknown, malformed, and newer schemas remain rejected.

## Draft and Confirmed state

`save_pipeline_state()` validates the complete Pipeline V2 contract before a
bounded deterministic JSON snapshot is upserted. The snapshot retains original
evidence, editable reports, append-only edit history, user-added evidence,
pre- and post-review conflict audits, confirmation timestamps, LLM routing,
final interpretations, and provider/model provenance without duplicating those
fields across database columns.

The table stores queryable `review_state`, `workflow_state`, pipeline schema
version, and update time beside the snapshot. `review_state` is derived rather
than trusted:

- `draft`: one or more variants still lack a confirmed reviewed package;
- `confirmed`: every variant has an immutable confirmed package.

`load_pipeline_state()` revalidates the JSON and rejects inconsistent table
metadata. `run_analysis()` automatically saves its Phase A Draft after assigning
the analysis ID. `resume_saved_analysis()` reloads that Draft, requires confirmed
evidence for every variant, runs Phase B, and replaces it with the completed
Confirmed state.

## Cache-key audit

No allele-level cache exists in the current backend, so no speculative cache
table or unused key builder was added. The existing caches are scoped to their
actual data:

- Phen2Gene keys include provider context, normalized HPO terms, and genes;
- MyDisease keys include normalized gene identity plus provider and schema
  version context.

When a future variant-level cache is introduced, its key must include genome
assembly, the normalized chromosome/position/reference/alternate allele, and
provider plus provider-version context. A gene- or HPO-scoped key must not be
misrepresented as an allele cache key.

## Safety and verification

- Pipeline snapshots are capped at 8 MiB and must be JSON-safe.
- Analysis IDs must already exist and retain foreign-key cascade behavior.
- Snapshot updates also synchronize bounded status and warnings metadata.
- Reads revalidate the Pipeline V2 schema and cross-check analysis identity,
  workflow state, review state, and pipeline schema version.
- Tests cover version `1` migration, Draft round trips, preserved edit history
  and provenance, saved Draft resumption, Confirmed replacement, and tamper
  rejection.

## Completion condition

An analysis can be saved after Phase A as a Draft, loaded later, resumed only
after complete human confirmation, and persisted as the completed Confirmed
analysis without losing its original evidence or review audit trail.
