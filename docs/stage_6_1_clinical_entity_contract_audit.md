# Stage 6.1 — Clinical Entity Extraction Contract Audit and Schema Design

## Scope and outcome

This artifact audits the current Persian clinical-text boundary and designs a
future contract. It does not change production extraction, normalization,
provider routing, evidence semantics, interpretation, or UI behavior.

The current implementation is not purely phenotype-only at the LLM response
boundary: schema `2.0` can retain a disease or syndrome as a bounded raw
`disease_mentions` phrase. The semantic loss occurs immediately after that
boundary. Non-HPO mentions are displayed from temporary Streamlit session state,
but they cannot enter the analysis request, `PipelineResult`, persistence,
recovery, resolution, or evidence-routing contracts.

## 1. Baseline

| Item | Current value at audit start |
| --- | --- |
| Branch | `master` |
| HEAD | `0a48517f4bbf98e945ad0d722e74039a56cf81c8` |
| Working tree | clean |
| Pipeline schema | `3.4` |
| EvidenceObject schema | `2.5` |
| SQLite source schema | `4` |
| SQLite file `PRAGMA user_version` | `4` |
| Recovery request schema | `3` |
| Extraction schema | `2.0` |
| Extraction prompt | `phenotype-extraction-v2.0` |
| Full offline baseline before the RED test | `1575 passed, 6 skipped` |
| Focused extraction baseline before the RED test | `13 passed` |
| Testing V3 phenotype marker baseline before the RED test | `40 passed, 1541 deselected` |

The required `docs/live_run_stabilization_closeout.md` artifact is absent from
the current worktree and from repository history. The other three required
authoritative documents were read completely. The missing artifact did not make
the extraction path unknowable, but it is a documentation gap and remains a
Stage 6 review risk.

## 2. Current extraction and routing path

```text
frontend/ui.py::_render_phenotype_extraction
  -> backend/phenotype_llm.py::extract_hpo_candidates
     -> backend/privacy.py::sanitize_phenotype_clinical_text
     -> backend/privacy.py::validate_phenotype_extraction_payload
     -> backend/llm.py::call_llm
     -> backend/phenotype_llm.py::_parse_extraction
  -> backend/phenotype_selection.py::validate_hpo_candidates
     -> backend/phenotype.py::validate_hpo_id
     -> backend/phenotype.py::lookup_hpo_term/search_hpo_terms
  -> frontend/ui.py HPO editor and explicit acceptance
     -> backend/phenotype_selection.py::accept_hpo_candidates
     -> backend/phenotype.py::normalize_phenotypes
  -> frontend/ui.py::_prepare_input
  -> frontend/execution.py::execute_analysis / recovery checkpoint
  -> backend/pipeline.py::run_analysis
     -> validate_analysis_input / validate_analysis_context
     -> match_phenotypes
     -> enrich_with_phen2gene
     -> enrich_with_mydisease
     -> enrich_with_medgen / enrich_with_medgen_phenotype_gene
     -> enrich_with_medgen_gene_disease
     -> build_evidence_objects_isolated
  -> EvidenceObject phenotype_relationship and downstream interpretation/report
```

Only locally accepted HPO identifiers cross `_prepare_input`. The downstream
phenotype route is therefore:

```text
accepted HPO ID
  -> local ontology canonicalization
  -> local exact HPO/gene overlap
  -> Phen2Gene or its approved local fallback
  -> gene-keyed MyDisease context
  -> gene/HPO-keyed MedGen context
  -> EvidenceObject phenotype relationship
```

There is no parallel user-entered disease route.

## 3. Current extraction contract

The LLM receives exactly a bounded, de-identified `clinical_text_fa` value and
`task=extract_hpo_candidates`. It receives no variants, provider payloads,
EvidenceObjects, patient identifiers, or interpretation data.

The strict response has five required arrays:

| Field | Representation | Current meaning |
| --- | --- | --- |
| `phenotype_candidates` | `hpo_id`, `label`, `source_phrase_fa` | affirmed/current phenotype proposed as HPO |
| `disease_mentions` | `source_phrase_fa` only | raw disease/syndrome phrase |
| `negated_phenotype_mentions` | `source_phrase_fa` only | negated phenotype |
| `uncertain_phenotype_mentions` | `source_phrase_fa` only | suspected/uncertain phenotype |
| `unmapped_clinical_phrases` | `source_phrase_fa` only | grounded but unmapped phrase |

