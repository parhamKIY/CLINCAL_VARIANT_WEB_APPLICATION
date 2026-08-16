"""Deterministic Stage 6B controlled active-promotion contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.report import build_evidence_object
from backend.shadow_composition import shadow_free_evidence_for_llm
from tests.test_pipeline import TestEvidenceObject as EvidenceFactory


IDENTITY = {
    "assembly": "GRCh38",
    "chrom": "2",
    "pos": 166848215,
    "ref": "C",
    "alt": "T",
}
TRANSCRIPT = "NM_000001.5"
HGVS_C = "NM_000001.5:c.100A>G"
HGVS_P = "NP_000001.1:p.(Lys34Arg)"


pytestmark = pytest.mark.stage59_testing_v3


def _genebe_source(
    *,
    transcript: str = TRANSCRIPT,
    hgvs_c: str | None = HGVS_C,
    hgvs_p: str | None = HGVS_P,
    returned_identity: dict[str, object] | None = None,
) -> dict[str, object]:
    identity = dict(IDENTITY)
    if returned_identity is not None:
        identity.update(returned_identity)
    return {
        "status": "success",
        "provider": "GeneBe",
        "request_assembly": "GRCh38",
        "returned_variant": {
            key: identity[key]
            for key in ("chrom", "pos", "ref", "alt")
        },
        "gene": "GENE1",
        "transcript": transcript,
        "effect": "missense_variant",
        "consequences": [
            {
                "gene": "GENE1",
                "transcript": transcript,
                "hgvs_c": hgvs_c,
                "hgvs_p": hgvs_p,
                "consequence_terms": ["missense_variant"],
            }
        ],
    }


def _fallback_candidate(
    *,
    transcript: str = TRANSCRIPT,
    hgvs_c: str | None = HGVS_C,
    hgvs_p: str | None = HGVS_P,
    genebe: dict[str, object] | None = None,
) -> dict[str, object]:
    candidate = EvidenceFactory._pipeline_candidate()
    candidate.update(
        {
            "gene": "GENE1",
            "gene_id": "HGNC:1",
            "transcript": transcript,
            "hgvsc": hgvs_c,
            "hgvsp": hgvs_p,
            "protein_change": hgvs_p,
            "consequence": None,
            "impact": None,
        }
    )
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    vep = sources["vep"]
    assert isinstance(vep, dict)
    vep.update(
        {
            "status": "partial",
            "provider": "VariantValidator",
            "provider_role": "fallback",
            "fallback_used": True,
            "fallback_for": "ensembl_vep",
            "primary_provider": "Ensembl VEP",
            "primary_failure": "timeout",
            "fallback_status": "success",
            "source_type": "validation_mapping_fallback",
            "normalized_variant": dict(IDENTITY),
            "validated_gene": "GENE1",
            "validated_gene_id": "HGNC:1",
            "validated_transcript": transcript,
            "validated_transcript_hgvs": hgvs_c,
            "validated_protein_hgvs": hgvs_p,
            "selected_record": hgvs_c or "selected-record",
        }
    )
    sources["genebe"] = genebe or _genebe_source()
    return candidate


def _genebe_only_candidate(
    *,
    vep_status: str = "error",
    genebe: dict[str, object] | None = None,
) -> dict[str, object]:
    candidate = EvidenceFactory._pipeline_candidate()
    candidate.update(
        {
            "gene": None,
            "gene_id": None,
            "transcript": None,
            "hgvsc": None,
            "hgvsp": None,
            "protein_change": None,
            "consequence": None,
            "impact": None,
        }
    )
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    sources["vep"] = {
        "status": vep_status,
        "provider": "Ensembl VEP",
        **({"primary_failure": "timeout"} if vep_status == "error" else {}),
    }
    sources["genebe"] = genebe or _genebe_source()
    return candidate


def _promotion_fields(evidence: dict[str, object]) -> dict[str, object]:
    promotion = evidence["annotation_promotion"]
    assert isinstance(promotion, dict)
    fields = promotion["fields"]
    assert isinstance(fields, dict)
    return fields


def test_safe_versioned_bundle_is_actively_promoted_with_actual_provenance() -> None:
    evidence = build_evidence_object(_fallback_candidate())
    fields = _promotion_fields(evidence)

    assert evidence["gene"] == "GENE1"
    assert evidence["transcript"] == TRANSCRIPT
    assert evidence["variant_context"]["hgvs_c"] == HGVS_C
    assert evidence["variant_context"]["hgvs_p"] == HGVS_P
    assert evidence["protein_change"] == HGVS_P
    assert set(fields) == {"gene", "transcript", "hgvs_c", "hgvs_p"}
    for field in fields.values():
        assert field["source"] == "VariantValidator"
        assert field["promotion_state"] == "PROMOTED"
        assert field["identity_proof"]["decision"] == "EXACT"
    assert fields["transcript"]["transcript_context"] == TRANSCRIPT
    assert fields["hgvs_c"]["transcript_context"] == TRANSCRIPT
    assert fields["hgvs_p"]["transcript_context"] == TRANSCRIPT


def test_gene_only_promotion_does_not_activate_an_incomplete_hgvs_bundle() -> None:
    evidence = build_evidence_object(
        _fallback_candidate(hgvs_c=None, hgvs_p=None)
    )

    assert evidence["gene"] == "GENE1"
    assert evidence["transcript"] is None
    assert evidence["variant_context"]["hgvs_c"] is None
    assert evidence["variant_context"]["hgvs_p"] is None
    assert set(_promotion_fields(evidence)) == {"gene"}


def test_exact_identity_mismatch_blocks_gene_and_bundle_promotion() -> None:
    candidate = _genebe_only_candidate(
        genebe=_genebe_source(returned_identity={"pos": 166848216})
    )
    evidence = build_evidence_object(candidate)

    assert evidence["gene"] is None
    assert evidence["transcript"] is None
    assert "annotation_promotion" not in evidence
    shadow = evidence["shadow_composition"]
    assert shadow["fields"]["gene"]["decision"] == "REJECTED_UNSAFE"


def test_conflicting_gene_assertion_is_not_selected_or_attributed_to_vep() -> None:
    genebe = _genebe_source()
    genebe["gene"] = "GENE2"
    evidence = build_evidence_object(_genebe_only_candidate(genebe=genebe))
    fields = _promotion_fields(evidence)

    assert evidence["gene"] is None
    assert "gene" not in fields
    assert evidence["annotations"]["vep"]["provider"] == "Ensembl VEP"
    assert evidence["annotations"]["vep"]["gene"] is None
    assert fields["transcript"]["source"] == "GeneBe"


def test_usable_vep_gene_is_never_overwritten_or_relabelled() -> None:
    candidate = EvidenceFactory._pipeline_candidate()
    before = deepcopy(candidate)

    evidence = build_evidence_object(candidate)

    assert candidate == before
    assert evidence["gene"] == candidate["gene"]
    assert "annotation_promotion" not in evidence
    assert "shadow_composition" not in evidence


def test_unversioned_transcript_cannot_activate_transcript_or_hgvs() -> None:
    transcript = "NM_000001"
    evidence = build_evidence_object(
        _fallback_candidate(
            transcript=transcript,
            hgvs_c=f"{transcript}:c.100A>G",
        )
    )

    assert evidence["gene"] == "GENE1"
    assert evidence["transcript"] is None
    assert evidence["variant_context"]["hgvs_c"] is None
    assert evidence["variant_context"]["hgvs_p"] is None
    assert set(_promotion_fields(evidence)) == {"gene"}


def test_version_and_accession_mismatch_block_the_versioned_bundle() -> None:
    for hgvs_c in ("NM_000001.4:c.100A>G", "NM_000002.5:c.100A>G"):
        evidence = build_evidence_object(_fallback_candidate(hgvs_c=hgvs_c))

        assert evidence["gene"] == "GENE1"
        assert evidence["transcript"] is None
        assert evidence["variant_context"]["hgvs_c"] is None
        assert evidence["variant_context"]["hgvs_p"] is None
        assert set(_promotion_fields(evidence)) == {"gene"}


def test_cross_transcript_provider_conflict_blocks_the_hgvs_bundle() -> None:
    evidence = build_evidence_object(
        _fallback_candidate(
            genebe=_genebe_source(transcript="NM_000001.6")
        )
    )
    fields = _promotion_fields(evidence)

    assert evidence["gene"] == "GENE1"
    assert evidence["transcript"] is None
    assert evidence["variant_context"]["hgvs_c"] is None
    assert evidence["variant_context"]["hgvs_p"] is None
    assert set(fields) == {"gene"}
    assert evidence["shadow_composition"]["fields"]["consequence"]["decision"] == "CONFLICT"


def test_consequence_and_structural_fields_remain_unpromoted() -> None:
    evidence = build_evidence_object(_genebe_only_candidate())

    assert evidence["consequence"] is None
    assert evidence["impact"] is None
    assert set(_promotion_fields(evidence)) == {
        "gene", "transcript", "hgvs_c", "hgvs_p"
    }
    assert _promotion_fields(evidence)["gene"]["source"] == "GeneBe"
    shadow = evidence["shadow_composition"]
    assert shadow["fields"]["consequence"]["decision"] == "COMPOSABLE"
    assert shadow["fields"]["impact"]["decision"] == "NOT_COMPOSED"
    assert shadow["fields"]["mane"]["decision"] == "NOT_COMPOSED"
    assert shadow["fields"]["canonical"]["decision"] == "NOT_COMPOSED"


def test_valid_no_match_and_partial_remain_distinct_field_level_states() -> None:
    no_match = build_evidence_object(
        _genebe_only_candidate(vep_status="not_found")
    )
    partial_candidate = _genebe_only_candidate(vep_status="success")
    partial_candidate["consequence"] = "missense_variant"
    partial = build_evidence_object(partial_candidate)

    assert no_match["shadow_composition"]["vep_state"] == "valid_no_match"
    assert partial["shadow_composition"]["vep_state"] == "partial"
    assert no_match["gene"] == "GENE1"
    assert partial["gene"] == "GENE1"
    assert no_match["source_statuses"]["vep"] == "not_found"
    assert partial["source_statuses"]["vep"] == "success"


def test_shadow_stays_out_of_the_llm_projection_while_promoted_fields_remain() -> None:
    evidence = build_evidence_object(_genebe_only_candidate())
    llm_evidence = shadow_free_evidence_for_llm(evidence)

    assert "shadow_composition" not in llm_evidence
    assert "annotation_promotion" not in llm_evidence
    assert llm_evidence["gene"] == "GENE1"
    assert llm_evidence["transcript"] == TRANSCRIPT
    assert llm_evidence["consequence"] is None
