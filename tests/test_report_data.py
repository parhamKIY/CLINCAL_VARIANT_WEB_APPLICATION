"""Stage 81 ReportData V4 schema and missingness acceptance tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.references import canonicalize_reference
from backend.report_data import (
    REPORT_DATA_SCHEMA_VERSION,
    ReportDataError,
    validate_report_data,
)


def _report_data(*, sparse: bool = False) -> dict[str, object]:
    interpretation = None if sparse else "Evidence-grounded interpretation."
    return {
        "schema_version": REPORT_DATA_SCHEMA_VERSION,
        "report_id": "report-001",
        "analysis_id": "analysis-001",
        "input_index": 0,
        "variant_identity": {
            "genome_build": "GRCh38",
            "chromosome": "2",
            "position": 100001,
            "reference": "C",
            "alternate": "T",
            "gene": None if sparse else "GENE1",
            "gene_id": None if sparse else "HGNC:1",
            "transcript": None if sparse else "NM_000001.1",
            "hgvs_c": None if sparse else "c.101C>T",
            "hgvs_p": None if sparse else "p.(Arg34Trp)",
            "zygosity": None,
        },
        "phenotype_summary": {
            "accepted_hpo_terms": [
                {"hpo_id": "HP:0000707", "label": "Neurologic abnormality"}
            ],
            "matched_hpo_terms": [] if sparse else [
                {"hpo_id": "HP:0000707", "label": "Neurologic abnormality"}
            ],
            "concordance": (
                "no_supported_association" if sparse else "supported"
            ),
            "score": None if sparse else 0.75,
            "summary": (
                "No supported phenotype association was identified."
                if sparse
                else "The accepted phenotype has supporting evidence."
            ),
            "evidence_status": "no_match" if sparse else "available",
        },
        "conclusive_result": {
            "gene": None if sparse else "GENE1",
            "hgvs_c": None if sparse else "c.101C>T",
            "hgvs_p": None if sparse else "p.(Arg34Trp)",
            "zygosity": None,
            "classification": None,
            "classification_source": None,
            "status": "not_assessed",
        },
        "call_quality": {
            "qual": None if sparse else 50.0,
            "filter": None if sparse else "PASS",
            "status": "not_evaluated" if sparse else "passed",
            "acknowledged_at": None,
            "override_reason": None,
            "override_timestamp": None,
        },
        "main_findings": {
            "population_frequencies": [
                {
                    "source": "gnomAD",
                    "dataset": "gnomAD r4",
                    "frequency": None if sparse else 0.0001,
                    "status": "no_match" if sparse else "available",
                    "provider_role": "primary",
                    "fallback_used": False,
                }
            ],
            "disease_associations": [] if sparse else [
                {
                    "source": "ClinGen / GenCC",
                    "disease_name": "Synthetic disease context",
                    "disease_id": "MONDO:0000001",
                    "inheritance": "autosomal dominant",
                    "relationship_scope": "gene_disease_context",
                    "status": "available",
                }
            ],
            "computational_evidence": [] if sparse else [
                {
                    "source": "Ensembl VEP",
                    "tool": "SIFT",
                    "prediction": "deleterious",
                    "score": 0.01,
                    "status": "available",
                }
            ],
            "stable_variant_ids": [
                {
                    "source": "dbSNP",
                    "identifier_type": "rsID",
                    "identifier": None if sparse else "rs123",
                    "status": "no_match" if sparse else "available",
                }
            ],
            "classifications": [
                {
                    "source": "NCBI ClinVar",
                    "classification": (
                        None if sparse else "uncertain significance"
                    ),
                    "review_status": None if sparse else "single submitter",
                    "criteria": [],
                    "independent_evidence": True,
                    "status": "no_match" if sparse else "available",
                }
            ],
        },
        "interpretation": {
            "original_model_interpretation": interpretation,
            "current_reviewer_interpretation": interpretation,
            "model": "model-1",
            "prompt_version": "1.1",
            "generated_at": "2026-08-11T00:00:00+00:00",
            "edit_history": [],
            "interpretation_status": (
                "unavailable" if sparse else "available"
            ),
            "failure_type": "request_timeout" if sparse else None,
            "conflict_assessment": None if sparse else "No conflict detected.",
        },
        "classification_summary": {
            "reviewer_confirmed_classification": None,
            "clinvar_classification": (
                None if sparse else "uncertain significance"
            ),
            "automated_classification": None,
            "conflict_status": "unavailable" if sparse else "none",
            "conflict_severity": None,
            "source_attributions": [] if sparse else ["NCBI ClinVar"],
            "independent_acmg_adjudication": False,
            "summary": None,
        },
        "preliminary_classification": {
            "status": "unavailable" if sparse else "classified",
            "classification": None if sparse else "Likely pathogenic",
            "rationale": (
                "No preliminary classification was generated."
                if sparse
                else "The retained evidence supports the preliminary label."
            ),
            "limitations": [] if sparse else ["Qualified human review is required."],
            "review_required": True,
        },
        "literature_references": [] if sparse else [
            canonicalize_reference(
                source="PubMed",
                identifier="25741868",
                title="Standards and guidelines",
            )
        ],
        "data_sources": [
            {
                "source": "gnomAD",
                "capability": "population_frequency",
                "status": "no_match" if sparse else "available",
                "operational_status": "no_match" if sparse else "success",
                "provider_role": "primary",
                "method": "exact_allele_query",
                "record_identifier": None,
                "dataset": "gnomAD r4",
                "human_url": None,
                "link_status": "unavailable",
                "fallback_used": False,
                "primary_failure": None,
                "retrieved_at": "2026-08-11T00:00:00+00:00",
            }
        ],
        "warnings": [
            {
                "severity": "INFO" if sparse else "PARTIAL",
                "code": (
                    "phenotype_non_concordance" if sparse else "source_partial"
                ),
                "message": (
                    "No supported phenotype association was identified."
                    if sparse
                    else "One optional source was unavailable."
                ),
                "capability": "phenotype_gene" if sparse else "literature",
            }
        ],
        "provenance": {
            "source_report_schema_version": "2.1",
            "evidence_schema_version": "2.5",
            "interpretation_schema_version": "1.1",
            "providers": ["gnomAD"],
            "upstream_sources": ["gnomAD"],
            "generated_at": "2026-08-11T00:00:00+00:00",
        },
        "review_state": {
            "review_status": "draft",
            "include_in_final_report": True,
            "confirmed_at": None,
            "reviewer_summary": None,
            "reviewer_notes": [],
            "selection_history": [],
        },
        "template_version": "professor-report-v1",
    }


@pytest.mark.stage81_report_data
def test_rich_report_data_is_valid_and_copied() -> None:
    original = _report_data()

    validated = validate_report_data(original)

    assert validated == original
    assert validated is not original
    assert validated["main_findings"] is not original["main_findings"]


@pytest.mark.stage81_report_data
def test_legitimate_missingness_and_non_concordance_are_valid() -> None:
    validated = validate_report_data(_report_data(sparse=True))

    assert validated["variant_identity"]["gene"] is None
    assert validated["variant_identity"]["zygosity"] is None
    assert validated["phenotype_summary"]["concordance"] == (
        "no_supported_association"
    )
    assert validated["interpretation"]["interpretation_status"] == (
        "unavailable"
    )
    assert validated["main_findings"]["population_frequencies"][0][
        "status"
    ] == "no_match"


@pytest.mark.stage81_report_data
def test_main_findings_reject_preformatted_or_unknown_fields() -> None:
    value = _report_data()
    value["main_findings"]["preformatted_table"] = "LLM table text"

    with pytest.raises(ReportDataError, match="main_findings has invalid fields"):
        validate_report_data(value)


@pytest.mark.stage81_report_data
def test_interpretation_edit_history_must_replay_current_value() -> None:
    value = _report_data()
    interpretation = value["interpretation"]
    interpretation["current_reviewer_interpretation"] = "Reviewer revision."
    interpretation["edit_history"] = [
        {
            "sequence": 1,
            "old_value": "Wrong starting value.",
            "new_value": "Reviewer revision.",
            "timestamp": "2026-08-11T01:00:00+00:00",
            "reviewer_context": None,
        }
    ]

    with pytest.raises(ReportDataError, match="does not replay"):
        validate_report_data(value)


@pytest.mark.stage81_report_data
def test_non_literature_reference_is_rejected() -> None:
    value = _report_data()
    value["literature_references"] = [
        canonicalize_reference(
            source="NCBI ClinVar",
            identifier="VCV000000001.1",
        )
    ]

    with pytest.raises(ReportDataError, match="not a literature record"):
        validate_report_data(value)


@pytest.mark.stage81_report_data
def test_fallback_source_requires_role_and_primary_failure() -> None:
    value = _report_data()
    source = value["data_sources"][0]
    source["fallback_used"] = True

    with pytest.raises(ReportDataError, match="fallback provenance"):
        validate_report_data(value)


@pytest.mark.stage81_report_data
def test_prohibited_identity_fields_are_rejected() -> None:
    value = _report_data()
    value["review_state"]["reviewer_notes"] = [
        "Patient name: Prohibited Person"
    ]

    with pytest.raises(ReportDataError, match="prohibited clinical data"):
        validate_report_data(value)


@pytest.mark.stage81_report_data
def test_selection_history_must_replay_inclusion_state() -> None:
    value = _report_data()
    value["review_state"]["include_in_final_report"] = False
    value["review_state"]["selection_history"] = [
        {
            "sequence": 1,
            "old_value": True,
            "new_value": False,
            "timestamp": "2026-08-11T01:00:00+00:00",
            "reviewer_context": None,
        }
    ]

    validated = validate_report_data(value)

    assert validated["review_state"]["include_in_final_report"] is False


@pytest.mark.stage81_report_data
def test_matched_hpo_terms_must_be_accepted() -> None:
    value = _report_data()
    value["phenotype_summary"]["matched_hpo_terms"] = [
        {"hpo_id": "HP:0001250", "label": "Seizure"}
    ]

    with pytest.raises(ReportDataError, match="must be accepted terms"):
        validate_report_data(value)


@pytest.mark.stage81_report_data
def test_independent_acmg_adjudication_is_out_of_scope() -> None:
    value = _report_data()
    value["classification_summary"][
        "independent_acmg_adjudication"
    ] = True

    with pytest.raises(ReportDataError, match="does not support independent"):
        validate_report_data(value)