Current bounds are 25 phenotype candidates, 25 items per non-HPO category,
and 50 total mentions. The parser requires exact fields, valid JSON, a safe
finish reason, unique HPO identifiers, grounded Persian source phrases, and no
cross-category duplicate phrase. It also rejects a phrase containing a syndrome
marker when the model returns that phrase as an HPO phenotype candidate.

The prompt correctly prohibits diagnosis invention and says that explicit
disease/syndrome names belong in `disease_mentions`. However, the schema does not
provide a general entity record:

- entity type is implied by the containing array rather than stored;
- disease assertion state is absent;
- phenotype `PRESENT`, `NEGATED`, and uncertain state are split across arrays;
- `HISTORICAL` is not representable;
- `SUSPECTED` is not distinguishable from other uncertainty;
- normalized surface text is absent;
- disease identifier and resolver provenance are absent;
- source offsets are absent;
- confidence is absent.

The contract is especially ambiguous for a negated, suspected, or historical
disease because the only disease category has no assertion field, while the
negated/uncertain categories are phenotype-specific.

## 4. Persian normalization and HPO behavior

`sanitize_phenotype_clinical_text` trims the input, enforces the 4,000-character
and control-character bounds, and redacts recognized identifiers. It does not
perform ontology normalization.

`_normalize_persian_for_grounding` applies NFKC, maps Arabic Yeh/Kaf variants,
removes tatweel and Arabic diacritics, converts ZWNJ/ZWJ to spaces, and collapses
whitespace. This normalized form is used only to prove that each returned phrase
occurs in the sanitized input. It is not retained as a normalized clinical entity.

HPO resolution is deterministic after model output: exact-format HPO identifiers
and labels are checked against the installed local ontology, alternate IDs are
canonicalized, and invalid/ambiguous candidates are rejected before review. The
current local search indexes HPO labels and synonyms; it is not a Persian disease
resolver.

## 5. Exact Wallenberg loss point

For the deterministic input:

```text
بیمار نابینایی دارد و سندروم والنبرگ دارد.
```

a controlled current-schema response can retain:

- blindness as `HP:0000618` in `phenotype_candidates`;
- `سندروم والنبرگ دارد` in `disease_mentions`.

The disease is therefore not necessarily lost inside `call_llm` or
`_parse_extraction` at current HEAD. It is lost as actionable, durable case
context in `frontend/ui.py::_render_phenotype_extraction`:

1. `disease_mentions` is copied only to `phenotype_non_hpo_mentions` Streamlit
   session state and rendered as an informational message.
2. The stored extraction provenance contains model/schema metadata and candidate
   HPO IDs only.
3. `_prepare_input` builds an `AnalysisSubmission` containing `phenotypes` and
   HPO extraction provenance, but no disease or generalized entity collection.
4. `AnalysisInput`, `AnalysisContext`, and `PipelineResult` have no clinical
   entity field; `validate_analysis_context` rejects additional fields.
5. Recovery schema `3` and SQLite schema `4` persist accepted HPO IDs and bounded
   extraction provenance only.
6. No disease resolver or disease-specific routing function receives the phrase.

The temporary disease display is also cleared when the source text changes and is
not restored from a saved analysis. This is the exact production handoff where a
successfully extracted raw disease mention ceases to be represented.

## 6. Existing disease-related capabilities

| Capability | Current query basis | Can resolve Persian user disease text now? |
| --- | --- | --- |
| Local HPO ontology | HPO ID/English label/synonym | No; phenotype ontology only |
| Local `phenotype.hpoa` | accepted HPO -> disease associations | No inverse clinical-text resolver exists |
| MyDisease | annotated gene/HGNC context, then patient HPO comparison | No; `enrich_with_mydisease` is not a term resolver |
| MedGen disease/HPO | annotated gene, optionally accepted HPO context | No current phrase-to-concept entry point |
| MedGen phenotype-gene | gene + accepted HPO label | No |
| MedGen gene-disease | annotated gene | No |
| ClinGen/GenCC | annotated gene/variant context | No |
| CSpec | gene plus existing exact MONDO identifiers from annotation sources | No; context-only and not a clinical term resolver |

