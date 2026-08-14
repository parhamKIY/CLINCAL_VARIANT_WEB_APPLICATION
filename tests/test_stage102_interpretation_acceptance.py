"""Stage 102 corrected-interpretation architecture acceptance suite."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.conflict_auditor import audit_evidence_conflicts
from backend.llm import LLMClient, LLMResponse, LLMTimeoutError
from backend.variant_interpretation import interpret_variant, interpret_variants
from backend.variant_report import build_draft_variant_report
from frontend.variant_status import build_variant_status_card
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    TestStage50SingleModelInterpretation as InterpretationFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage102_interpretation_acceptance
_TIMESTAMP = "2026-08-11T14:00:00Z"


def _response(
    *,
    interpretation: str,
    conflict_assessment: str = "No meaningful conflict is present.",
    phenotype_conclusion: str = "partially supported",
    warnings: list[str] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "interpretation": interpretation,
                "conflict_assessment": conflict_assessment,
                "phenotype_conclusion": phenotype_conclusion,
                "warnings": warnings or [],
            }
        ),
        model="stage102-acceptance-model",
        finish_reason="stop",
    )


def _irrelevant_phenotype_evidence() -> dict[str, object]:
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
    return evidence


def _partial_provider_evidence() -> dict[str, object]:
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    evidence["clinvar_accession"] = None
    evidence["clinvar_significance"] = None
    evidence["clinvar_review_status"] = None
    evidence["clinvar_conditions"] = []
    evidence["references"] = []
    evidence["source_statuses"]["clinvar"] = "error"
    evidence["warnings"].append(
        "ClinVar request failed; evidence is unavailable."
    )

    pathogenicity = evidence["pathogenicity"]
    assert isinstance(pathogenicity, dict)
    pathogenicity["clinvar_classification"] = None
    pathogenicity["clinvar_review_status"] = None
    pathogenicity["clinvar_conditions"] = []
    pathogenicity["clinvar_conflicting_submissions"] = {}

    capability = evidence["capability_results"]["clinvar_evidence"]
    assert isinstance(capability, dict)
    capability["status"] = "unavailable"
    capability["data"] = {
        "evidence_path": "pathogenicity.clinvar",
        "evidence_present": False,
    }

    for provider in evidence["provenance"]["providers"]:
        if provider["source"] == "clinvar":
            provider["status"] = "error"
    for lineage in evidence["provenance"]["lineage"]:
        if lineage["evidence_path"] == "pathogenicity.clinvar":
            lineage["status"] = "error"
            lineage["evidence_present"] = False

    conflict_audit = evidence["conflict_audit"]
    assert isinstance(conflict_audit, dict)
    conflict_audit["pre_review"] = audit_evidence_conflicts(
        evidence,
        phase="pre_review",
    )
    return evidence


def test_102_1_normal_evidence_interpretation_succeeds() -> None:
    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(
            FakeLLMAdapter(
                _response(
                    interpretation=(
                        "The supplied evidence supports cautious human review."
                    )
                )
            )
        ),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["interpretation"] == (
        "The supplied evidence supports cautious human review."
    )
    assert result["error_type"] is None


def test_102_2_irrelevant_phenotype_is_explicit_non_concordance() -> None:
    result = interpret_variant(
        _irrelevant_phenotype_evidence(),
        client=LLMClient(
            FakeLLMAdapter(
                _response(
                    interpretation=(
                        "Remaining variant evidence still requires human review."
                    ),
                    phenotype_conclusion="no supported association found",
                )
            )
        ),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["interpretation"].startswith(
        "Phenotype conclusion: no supported association found."
    )
    assert "Remaining variant evidence" in result["interpretation"]


def test_102_3_partial_provider_coverage_uses_restrained_language() -> None:
    evidence = _partial_provider_evidence()
    adapter = FakeLLMAdapter(
        _response(
            interpretation=(
                "Available sources support only an evidence-limited review; "
                "missing provider evidence remains unknown."
            ),
            warnings=[
                "CSpec, phenotype-gene, and disease-context coverage is incomplete."
            ],
        )
    )

    result = interpret_variant(
        evidence,
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert "evidence-limited" in result["interpretation"]
    assert "remains unknown" in result["interpretation"]
    system_prompt = adapter.requests[0].messages[0].content
    user_prompt = adapter.requests[0].messages[1].content
    assert "Missing evidence is unknown" in system_prompt
    assert '"clinvar":"error"' in user_prompt
    assert "ClinVar request failed; evidence is unavailable." in user_prompt
    assert "do not manufacture a conflict or overstate agreement" in user_prompt


def test_102_4_conflicting_evidence_remains_unresolved() -> None:
    result = interpret_variant(
        InterpretationFactory._conflicting_evidence(),
        client=LLMClient(
            FakeLLMAdapter(
                _response(
                    interpretation=(
                        "The discordant classifications require human review."
                    ),
                    conflict_assessment=(
                        "The supplied classifications disagree and remain unresolved."
                    ),
                )
            )
        ),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["prompt_mode"] == "conflict_aware"
    assert result["conflict_status"] == "conflict"
    assert "disagree" in result["conflict_assessment"]
    assert "remain unresolved" in result["conflict_assessment"]


def test_102_5_transient_model_failure_recovers_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.llm.sleep", lambda _: None)
    adapter = SequenceLLMAdapter(
        [
            LLMTimeoutError("temporary timeout"),
            _variant_interpretation_response(
                model="stage102-recovered-model"
            ),
        ]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        max_retries=1,
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["response_model"] == "stage102-recovered-model"
    assert len(adapter.requests) == 2


def test_102_6_structured_output_failure_exercises_repair_path() -> None:
    adapter = SequenceLLMAdapter(
        [
            LLMResponse(
                content=json.dumps({"interpretation": "incomplete"}),
                model="stage102-primary-model",
                finish_reason="stop",
            ),
            _variant_interpretation_response(
                model="stage102-repaired-model"
            ),
        ]
    )

    result = interpret_variant(
        EvidenceFactory._complete_evidence_object(),
        client=LLMClient(adapter),
        timestamp=_TIMESTAMP,
    )

    assert result["status"] == "success"
    assert result["response_model"] == "stage102-repaired-model"
    assert len(adapter.requests) == 2
    assert "RECOVERY INSTRUCTION" not in (
        adapter.requests[0].messages[1].content
    )
    assert "RECOVERY INSTRUCTION" in adapter.requests[1].messages[1].content


def test_102_7_total_failure_keeps_variant_and_requires_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.llm.sleep", lambda _: None)
    evidence = EvidenceFactory._complete_evidence_object()
    adapter = SequenceLLMAdapter(
        [
            LLMTimeoutError("first provider timeout"),
            LLMTimeoutError("second provider timeout"),
        ]
    )
    interpretation = interpret_variants(
        [evidence],
        client=LLMClient(adapter),
        max_retries=1,
        timestamp=_TIMESTAMP,
    )[0]
    report = build_draft_variant_report(
        evidence,
        interpretation,
        variant_index=0,
    )
    card = build_variant_status_card(report, variant_index=0, total=1)

    assert interpretation["status"] == "failed"
    assert len(adapter.requests) == 2
    assert report["reviewed_report"]["variant_summary"]["gene"] == "SCN1A"
    assert report["reviewed_report"]["variant_interpretation"]["status"] == (
        "failed"
    )
    assert card["status"] == "Interpretation requires attention"
    assert card["interpretation"] == "Interpretation requires attention"
    assert card["notices"][0] == {
        "severity": "ACTION REQUIRED",
        "message": (
            "Interpretation could not be produced after recovery attempts. "
            "Review the collected evidence and retry interpretation."
        ),
    }
