"""Stage 8 runtime capability/disposition contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.evidence_coverage import build_evidence_coverage
from backend.evidence_readiness import (
    build_evidence_readiness_audit,
    validate_evidence_readiness_audit,
)
from backend.final_disposition import (
    build_final_disposition,
    build_final_dispositions,
)
from backend.report import build_evidence_object
from frontend.variant_status import build_variant_status_cards
from tests.test_stage6b_active_promotion import _fallback_candidate
from tests.test_pipeline import TestEvidenceObject as EvidenceFactory


pytestmark = pytest.mark.stage8_final_disposition


def _evidence() -> dict[str, object]:
    return deepcopy(EvidenceFactory._complete_evidence_object())


def _audit(evidence: dict[str, object], state: str) -> dict[str, object]:
    audit = dict(build_evidence_readiness_audit(evidence, variant_index=0))
    audit["readiness_after_rescue"] = state
    return validate_evidence_readiness_audit(audit)


def _capability(coverage: dict[str, object], target: str) -> dict[str, object]:
    capabilities = coverage["semantic_capabilities"]
    assert isinstance(capabilities, list)
    return next(
        item
        for item in capabilities
        if isinstance(item, dict) and item["semantic_target"] == target
    )


def _notice(disposition: dict[str, object], code: str) -> dict[str, object]:
    notices = disposition["reviewer_notices"]
    assert isinstance(notices, list)
    return next(
        item for item in notices if isinstance(item, dict) and item["code"] == code
    )


@pytest.mark.parametrize(
    ("internal", "expected"),
    [
        ("READY", "READY"),
        ("READY_WITH_LIMITATIONS", "READY_WITH_LIMITATIONS"),
        ("MINIMUM_IDENTITY_FAILURE", "BLOCKED"),
    ],
)
def test_persisted_internal_readiness_maps_to_runtime_disposition_only(
    internal: str,
    expected: str,
) -> None:
    evidence = _evidence()

    disposition = build_final_disposition(evidence, _audit(evidence, internal))

    assert disposition["internal_readiness"] == internal
    assert disposition["state"] == expected
    assert disposition["schema_version"] == "1.0"
    if expected == "BLOCKED":
        assert _notice(disposition, "MINIMUM_IDENTITY_FAILURE")["severity"] == "BLOCKING"


def test_rescue_required_is_reassessed_and_never_exposed_as_final() -> None:
    evidence = _evidence()

    disposition = build_final_disposition(
        evidence,
        _audit(evidence, "RESCUE_REQUIRED"),
    )

    assert disposition["state"] == "READY_WITH_LIMITATIONS"
    assert disposition["state"] != "RESCUE_REQUIRED"


def test_safe_sparse_annotation_is_limited_not_blocked() -> None:
    evidence = _evidence()
    evidence["consequence"] = None
    before = deepcopy(evidence)

    disposition = build_final_disposition(
        evidence,
        _audit(evidence, "READY_WITH_LIMITATIONS"),
    )

    assert disposition["state"] == "READY_WITH_LIMITATIONS"
    assert _notice(disposition, "CAPABILITY_DEGRADED")["semantic_target"] == "annotation"
    assert evidence == before


def test_valid_no_match_and_operational_failure_have_different_reviewer_meanings() -> None:
    no_match = _evidence()
    no_match["pathogenicity"] = dict(no_match["pathogenicity"])
    no_match["pathogenicity"]["expert_curated_variant_context"] = {
        "status": "no_match",
        "records": [],
    }
    failed = deepcopy(no_match)
    failed["pathogenicity"]["expert_curated_variant_context"] = {
        "status": "timeout",
        "records": [],
    }

    no_match_disposition = build_final_disposition(
        no_match, _audit(no_match, "READY_WITH_LIMITATIONS")
    )
    failed_disposition = build_final_disposition(
        failed, _audit(failed, "READY_WITH_LIMITATIONS")
    )

    assert _notice(no_match_disposition, "VALID_NO_MATCH")["severity"] == "INFO"
    assert "ERepo assertion" in _notice(no_match_disposition, "VALID_NO_MATCH")["message"]
    assert _notice(failed_disposition, "CAPABILITY_RETRIEVAL_UNAVAILABLE")["severity"] == "PARTIAL"


def test_medgen_support_never_becomes_gene_disease_validity() -> None:
    evidence = _evidence()
    pathogenicity = evidence["pathogenicity"]
    assert isinstance(pathogenicity, dict)
    pathogenicity["clingen_context"] = []
    pathogenicity["medgen_gene_disease_context"] = {
        "status": "success",
        "retrieval_state": "accepted_records",
        "records": [{"title": "Supporting context"}],
        "provider": "NCBI MedGen",
    }

    disposition = build_final_disposition(
        evidence, _audit(evidence, "READY_WITH_LIMITATIONS")
    )

    assert disposition["capability_states"]["gene_disease_validity"] == "UNAVAILABLE"
    assert disposition["capability_states"]["gene_disease_support"] == "FULL"
    assert _notice(disposition, "GENE_DISEASE_VALIDITY_UNAVAILABLE")["message"] == (
        "Gene-disease validity classification is unavailable; MedGen provides "
        "supporting gene-disease context only."
    )


def test_phenotype_support_never_fabricates_a_ranking_or_hpo_requirement() -> None:
    evidence = _evidence()
    phenotype = evidence["phenotype_relationship"]
    assert isinstance(phenotype, dict)
    phenotype.update(
        {
            "patient_hpo_terms": ["HP:0001250"],
            "phen2gene": {},
            "matched_patient_hpo_terms": ["HP:0001250"],
            "local_phenotype_score": 0.5,
        }
    )
    disposition = build_final_disposition(
        evidence, _audit(evidence, "READY_WITH_LIMITATIONS")
    )
    assert disposition["capability_states"]["phenotype_gene_ranking"] == "UNAVAILABLE"
    assert disposition["capability_states"]["phenotype_gene_support"] == "FULL"
    assert "rank/score" in _notice(
        disposition, "PHENOTYPE_GENE_RANKING_UNAVAILABLE"
    )["message"]

    phenotype["patient_hpo_terms"] = []
    no_hpo = build_final_disposition(
        evidence, _audit(evidence, "READY_WITH_LIMITATIONS")
    )
    assert no_hpo["capability_states"]["phenotype_gene_ranking"] == "NOT_APPLICABLE"
    assert _notice(no_hpo, "CAPABILITY_NOT_APPLICABLE")["severity"] == "INFO"


def test_no_verified_gene_association_and_correlation_remain_reviewer_context() -> None:
    evidence = _evidence()
    pathogenicity = evidence["pathogenicity"]
    provenance = evidence["provenance"]
    assert isinstance(pathogenicity, dict)
    assert isinstance(provenance, dict)
    pathogenicity["medgen_gene_disease_context"] = {
        "status": "no_match",
        "retrieval_state": "no_verified_gene_association",
        "records": [],
        "candidate_diagnostics": [{"rejection_reason": "gene_mismatch"}],
    }
    provenance["shared_upstream_groups"] = [
        {
            "upstream_source": "ClinVar",
            "evidence_paths": ["clinvar_accession"],
        }
    ]

    disposition = build_final_disposition(
        evidence, _audit(evidence, "READY_WITH_LIMITATIONS")
    )

    assert _notice(disposition, "NO_VERIFIED_GENE_ASSOCIATION")["severity"] == "INFO"
    assert _notice(disposition, "CORRELATED_UPSTREAM_EVIDENCE")["correlation_groups"] == [
        "ClinVar"
    ]


def test_composed_annotation_is_active_coverage_but_missing_consequence_is_visible() -> None:
    evidence = _evidence()
    evidence["consequence"] = None
    evidence["annotation_promotion"] = {
        "vep_state": "operational_failure",
        "fields": {
            "gene": {
                "source": "GeneBe",
                "composition_state": "composed",
                "promotion_state": "PROMOTED",
                "limitations": ["vep_operational_failure"],
            }
        },
    }
    coverage = build_evidence_coverage(evidence)
    annotation = _capability(coverage, "annotation")

    disposition = build_final_disposition(
        evidence,
        _audit(evidence, "READY_WITH_LIMITATIONS"),
        coverage=coverage,
    )

    assert annotation["state"] == "DEGRADED"
    assert annotation["composition_state"] == "mixed"
    assert disposition["state"] == "READY_WITH_LIMITATIONS"
    assert _notice(disposition, "COMPOSED_ANNOTATION")["severity"] == "INFO"


def test_runtime_projection_preserves_input_order_and_cards_prioritize_capability() -> None:
    first = _evidence()
    second = _evidence()
    second["variant"] = {"chrom": "7", "pos": 140453136, "ref": "A", "alt": "T"}
    first_audit = _audit(first, "READY_WITH_LIMITATIONS")
    second_audit = _audit(second, "READY_WITH_LIMITATIONS")
    second_audit = dict(second_audit)
    second_audit["variant_index"] = 1
    second_audit = validate_evidence_readiness_audit(second_audit)
    before_first = deepcopy(first)
    before_second = deepcopy(second)

    dispositions = build_final_dispositions([first, second], [first_audit, second_audit])
    cards = build_variant_status_cards(
        {
            "variant_count": 2,
            "evidence_objects": [first, second],
            "evidence_readiness": [first_audit, second_audit],
            "draft_variant_reports": [],
        }
    )

    assert [item["variant_index"] for item in dispositions] == [0, 1]
    assert [card["final_disposition"] for card in cards] == [
        "READY_WITH_LIMITATIONS",
        "READY_WITH_LIMITATIONS",
    ]
    assert cards[0]["capability_summary"].startswith("Evidence capability:")
    assert first == before_first
    assert second == before_second


def test_bounded_non_phi_end_to_end_stage6_to_reviewer_projection() -> None:
    """Public synthetic candidate: promotion, coverage, final capability, UI model."""

    evidence = build_evidence_object(_fallback_candidate())
    before = build_evidence_readiness_audit(evidence, variant_index=0)
    after = build_evidence_readiness_audit(
        evidence,
        variant_index=0,
        before_rescue=before,
    )
    coverage = build_evidence_coverage(evidence)
    disposition = build_final_disposition(evidence, after, coverage=coverage)
    cards = build_variant_status_cards(
        {
            "variant_count": 1,
            "evidence_objects": [evidence],
            "evidence_readiness": [after],
            "draft_variant_reports": [],
        }
    )

    assert coverage["semantic_capabilities"][0]["composition_state"] == "composed"
    assert disposition["state"] == "READY_WITH_LIMITATIONS"
    assert cards[0]["final_disposition"] == "READY_WITH_LIMITATIONS"
    assert any(
        notice["message"].startswith("Annotation uses safely promoted")
        for notice in cards[0]["capability_notices"]
    )
