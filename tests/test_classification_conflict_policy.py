"""Source assertion conflicts remain consistent through interpretation and reports."""

import json
import hashlib
from copy import deepcopy

import pytest

from backend.classification_evidence import build_classification_evidence_audit
from backend.evidence_confirmation import (
    confirm_evidence_review,
    validate_reviewed_evidence_package,
)
from backend.evidence_review import build_evidence_review_reports
from backend.llm import LLMClient, LLMResponse
from backend.report import build_evidence_object, validate_evidence_object
from backend.report_data_projection import build_report_data_from_draft
from backend.variant_interpretation import interpret_variant
from backend.variant_report import build_draft_variant_report
from frontend.report_preview import render_draft_report_preview_pages
from test_pipeline import FakeLLMAdapter, TestEvidenceObject as EvidenceFactory


def _candidate(direct, automated):
    candidate = EvidenceFactory._pipeline_candidate()
    candidate["sources"]["clinvar"]["clinical_significance"] = direct
    if automated is not None:
        candidate["sources"]["genebe"] = {
            "status": "success",
            "provider": "GeneBe",
            "automated_acmg_classification": automated,
            "automated_acmg_criteria": [],
        }
    return candidate


@pytest.mark.parametrize(
    "direct,automated,severity,conflict",
    [
        ("Pathogenic/Likely pathogenic", "Benign", "major", True),
        ("Benign/Likely benign", "Pathogenic", "major", True),
        ("Conflicting classifications of pathogenicity", None, "major", True),
        ("Conflicting interpretations of pathogenicity", None, "major", True),
        ("Pathogenic", "Likely pathogenic", "minor", False),
        ("Benign", "Likely benign", "minor", False),
        ("Pathogenic/Likely pathogenic", "Pathogenic", "minor", False),
        ("Benign/Likely benign", "Likely benign", "minor", False),
        ("Pathogenic/Likely pathogenic", None, "none", False),
        ("Uncertain significance", "Pathogenic", "major", True),
        ("Benign", "Pathogenic", "major", True),
        ("Pathogenic", "Pathogenic", "none", False),
    ],
)
def test_conflict_policy_through_evidence_prompt_and_report(
    direct, automated, severity, conflict
):
    candidate = _candidate(direct, automated)
    original = deepcopy(candidate)
    evidence = build_evidence_object(candidate)
    audit = evidence["conflict_audit"]["pre_review"]
    assert audit["routing_severity"] == severity
    assert audit["status"] == ("conflict" if conflict else "no_conflict")
    assert any(r["original_label"] == direct for r in audit["normalized_classifications"])
    assert candidate == original
    assert evidence["pathogenicity"]["clinvar_classification"] == direct
    assert evidence["pathogenicity"]["automated_acmg_classification"] == automated
    assert audit["final_classification"] is None
    assert (build_classification_evidence_audit(evidence)["state"] ==
            "CONFLICTING_CLASSIFICATIONS") is conflict

    adapter = FakeLLMAdapter(LLMResponse(
        content=json.dumps({
            "ai_classification": "Uncertain significance",
            "interpretation": "The available evidence remains uncertain for review.",
            "conflict_assessment": "Source assertions are retained for review.",
            "phenotype_conclusion": "partially supported",
            "warnings": [],
        }),
        model="conflict-policy-test", finish_reason="stop",
    ))
    result = interpret_variant(evidence, client=LLMClient(adapter))
    assert result["prompt_mode"] == ("conflict_aware" if conflict else "standard")
    assert result["ai_classification"] == "Uncertain significance"
    draft = build_draft_variant_report(evidence, result, variant_index=0)
    report = build_report_data_from_draft(draft, analysis_id="analysis-" + "4" * 32)
    assert report["classification_summary"]["conflict_status"] == (
        "conflict" if conflict else "none"
    )
    assert ("Source classification context: Classification conflict" in
            render_draft_report_preview_pages(draft)[0]) is conflict
    assert len(adapter.requests) == 1


def test_same_direction_review_quality_difference_does_not_escalate_conflict():
    candidate = _candidate("Pathogenic", "Likely pathogenic")
    candidate["sources"]["genebe"]["clinvar_derived"] = {
        "upstream_source": "ClinVar",
        "classification": "Likely pathogenic",
        "review_status": "criteria provided, single submitter",
    }
    audit = build_evidence_object(candidate)["conflict_audit"]["pre_review"]
    assert audit["status"] == "no_conflict"
    assert audit["routing_severity"] == "minor"
    assert "source_quality_mismatch" not in {
        finding["conflict_type"] for finding in audit["findings"]
    }


def test_historical_evidence_with_old_conflict_policy_remains_valid():
    evidence = build_evidence_object(_candidate("Pathogenic", "Likely pathogenic"))
    # Exact pre-fix audit for these source assertions; preserve the saved snapshot.
    audit = evidence["conflict_audit"]["pre_review"]
    audit.pop("classification_policy", None)
    audit["status"] = "conflict"
    audit["routing_severity"] = "major"
    for finding in audit["findings"]:
        if finding["conflict_type"] == "classification_disagreement":
            finding["severity"] = "major"
            finding["message"] = "Normalized five-class source assertions disagree."
    original = deepcopy(evidence)
    assert validate_evidence_object(evidence) == original
    assert evidence == original

    report = build_evidence_review_reports(
        [evidence], timestamp="2026-09-05T00:00:00Z"
    )[0]
    package = confirm_evidence_review(report, timestamp="2026-09-05T00:01:00Z")
    assert package["post_review_conflict"]["routing_severity"] == "minor"
    # Reconstruct the historical confirmed snapshot with its exact old audit.
    historical_post = deepcopy(audit)
    historical_post["phase"] = "post_review"
    historical_post["findings"].append({
        "conflict_type": "user_override_conflict",
        "severity": "major",
        "evidence_paths": ["reviewed_values.classification"],
        "sources": ["Human review", "Original source evidence"],
        "message": "Reviewed values conflict with preserved source evidence.",
    })
    package["post_review_conflict"] = historical_post
    serialized = json.dumps(
        {k: v for k, v in package.items() if k != "package_id"},
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    package["package_id"] = "reviewed-package-" + hashlib.sha256(serialized).hexdigest()[:24]
    assert validate_reviewed_evidence_package(package) == package


def test_conflict_policy_marker_does_not_allow_tampering():
    evidence = build_evidence_object(_candidate("Pathogenic/Likely pathogenic", "Benign"))
    evidence["conflict_audit"]["pre_review"]["routing_severity"] = "none"
    from backend.report import EvidenceObjectError
    with pytest.raises(EvidenceObjectError, match="deterministic source-evidence audit"):
        validate_evidence_object(evidence)