MyDisease and MedGen already expose useful normalized external disease context,
identifiers, HPO associations, and provenance after their existing gene/HPO
queries. They are disconnected from the Persian narrative and cannot be relabeled
as a safe disease resolver without a new, validated query/match contract. No
existing function safely resolves `سندروم والنبرگ` or its normalized equivalent.

## 7. Reviewer UI capability

The reviewer can edit/include locally validated HPO rows and explicitly accept
them. Manual HPO search remains available.

Non-HPO mentions are read-only informational strings. The UI cannot assign or
correct entity type, assertion, normalized concept, identifier, ambiguity, or
resolution state. It cannot accept/remove a disease entity as case context, and
no disease state survives refresh, persistence, or hydration. The later evidence
review UI shows provider-derived disease context, not the user's extracted disease
assertion.

## 8. Proposed minimal future contract

The extraction contract should be explicitly versioned and return one bounded
`clinical_entities` array. A minimal reviewed record is:

```text
ClinicalEntity
  original_text: non-empty sanitized verbatim Persian phrase
  normalized_text: deterministic Unicode/surface-normalized phrase
  entity_type: PHENOTYPE | DISEASE
  assertion: PRESENT | SUSPECTED | NEGATED | HISTORICAL
  source_span: {start, end} | null
  resolution: ClinicalEntityResolution | null

ClinicalEntityResolution
  status: RESOLVED | UNRESOLVED | AMBIGUOUS
  identifier: string | null
  preferred_label: string | null
  provider: string | null
  match_type: EXACT | SYNONYM | null
```

`DISEASE` includes explicitly named syndromes. `source_span` is optional only
when it can be validated against the sanitized text; phrase grounding remains
mandatory. Resolver fields must be populated by deterministic provider/local
validation, not trusted from the extraction LLM. Numeric extraction confidence
should not be added because the current architecture has no approved calibration
contract. If later required, it needs a bounded confidence-state definition and
separate acceptance tests.

The LLM should return explicit mention text, `entity_type`, and `assertion` only.
The backend should derive deterministic surface normalization and validate phrase
grounding. Ontology/provider identity belongs to a separate resolver step.

The existing accepted HPO structure should remain the canonical input to
Phen2Gene. A reviewed `PHENOTYPE/PRESENT` entity may produce an HPO resolution;
the HPO ID is then accepted through the existing local review path. Disease IDs
must never be stored in HPO fields.

## 9. Required future contract changes

| Area | Assessment | Required future action |
| --- | --- | --- |
| Extraction schema | versioning needed | Replace the category-specific `2.0` shape with a strict generalized entity schema; retain a bounded compatibility reader for historical `2.0` records only. |
| Prompt/task contract | versioning needed | Change the task from HPO-candidate extraction to explicit clinical-entity extraction while preserving the no-diagnosis rule. |
| PipelineResult / AnalysisContext | extension and versioning needed | Add reviewed `clinical_entities: list[...] | null`; use `null` for historical analyses where capture did not exist and `[]` for a new analysis with no accepted entities. Keep `accepted_hpo_terms`. |
| Persistence | extension and SQLite versioning needed | Add a nullable bounded `clinical_entities_json` projection beside accepted HPOs; keep the canonical pipeline snapshot; migrate old rows to `NULL`, never fabricated entities. |
| Recovery/hydration | extension and versioning needed | Carry only sanitized, reviewed bounded entities; safely recognize schema-3 checkpoints as historical/no-capture during the compatibility window. |
| Reviewer UI state | extension needed | Generalize the existing review flow into separate phenotype/finding and disease/syndrome sections with assertion and resolution state; retain manual HPO editing. |
| EvidenceObject | no extraction-stage change needed | Do not insert raw user disease assertions into EvidenceObject evidence nodes. Existing externally sourced disease/HPO and gene-disease nodes retain their semantics. Any later projection of resolved external knowledge requires its own provenance review. |

The nullable historical state is necessary: an empty list must not falsely claim
that an old phenotype-only analysis was checked and contained no disease entities.

## 10. Future routing and safety invariants

