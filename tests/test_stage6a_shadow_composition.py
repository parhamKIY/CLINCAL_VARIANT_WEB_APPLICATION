"""Deterministic Stage 6A shadow-composition contract tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from backend.database import DATABASE_SCHEMA_VERSION
from backend.annotation import annotate_variants
from backend.evidence_readiness import build_evidence_readiness_audit
from backend.pipeline import PIPELINE_SCHEMA_VERSION
from backend.report import (
    EVIDENCE_SCHEMA_VERSION,
    build_evidence_object,
    sanitize_evidence_object,
    validate_evidence_object,
)
from backend.shadow_composition import (
    SHADOW_COMPOSITION_SCHEMA_VERSION,
    build_shadow_composition,
    compare_shadow_to_vep,
    compose_shadow_annotation,
    shadow_free_evidence_for_llm,
    validate_shadow_composition,
)


pytestmark = pytest.mark.stage59_testing_v3
from backend.variant_interpretation import _build_prompt
from tests.test_pipeline import (
    FakeResponse,
    FakeSession,
    TestAnnotation as AnnotationFactory,
    TestEvidenceObject as EvidenceFactory,
)
from tools.generate_stage6a_counterfactual_audit import render_audit


IDENTITY = {
    "assembly": "GRCh38",
    "chrom": "1",
    "pos": 100,
    "ref": "A",
    "alt": "G",
}
FIELDS = ("gene", "transcript", "hgvs_c", "hgvs_p", "consequence")


def _genebe_record(
    *,
    transcript: str = "NM_000001.2",
    terms: list[str] | None = None,
    hgvs_c: str | None = "NM_000001.2:c.100A>G",
    hgvs_p: str | None = "NP_000001.1:p.(Lys34Arg)",
) -> dict[str, object]:
    return {
        "gene": "GENE1",
        "transcript": transcript,
        "hgvs_c": hgvs_c,
        "hgvs_p": hgvs_p,
        "consequence_terms": terms or ["missense_variant"],
    }


def _fallback_evidence(
    *,
    genebe_identity: dict[str, object] | None = None,
    genebe_records: list[dict[str, object]] | None = None,
    variantvalidator: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "variantvalidator": variantvalidator or {},
        "genebe": {
            "identity": genebe_identity or dict(IDENTITY),
            "records": (
                genebe_records
                if genebe_records is not None
                else [_genebe_record()]
            ),
        },
        "myvariant": {},
    }


def _all_gaps() -> dict[str, bool]:
    return {field: True for field in FIELDS}


def _operational_shadow(
    **kwargs: object,
) -> dict[str, object]:
    return compose_shadow_annotation(
        identity_context=IDENTITY,
        vep_state="operational_failure",
        field_gaps=_all_gaps(),
        fallback_evidence=_fallback_evidence(**kwargs),
        evaluation_mode="counterfactual",
    )


def _outage_candidate() -> dict[str, object]:
    candidate = EvidenceFactory._pipeline_candidate()
    candidate["gene"] = None
    candidate["transcript"] = None
    candidate["consequence"] = None
    candidate["impact"] = None
    candidate["protein_change"] = None
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    sources["vep"] = {
        "status": "error",
        "provider": "Ensembl VEP",
        "primary_failure": "timeout",
    }
    sources["genebe"] = {
        "status": "success",
        "provider": "GeneBe",
        "request_assembly": "GRCh38",
        "returned_variant": {
            "chrom": "2",
            "pos": 166848215,
            "ref": "C",
            "alt": "T",
        },
        "consequences": [
            {
                "gene": "SCN1A",
                "transcript": "NM_001165963.4",
                "hgvs_c": "NM_001165963.4:c.3877G>A",
                "hgvs_p": "NP_001159435.1:p.(Val1293Ile)",
                "consequence_terms": ["missense_variant"],
            }
        ],
    }
    return candidate


def test_exact_genebe_transcript_record_composes_only_safe_fields() -> None:
    shadow = _operational_shadow()

    assert shadow["schema_version"] == SHADOW_COMPOSITION_SCHEMA_VERSION
    assert shadow["vep_state"] == "operational_failure"
    assert shadow["evaluation_mode"] == "counterfactual"
    for field in FIELDS:
        assert shadow["fields"][field]["decision"] == "COMPOSABLE"
        assert shadow["fields"][field]["source"] == "GeneBe"
        assert shadow["fields"][field]["identity_proof"]["decision"] == "EXACT"
    assert shadow["fields"]["impact"]["decision"] == "NOT_COMPOSED"
    assert shadow["fields"]["mane"]["decision"] == "NOT_COMPOSED"
    assert shadow["fields"]["canonical"]["decision"] == "NOT_COMPOSED"
    assert validate_shadow_composition(shadow) == shadow


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("assembly", "GRCh37"),
        ("chrom", "2"),
        ("pos", 101),
        ("ref", "C"),
        ("alt", "T"),
    ],
)
def test_genebe_field_is_rejected_without_exact_identity(
    field: str,
    value: object,
) -> None:
    returned = dict(IDENTITY)
    returned[field] = value
    shadow = _operational_shadow(genebe_identity=returned)

    for name in FIELDS:
        field_result = shadow["fields"][name]
        assert field_result["candidate"] is None
        assert field_result["decision"] == "REJECTED_UNSAFE"
        assert field_result["identity_proof"]["decision"] == "MISMATCH"


def test_provider_presence_without_identity_proof_is_rejected() -> None:
    fallback = _fallback_evidence()
    genebe = fallback["genebe"]
    assert isinstance(genebe, dict)
    genebe.pop("identity")
    shadow = compose_shadow_annotation(
        identity_context=IDENTITY,
        vep_state="operational_failure",
        field_gaps=_all_gaps(),
        fallback_evidence=fallback,
        evaluation_mode="counterfactual",
    )

    assert shadow["fields"]["gene"]["decision"] == "REJECTED_UNSAFE"
    assert "identity_proof_unavailable" in shadow["fields"]["gene"]["rejection_reasons"]


def test_variantvalidator_and_genebe_version_mismatch_is_retained_as_conflict() -> None:
    shadow = _operational_shadow(
        variantvalidator={
            "identity": dict(IDENTITY),
            "gene": "GENE1",
            "transcript": "NM_000001.5",
            "hgvs_c": "NM_000001.5:c.100A>G",
            "hgvs_p": "NP_000001.1:p.(Lys34Arg)",
        },
        genebe_records=[_genebe_record(transcript="NM_000001.6")],
    )

    assert shadow["fields"]["transcript"]["candidate"] == "NM_000001.5"
    assert shadow["fields"]["transcript"]["decision"] == "COMPOSABLE"
    assert shadow["fields"]["hgvs_c"]["source"] == "VariantValidator"
    assert shadow["fields"]["hgvs_p"]["source"] == "VariantValidator"
    consequence = shadow["fields"]["consequence"]
    assert consequence["decision"] == "CONFLICT"
    assert "versioned_transcript_mismatch" in consequence["rejection_reasons"]


@pytest.mark.parametrize(
    ("records", "expected_reason"),
    [
        ([], "transcript_bound_consequence_unavailable"),
        ([_genebe_record(terms=["unknown_effect"])], "unknown_consequence_term"),
        ([_genebe_record(terms=["missense_variant", "stop_gained"])], "ambiguous_consequence_term"),
    ],
)
def test_genebe_raw_or_ambiguous_effect_never_composes_consequence(
    records: list[dict[str, object]],
    expected_reason: str,
) -> None:
    shadow = _operational_shadow(genebe_records=records)
    consequence = shadow["fields"]["consequence"]

    assert consequence["candidate"] is None
    assert consequence["decision"] == "REJECTED_UNSAFE"
    assert expected_reason in consequence["rejection_reasons"]


def test_raw_genebe_effect_is_ignored_without_a_transcript_bound_record() -> None:
    fallback = _fallback_evidence(genebe_records=[])
    genebe = fallback["genebe"]
    assert isinstance(genebe, dict)
    genebe["effect"] = "missense_variant"

    shadow = compose_shadow_annotation(
        identity_context=IDENTITY,
        vep_state="operational_failure",
        field_gaps=_all_gaps(),
        fallback_evidence=fallback,
        evaluation_mode="counterfactual",
    )

    consequence = shadow["fields"]["consequence"]
    assert consequence["candidate"] is None
    assert consequence["decision"] == "REJECTED_UNSAFE"
    assert "transcript_bound_consequence_unavailable" in consequence["rejection_reasons"]


@pytest.mark.parametrize(
    ("status", "root_fields", "expected_state"),
    [
        ("not_found", {}, "valid_no_match"),
        ("success", {"gene": "GENE1"}, "partial"),
        ("success", {}, "insufficient"),
    ],
)
def test_vep_states_remain_distinct_and_field_aware(
    status: str,
    root_fields: dict[str, object],
    expected_state: str,
) -> None:
    candidate = _outage_candidate()
    candidate.update(root_fields)
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    sources["vep"] = {"status": status, "provider": "Ensembl VEP"}

    shadow = build_shadow_composition(candidate)

    assert shadow is not None
    assert shadow["vep_state"] == expected_state
    if expected_state == "partial":
        assert shadow["field_gaps"]["gene"] is False
        assert shadow["fields"]["gene"]["decision"] == "NOT_COMPOSED"
        assert shadow["field_gaps"]["consequence"] is True


def test_usable_vep_does_not_create_shadow_or_change_legacy_evidence() -> None:
    candidate = EvidenceFactory._pipeline_candidate()
    legacy = build_evidence_object(candidate)

    assert build_shadow_composition(candidate) is None
    assert "shadow_composition" not in legacy
    assert validate_evidence_object(legacy) == legacy


def test_normal_vep_success_never_calls_variantvalidator_for_shadow() -> None:
    session = FakeSession(
        [FakeResponse(200, [AnnotationFactory._vep_response()])]
    )

    annotation = annotate_variants(
        [AnnotationFactory._variant()],
        session=session,  # type: ignore[arg-type]
        max_retries=0,
    )[0]
    evidence = build_evidence_object(annotation)

    assert annotation["sources"]["vep"]["status"] == "success"
    assert session.variantvalidator_get_calls == []
    assert "shadow_composition" not in evidence


def test_outage_path_persists_observational_shadow_without_promoting_it() -> None:
    session = FakeSession(
        [FakeResponse(503, {"error": "unavailable"})],
        variantvalidator_responses=[
            FakeResponse(200, AnnotationFactory._variantvalidator_response())
        ],
        genebe_responses=[
            FakeResponse(
                200,
                {
                    "variants": [
                        {
                            "chr": "1",
                            "pos": 100,
                            "ref": "A",
                            "alt": "G",
                            "gene_symbol": "GENE1",
                            "transcript": "NM_000001.2",
                            "effect": "missense_variant",
                            "consequences": [
                                {
                                    "gene_symbol": "GENE1",
                                    "transcript": "NM_000001.2",
                                    "hgvs_c": "NM_000001.2:c.100A>G",
                                    "hgvs_p": "NP_000001.1:p.(Lys34Arg)",
                                    "consequences": ["missense_variant"],
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )
    annotation = annotate_variants(
        [AnnotationFactory._variant()],
        session=session,  # type: ignore[arg-type]
        max_retries=0,
    )[0]
    active_annotation = deepcopy(
        {
            field: annotation[field]
            for field in ("gene", "transcript", "hgvsc", "hgvsp", "consequence")
        }
    )

    evidence = build_evidence_object(annotation)
    shadow = evidence["shadow_composition"]

    assert annotation["sources"]["vep"]["provider"] == "VariantValidator"
    assert active_annotation == {
        "gene": evidence["gene"],
        "transcript": evidence["transcript"],
        "hgvsc": evidence["variant_context"]["hgvs_c"],
        "hgvsp": evidence["variant_context"]["hgvs_p"],
        "consequence": evidence["consequence"],
    }
    assert shadow["vep_state"] == "operational_failure"
    assert shadow["fields"]["consequence"]["candidate"] == "missense_variant"
    assert shadow["fields"]["consequence"]["source"] == "GeneBe"


def test_gap_driven_shadow_remains_additive_before_stage6b_promotion() -> None:
    candidate = _outage_candidate()
    before = deepcopy(candidate)

    evidence = build_evidence_object(candidate)

    assert candidate == before
    assert evidence["schema_version"] == "2.5"
    assert evidence["gene"] == "SCN1A"
    assert evidence["transcript"] == "NM_001165963.4"
    assert evidence["consequence"] is None
    shadow = evidence["shadow_composition"]
    assert shadow["evaluation_mode"] == "gap_driven"
    assert shadow["fields"]["gene"]["candidate"] == "SCN1A"
    assert shadow["fields"]["impact"]["decision"] == "NOT_COMPOSED"
    promotion = evidence["annotation_promotion"]
    assert promotion["fields"]["gene"]["source"] == "GeneBe"
    assert promotion["fields"]["transcript"]["source"] == "GeneBe"


def test_shadow_does_not_change_readiness_or_real_prompt_payload() -> None:
    candidate = _outage_candidate()
    evidence_with_shadow = build_evidence_object(candidate)
    evidence_without_shadow = deepcopy(evidence_with_shadow)
    evidence_without_shadow.pop("shadow_composition")
    evidence_without_shadow.pop("annotation_promotion")

    readiness_with = build_evidence_readiness_audit(
        evidence_with_shadow,
        variant_index=0,
    )
    readiness_without = build_evidence_readiness_audit(
        evidence_without_shadow,
        variant_index=0,
    )
    assert readiness_with == readiness_without
    projected_with = shadow_free_evidence_for_llm(evidence_with_shadow)
    projected_without = shadow_free_evidence_for_llm(evidence_without_shadow)
    assert projected_with == projected_without
    assert "human_review" not in projected_with
    assert "post_review" not in projected_with["conflict_audit"]
    assert _build_prompt(
        evidence_with_shadow,
        prompt_mode="standard",
        readiness_audit=readiness_with,
    ) == _build_prompt(
        evidence_without_shadow,
        prompt_mode="standard",
        readiness_audit=readiness_without,
    )


def test_shadow_is_sanitized_persistable_and_historical_evidence_stays_readable() -> None:
    evidence = build_evidence_object(_outage_candidate())
    sanitized = sanitize_evidence_object(evidence)
    historical = EvidenceFactory._complete_evidence_object()

    assert sanitized == evidence
    assert validate_evidence_object(historical) == historical
    assert EVIDENCE_SCHEMA_VERSION == "2.5"
    assert DATABASE_SCHEMA_VERSION == 4
    assert PIPELINE_SCHEMA_VERSION == "3.5"


def test_counterfactual_comparison_freezes_shadow_before_vep_comparator() -> None:
    shadow = _operational_shadow()
    comparison = compare_shadow_to_vep(
        shadow,
        {
            "gene": "GENE1",
            "transcript": "NM_000001.2",
            "hgvs_c": "NM_000001.2:c.100A>G",
            "hgvs_p": "NP_000001.1:p.(Lys34Arg)",
            "consequence": "missense_variant",
            "impact": "MODERATE",
            "mane": "NM_000001.2",
            "canonical": True,
        },
    )

    assert [row["classification"] for row in comparison[:5]] == ["MATCH"] * 5
    assert comparison[5]["classification"] == "NOT_COMPOSED"
    assert comparison[6]["classification"] == "NOT_COMPOSED"
    assert comparison[7]["classification"] == "NOT_COMPOSED"


def test_counterfactual_audit_artifact_is_current() -> None:
    artifact = Path("docs/stage_6a_counterfactual_audit.md")

    assert artifact.read_text(encoding="utf-8") == render_audit()
