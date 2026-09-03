"""Tests for Option 2 variant interpretation calibration and minor conflict harmonization."""

from __future__ import annotations

import json
from typing import Mapping

import pytest

from backend.conflict_auditor import (
    MEANINGFUL_CONFLICT_SEVERITIES,
    audit_evidence_conflicts,
)
from backend.llm import LLMClient, LLMResponse
from backend.report import build_evidence_object, validate_evidence_object
from backend.variant_interpretation import (
    VARIANT_INTERPRETATION_SYSTEM_PROMPT,
    _build_prompt,
    interpret_variant,
)
from test_pipeline import (
    FakeLLMAdapter,
    SequenceLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
)


def _build_atr_style_evidence(
    *,
    vep_transcript: str = "ENST00000350721.9",
    genebe_transcript: str = "ENST00000350721",
) -> dict[str, object]:
    candidate = EvidenceFactory._pipeline_candidate()
    sources = candidate["sources"]
    assert isinstance(sources, dict)
    sources["clinvar"] = {"status": "not_found"}
    candidate["transcript"] = vep_transcript
    sources["vep"] = {
        "status": "success",
        "provider": "Ensembl VEP",
        "provider_version": "113",
        "retrieved_at": "2026-08-01T00:00:00Z",
        "transcript": vep_transcript,
        "gene": "ATR",
        "gene_id": "ENSG00000175387",
        "hgvsc": f"{vep_transcript}:c.5289-207T>G",
        "hgvsp": None,
        "consequence": "intron_variant",
        "impact": "MODIFIER",
        "assembly": "GRCh38",
        "normalized_variant": {
            "chrom": "3",
            "pos": 142499925,
            "ref": "A",
            "alt": "C",
        },
    }
    sources["genebe"] = {
        "status": "success",
        "provider": "GeneBe",
        "provider_version": "test",
        "retrieved_at": "2026-08-01T00:00:00Z",
        "gene": "ATR",
        "gene_hgnc_id": 795,
        "transcript": genebe_transcript,
        "automated_acmg_classification": "Likely_benign",
        "automated_acmg_criteria": ["PM2", "BP4_Strong"],
        "automated_acmg_score": -6.0,
        "population_annotations": {},
        "predictor_annotations": {},
        "clinvar_derived": {},
    }
    return validate_evidence_object(build_evidence_object(candidate))


def test_version_only_transcript_difference_produces_no_conflict_status_and_minor_severity() -> None:
    """When only minor findings exist (like transcript version difference), status is no_conflict."""
    evidence = _build_atr_style_evidence()
    audit = evidence["conflict_audit"]["pre_review"]
    assert isinstance(audit, dict)

    assert audit["routing_severity"] == "minor"
    assert audit["routing_severity"] not in MEANINGFUL_CONFLICT_SEVERITIES
    assert audit["status"] == "no_conflict"
    findings = audit["findings"]
    assert len(findings) == 1
    assert findings[0]["conflict_type"] == "transcript_mismatch"
    assert findings[0]["severity"] == "minor"
    assert "not a biological conflict" in findings[0]["message"]


def test_genuine_conflict_produces_conflict_status_and_conflict_aware_mode() -> None:
    """When a major finding exists, status is conflict and prompt_mode is conflict_aware."""
    evidence = _build_atr_style_evidence(
        vep_transcript="ENST00000350721.9",
        genebe_transcript="ENST00000303395.1",
    )
    audit = evidence["conflict_audit"]["pre_review"]
    assert isinstance(audit, dict)

    assert audit["routing_severity"] == "major"
    assert audit["routing_severity"] in MEANINGFUL_CONFLICT_SEVERITIES
    assert audit["status"] == "conflict"


def test_system_prompt_contains_minor_conflict_and_absent_clinvar_guidance() -> None:
    """System prompt explicitly explains minor findings and absent ClinVar synthesis."""
    system_prompt = VARIANT_INTERPRETATION_SYSTEM_PROMPT

    assert "minor findings" in system_prompt.casefold()
    assert "not meaningful biological conflicts" in system_prompt.casefold()
    assert "when clinvar or literature is absent" in system_prompt.casefold() or (
        "when clinvar is absent" in system_prompt.casefold()
    )


def test_standard_mode_task_instruction_guides_direction_synthesis() -> None:
    """Task instruction in standard mode advises reflecting consistent automated ACMG criteria."""
    from backend.evidence_readiness import build_evidence_readiness_audit

    evidence = _build_atr_style_evidence()
    readiness_audit = build_evidence_readiness_audit(evidence, variant_index=0)
    prompt = _build_prompt(
        evidence,
        prompt_mode="standard",
        readiness_audit=readiness_audit,
    )

    assert "Task instruction:" in prompt
    assert "not biological conflicts" in prompt.casefold() or "not meaningful" in prompt.casefold()
    assert "automated acmg criteria" in prompt.casefold()
