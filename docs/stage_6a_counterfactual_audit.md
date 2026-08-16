# Stage 6A counterfactual audit

This deterministic offline audit freezes fallback-only shadow candidates before introducing the approved VEP-success fixture comparator. It is observational only: no candidate is promoted, and percentages are intentionally omitted.

- Fixture source: `tests.test_pipeline.TestAnnotation._vep_response` (approved pre-Stage-6 deterministic fixture).
- Normalized identity: `GRCh38:1:100:A>G`.
- Composer state: `operational_failure` / `counterfactual`.
- Comparator order: fallback fixture -> shadow composer -> frozen candidate -> VEP fixture.

## Raw field-by-field results

| Case | Identity | Field | Shadow candidate | VEP comparator | Classification | Reason |
| --- | --- | --- | --- | --- | --- | --- |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | gene | GENE1 | GENE1 | MATCH | exact_value_match |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | transcript | ENST000001 | ENST000001 | MATCH | exact_value_match |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | hgvs_c | ENST000001:c.100A>G | ENST000001:c.100A>G | MATCH | exact_value_match |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | hgvs_p | ENSP000001:p.Lys34Arg | ENSP000001:p.Lys34Arg | MATCH | exact_value_match |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | consequence | missense_variant | missense_variant | MATCH | exact_value_match |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | impact | N/A | MODERATE | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | mane | N/A | NM_000001.2 | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_exact_genebe | GRCh38:1:100:A>G | canonical | N/A | True | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | gene | GENE1 | GENE1 | MATCH | exact_value_match |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | transcript | NM_000001.5 | ENST000001 | CONFLICT | no_preexisting_equivalence_rule |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | hgvs_c | NM_000001.5:c.100A>G | ENST000001:c.100A>G | CONFLICT | no_preexisting_equivalence_rule |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | hgvs_p | NP_000001.1:p.(Lys34Arg) | ENSP000001:p.Lys34Arg | CONFLICT | no_preexisting_equivalence_rule |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | consequence | N/A | missense_variant | CONFLICT | versioned_transcript_mismatch |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | impact | N/A | MODERATE | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | mane | N/A | NM_000001.2 | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_versioned_transcript_conflict | GRCh38:1:100:A>G | canonical | N/A | True | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | gene | N/A | GENE1 | UNAVAILABLE | shadow_candidate_unavailable |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | transcript | N/A | ENST000001 | UNAVAILABLE | shadow_candidate_unavailable |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | hgvs_c | N/A | ENST000001:c.100A>G | UNAVAILABLE | shadow_candidate_unavailable |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | hgvs_p | N/A | ENSP000001:p.Lys34Arg | UNAVAILABLE | shadow_candidate_unavailable |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | consequence | N/A | missense_variant | REJECTED_UNSAFE | transcript_bound_consequence_unavailable |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | impact | N/A | MODERATE | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | mane | N/A | NM_000001.2 | NOT_COMPOSED | stage6a_structural_block |
| approved_vep_success_raw_effect_only | GRCh38:1:100:A>G | canonical | N/A | True | NOT_COMPOSED | stage6a_structural_block |

## Raw counts

| Classification | Count | Denominator |
| --- | ---: | ---: |
| CONFLICT | 4 | 24 |
| MATCH | 6 | 24 |
| NOT_COMPOSED | 9 | 24 |
| REJECTED_UNSAFE | 1 | 24 |
| UNAVAILABLE | 4 | 24 |

`SEMANTICALLY_EQUIVALENT` is not used because Stage 6A defines no new equivalence rule. `impact`, `mane`, and `canonical` are structurally `NOT_COMPOSED` by policy.
