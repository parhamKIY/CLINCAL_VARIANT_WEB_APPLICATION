"""Stage 90 phenotype non-concordance contract checks."""

from copy import deepcopy

import pytest

from backend.llm import LLMClient
from backend.report import validate_evidence_object
from backend.report_data_projection import build_report_data_from_draft
from backend.variant_interpretation import (
    interpret_variant,
    interpret_variants,
    validate_variant_interpretation_result,
)
from backend.variant_report import build_draft_variant_report
from test_pipeline import (
    FakeLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage90_phenotype_non_concordance


def _unrelated_abdominal_pain_evidence() -> dict[str, object]:
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    evidence["hpo_terms"] = ["HP:0002027"]
    evidence["matched_hpo_terms"] = []
    evidence["phenotype_score"] = 0.0
    phenotype = evidence["phenotype_relationship"]
    assert isinstance(phenotype, dict)
    phenotype["patient_hpo_terms"] = ["HP:0002027"]
    phenotype["matched_patient_hpo_terms"] = []
    phenotype["local_phenotype_score"] = 0.0
    phenotype["phenotype_status"] = "no_exact_match"
    return dict(validate_evidence_object(evidence))


def _no_phenotype_evidence() -> dict[str, object]:
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    evidence["hpo_terms"] = []
    evidence["matched_hpo_terms"] = []
    evidence["phenotype_score"] = None
    phenotype = evidence["phenotype_relationship"]
    assert isinstance(phenotype, dict)
    phenotype["patient_hpo_terms"] = []
    phenotype["matched_patient_hpo_terms"] = []
    phenotype["local_phenotype_score"] = None
    phenotype["phenotype_status"] = "not_applicable"
    return dict(validate_evidence_object(evidence))


def test_unrelated_phenotype_succeeds_with_explicit_non_concordance() -> None:
    evidence = _unrelated_abdominal_pain_evidence()
    adapter = FakeLLMAdapter(
        _variant_interpretation_response(
            phenotype_conclusion="no supported association found"
        )
    )

    result = interpret_variant(
        evidence,
        client=LLMClient(adapter),
        timestamp="2026-08-11T13:00:00Z",
    )

    assert result["status"] == "success"
    assert result["interpretation"] is not None
    assert result["interpretation"].startswith(
        "Phenotype conclusion: no supported association found."
    )
    prompt = adapter.requests[0].messages[1].content
    assert "'no supported association found'" in prompt
    assert "continue interpreting the variant" in prompt
    assert "not negative pathogenicity evidence" in prompt


def test_non_concordance_survives_report_projection_without_reclassification() -> None:
    evidence = _unrelated_abdominal_pain_evidence()
    interpretation = interpret_variant(
        evidence,
        client=LLMClient(
            FakeLLMAdapter(
                _variant_interpretation_response(
                    phenotype_conclusion="no supported association found"
                )
            )
        ),
        timestamp="2026-08-11T13:00:00Z",
    )
    draft = build_draft_variant_report(
        evidence,
        interpretation,
        variant_index=0,
    )
    report_data = build_report_data_from_draft(
        draft,
        analysis_id=f"analysis-{'9' * 32}",
    )

    assert report_data["phenotype_summary"]["concordance"] == (
        "no_supported_association"
    )
    assert report_data["conclusive_result"]["classification"] == (
        "Uncertain significance"
    )
    assert report_data["conclusive_result"]["classification_source"] == (
        "LLM draft classification"
    )
    assert report_data["conclusive_result"]["status"] == "available"
    assert report_data["classification_summary"][
        "independent_acmg_adjudication"
    ] is False
    assert report_data["classification_summary"]["clinvar_classification"] == (
        "Pathogenic"
    )
    assert report_data["interpretation"]["interpretation_status"] == "available"


def test_hallucinated_supported_conclusion_is_excluded_without_losing_core() -> None:
    evidence = _unrelated_abdominal_pain_evidence()
    adapter = FakeLLMAdapter(
        _variant_interpretation_response(
            phenotype_conclusion="supported"
        )
    )
    result = interpret_variants(
        [evidence],
        client=LLMClient(adapter),
        timestamp="2026-08-11T13:00:00Z",
    )[0]

    assert result["status"] == "success"
    assert result["ai_classification"] == "Uncertain significance"
    assert result["interpretation"] is not None
    assert result["interpretation"].startswith(
        "Phenotype conclusion: no supported association found."
    )
    assert result["phenotype_conclusion"] is None
    assert result["field_validation"]["phenotype_conclusion"] == "invalid"
    assert len(adapter.requests) == 1


def test_absent_phenotype_is_valid_unavailable_evidence() -> None:
    result = interpret_variant(
        _no_phenotype_evidence(),
        client=LLMClient(
            FakeLLMAdapter(
                _variant_interpretation_response(
                    phenotype_conclusion="phenotype evidence unavailable"
                )
            )
        ),
        timestamp="2026-08-11T13:00:00Z",
    )

    assert result["status"] == "success"
    assert result["interpretation"] is not None
    assert result["interpretation"].startswith(
        "Phenotype conclusion: phenotype evidence unavailable."
    )


def test_persisted_v11_prompt_provenance_remains_valid() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    result = interpret_variant(
        evidence,
        client=LLMClient(
            FakeLLMAdapter(_variant_interpretation_response())
        ),
        timestamp="2026-08-11T13:00:00Z",
    )
    legacy = dict(result)
    legacy["prompt_version"] = "variant-interpretation-v1.1"

    validated = validate_variant_interpretation_result(
        legacy,
        evidence=evidence,
    )

    assert validated["prompt_version"] == "variant-interpretation-v1.1"


@pytest.mark.parametrize(
    ("status", "matched", "conclusion"),
    [
        ("partial_match", ["HP:0001250"], "partially supported"),
        (
            "exact_match",
            ["HP:0001250", "HP:0001263"],
            "supported",
        ),
    ],
)
def test_supported_and_partial_conclusions_remain_valid(
    status: str,
    matched: list[str],
    conclusion: str,
) -> None:
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    evidence["matched_hpo_terms"] = matched
    phenotype = evidence["phenotype_relationship"]
    assert isinstance(phenotype, dict)
    phenotype["matched_patient_hpo_terms"] = matched
    phenotype["phenotype_status"] = status
    result = interpret_variant(
        evidence,
        client=LLMClient(
            FakeLLMAdapter(
                _variant_interpretation_response(
                    phenotype_conclusion=conclusion
                )
            )
        ),
        timestamp="2026-08-11T13:00:00Z",
    )

    assert result["status"] == "success"