1. An explicit disease/syndrome is a `DISEASE`; it is never forced into HPO.
2. Symptom clusters cannot create a disease entity. `سرگیجه + آتاکسی` must not
   become Wallenberg syndrome unless a disease name/synonym is explicitly present.
3. Every extracted phrase must be grounded in the sanitized narrative.
4. LLM output does not establish ontology identity; deterministic validation does.
5. `NEGATED` entities never enter positive phenotype ranking or positive disease
   evidence.
6. `SUSPECTED` and `HISTORICAL` remain distinct and cannot be coerced to `PRESENT`.
7. Only reviewed `PHENOTYPE/PRESENT` HPO resolutions enter the existing observed
   phenotype and Phen2Gene route.
8. Disease-derived HPO context is labeled as derived external context and is not
   added to observed patient HPO terms or submitted to Phen2Gene as observed.
9. A user-entered disease is case context, not proof of disease, variant evidence,
   ACMG evidence, or a clinical conclusion.
10. External disease knowledge retains provider/upstream provenance and remains
    distinct from the user's assertion.
11. Unresolved and ambiguous entities remain visible and durable; no guessing or
    silent disappearance is allowed.
12. Historical records remain phenotype-only records and receive no fabricated
    disease entities during migration.

## 11. RED reproduction

`tests/test_stage6_1_clinical_entity_contract_audit.py` uses a deterministic fake
LLM adapter for the mixed blindness + Wallenberg sentence. The current extraction
parser successfully retains the disease phrase. The test then attempts to carry a
minimal reviewed disease entity into `validate_analysis_context`.

Observed RED result:

```text
1 failed
PipelineResultError: pipeline.analysis_context has invalid fields.
```

This failure isolates the missing durable contract without depending on live LLM
or provider behavior. No production fix was applied.

## 12. Files inspected

Authoritative and current documentation:

- `docs/PROJECT_DECLARATION.md`
- `docs/AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md`
- `docs/EVIDENCE_RESILIENCE_IMPLEMENTATION_ROADMAP.md`
- `docs/COMPLETE_CODEX_EXECUTION_ROADMAP.md` (Stage 6 section)
- `docs/Clinical_Variant_Active_Codex_Roadmap_After_Stage3.md` (Stage 6 section)
- `README.md` (clinical input, phenotype review, persistence, and recovery sections)

Production path and contracts:

- `backend/phenotype_llm.py`
- `backend/phenotype_selection.py`
- `backend/phenotype.py`
- `backend/privacy.py`
- `backend/llm.py`
- `backend/pipeline.py`
- `backend/mydisease.py`
- `backend/medgen.py`
- `backend/annotation.py`
- `backend/cspec_cache.py`
- `backend/report.py`
- `backend/variant_report.py`
- `backend/database.py`
- `frontend/ui.py`
- `frontend/execution.py`
- `frontend/evidence_review.py`

Tests inspected:

- `tests/test_defect01_phenotype_extraction.py`
- relevant phenotype, UI, persistence, recovery, and Stage 60 sections of
  `tests/test_pipeline.py`

## 13. Files changed

- `tests/test_stage6_1_clinical_entity_contract_audit.py` — intentional RED
  reproduction only.
- `docs/stage_6_1_clinical_entity_contract_audit.md` — this audit and design.

No application code, schema, migration, prompt, provider route, evidence logic,
interpretation logic, or UI behavior was changed.

## 14. Remaining risks and Stage 6.2 decisions

- The required live-run stabilization closeout document is missing.
- No approved current disease-term resolver entry point exists. Stage 6.2 must
  prove whether an existing MedGen/MyDisease capability can be wrapped safely or
  whether an architectural gap requires separate approval.
- Persian disease synonym resolution is not implemented; unsafe fuzzy matching
  must not be introduced.
- Source spans need Unicode-index and duplicate-phrase rules before becoming
  required.
- A retention/privacy decision is needed for sanitized entity phrases in durable
  analysis records and one-hour recovery checkpoints; the full raw narrative
  should remain excluded.
- Backward hydration must distinguish historical no-capture from a new captured
  empty entity set.
- Assertion-specific routing policy, especially historical phenotypes, requires
  deterministic tests before implementation.
- Current README Stage 47 prose describes only the original candidate-list shape
  and does not fully document schema `2.0` non-HPO mention categories.
