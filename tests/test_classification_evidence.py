"""Defect 07 classification-evidence recovery and state tests."""

from __future__ import annotations

from backend.classification_evidence import (
    build_classification_evidence_audit,
    validate_classification_evidence_audit,
)


def _evidence(
    *,
    clinvar: str | None = None,
    genebe: str | None = None,
    myvariant_derived: str | None = None,
    clinvar_status: str = "no_match",
    genebe_status: str = "success",
    rescue: dict[str, object] | None = None,
) -> dict[str, object]:
    population: dict[str, object] = {
        "status": "success",
        "variant_id": "chr2:g.166848215C>T",
        "rsid": "rs123",
    }
    if myvariant_derived is not None:
        population["clinvar_derived"] = {
            "status": "available",
            "clinical_significance": myvariant_derived,
            "review_status": "criteria provided, single submitter",
        }
    clinvar_provenance: dict[str, object] = {
        "retrieval_assessment": {
            "schema_version": "1.0",
            "provider": "ncbi_clinvar",
            "status": clinvar_status,
            "cause": "TRUE_SOURCE_ABSENCE",
            "query_strategy": "deterministic_identifier_hierarchy",
            "identifiers_used": [
                {"type": "genomic_hgvs", "value": "NC_000002.12:g.166848215C>T"}
            ],
            "unused_eligible_identifiers": [],
            "candidates_returned": 2,
            "candidates_rejected": 2,
            "live_verification_required": False,
        }
    }
    if rescue is not None:
        clinvar_provenance["evidence_rescue"] = rescue
    return {
        "assembly": "GRCh38",
        "variant": {"chrom": "2", "pos": 166848215, "ref": "C", "alt": "T"},
        "pathogenicity": {
            "clinvar_classification": clinvar,
            "clinvar_review_status": "reviewed by expert panel" if clinvar else None,
            "automated_acmg_classification": genebe,
        },
        "annotations": {
            "genebe": {"status": genebe_status},
            "population": population,
        },
        "capability_results": {
            "clinvar_evidence": {
                "status": clinvar_status,
                "provider": "ncbi_clinvar",
                "provider_role": "primary",
                "method": "direct_clinvar_lookup",
                "provenance": clinvar_provenance,
            },
            "variant_context": {
                "status": "success",
                "provider": "myvariant",
                "provider_role": "primary",
                "method": "aggregated_variant_lookup",
                "provenance": {},
            },
        },
    }


def _rescue(*, recovered: bool, stop_reason: str) -> dict[str, object]:
    attempt = {
        "sequence": 1,
        "provider": "myvariant",
        "method": "reuse_exact_myvariant_clinvar_derivation",
        "identifier_type": "myvariant_variant_id",
        "identifier_value": "chr2:g.166848215C>T",
        "status": "success" if recovered else "no_match",
        "evidence_recovered": recovered,
        "evidence_path": (
            "annotations.population.clinvar_derived" if recovered else None
        ),
        "independent_evidence": False,
    }
    return {
        "schema_version": "1.0",
        "capability": "clinvar_evidence",
        "trigger": "primary_no_match",
        "primary_provider": "ncbi_clinvar",
        "primary_status": "no_match",
        "eligible": True,
        "attempted": True,
        "alternate_identifiers": [
            {"type": "myvariant_variant_id", "value": "chr2:g.166848215C>T"}
        ],
        "attempts": [attempt],
        "recovered": recovered,
        "recovered_provider": "myvariant" if recovered else None,
        "stop_reason": stop_reason,
    }


def test_genebe_only_is_secondary_classification_evidence() -> None:
    audit = build_classification_evidence_audit(
        _evidence(genebe="Uncertain significance")
    )

    assert audit["state"] == "SECONDARY_CLASSIFICATION_EVIDENCE_AVAILABLE"
    assert audit["retrieval_exhausted"] is True
    assert audit["classifications"] == [
        {
            "source": "GeneBe",
            "evidence_role": "automated",
            "classification": "Uncertain significance",
            "normalized_classification": "VUS",
            "review_status": "Automated annotation only",
            "independent_evidence": True,
        }
    ]


def test_myvariant_clinvar_rescue_is_retained_as_non_independent_evidence() -> None:
    audit = build_classification_evidence_audit(
        _evidence(
            myvariant_derived="Pathogenic",
            rescue=_rescue(recovered=True, stop_reason="evidence_recovered"),
        )
    )

    assert audit["state"] == "SECONDARY_CLASSIFICATION_EVIDENCE_AVAILABLE"
    assert audit["rescue_attempted"] is True
    assert audit["rescue_outcome"] == "evidence_recovered"
    assert audit["classifications"][0]["source"] == "MyVariant.info"
    assert audit["classifications"][0]["independent_evidence"] is False


def test_direct_and_automated_disagreement_remains_an_explicit_conflict() -> None:
    audit = build_classification_evidence_audit(
        _evidence(clinvar="VUS", genebe="Likely Pathogenic", clinvar_status="success")
    )

    assert audit["state"] == "CONFLICTING_CLASSIFICATIONS"
    assert [item["normalized_classification"] for item in audit["classifications"]] == [
        "VUS",
        "Likely Pathogenic",
    ]
    assert audit["independent_acmg_adjudication"] is False


def test_clinvar_conflicting_significance_is_not_discarded() -> None:
    audit = build_classification_evidence_audit(
        _evidence(
            clinvar="Conflicting classifications of pathogenicity",
            clinvar_status="success",
        )
    )

    assert audit["state"] == "CONFLICTING_CLASSIFICATIONS"
    assert audit["classifications"][0]["normalized_classification"] == (
        "Conflicting"
    )


def test_unavailable_requires_exhausted_queries_and_rescue() -> None:
    audit = build_classification_evidence_audit(
        _evidence(
            genebe_status="not_found",
            rescue=_rescue(recovered=False, stop_reason="no_secondary_evidence"),
        )
    )

    assert audit["state"] == "NO_CLASSIFICATION_AFTER_RESCUE"
    assert audit["retrieval_exhausted"] is True
    assert audit["sources_queried"] == ["NCBI ClinVar", "GeneBe", "MyVariant.info"]
    assert "genomic_hgvs: NC_000002.12:g.166848215C>T" in audit["identifiers_used"]
    assert audit["candidates_rejected"] == 2
    assert audit["unavailable_reasons"]
    assert validate_classification_evidence_audit(audit) == audit


def test_operational_failure_is_not_misreported_as_exhausted_unavailability() -> None:
    evidence = _evidence(genebe_status="unavailable", clinvar_status="unavailable")
    capability = evidence["capability_results"]["clinvar_evidence"]
    capability["provenance"]["retrieval_assessment"].update(
        {
            "status": "unavailable",
            "cause": "OPERATIONAL_FAILURE",
            "live_verification_required": False,
        }
    )

    audit = build_classification_evidence_audit(evidence)

    assert audit["state"] == "NOT_ASSESSED"
    assert audit["retrieval_exhausted"] is False
