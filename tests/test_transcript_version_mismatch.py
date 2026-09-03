"""Tests for transcript version-only mismatch severity downgrade (Option 1).

RED-first: these tests verify the NEW behaviour introduced by the
Option-1 fix to conflict_auditor.py.

Before the fix:
  ANY transcript string difference -> severity = "major"

After the fix:
  version-only difference (same base ENST/NM ID, only .N suffix differs)
    -> severity = "minor"  (not in MEANINGFUL_CONFLICT_SEVERITIES)
    -> prompt_mode = "standard"

  genuinely different base IDs -> severity = "major" (unchanged)

NOTE ON APPROACH:
  Unit tests (TestTranscriptVersionMismatchSeverity) patch transcript values
  directly into a copy of the EvidenceObject and call audit_evidence_conflicts
  in isolation -- bypassing the integrity check that validate_evidence_object
  runs, which is appropriate for testing conflict_auditor in isolation.

  Integration tests (TestVersionOnlyMismatchDoesNotForceConflictAwareMode)
  build a fresh EvidenceObject with all sources set so that build_evidence_object
  and conflict_auditor agree, then call interpret_variant.
"""

from __future__ import annotations

import copy
import json

import pytest

from backend.conflict_auditor import audit_evidence_conflicts
from backend.llm import LLMClient, LLMResponse
from backend.report import build_evidence_object, validate_evidence_object
from backend.variant_interpretation import interpret_variant
from test_pipeline import FakeLLMAdapter, TestEvidenceObject as EvidenceFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _findings_by_type(result: dict) -> dict[str, dict]:
    return {f["conflict_type"]: f for f in result["findings"]}


def _audit_direct(
    *,
    context_transcript: str,
    vep_transcript: str,
    genebe_transcript: str,
) -> dict:
    """Build evidence, patch transcripts in a deepcopy, run the auditor in
    isolation (no validate_evidence_object integrity check)."""
    candidate = EvidenceFactory._pipeline_candidate()
    sources = candidate["sources"]
    sources["clinvar"] = {"status": "not_found"}
    candidate["transcript"] = context_transcript
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
    evidence = validate_evidence_object(build_evidence_object(candidate))
    patched = copy.deepcopy(evidence)
    # Override the three transcript locations directly for the auditor call
    patched["variant_context"]["transcript"] = context_transcript
    patched["annotations"]["vep"]["transcript"] = vep_transcript
    patched["annotations"]["genebe"]["transcript"] = genebe_transcript
    # Call auditor directly -- bypasses the build-level integrity check
    return audit_evidence_conflicts(patched, phase="pre_review")


def _build_pipeline_evidence(
    *,
    vep_transcript: str,
    genebe_transcript: str,
    candidate_transcript: str | None = None,
) -> dict:
    """Build a fully consistent EvidenceObject through build_evidence_object,
    supplying VEP source data so that all three transcript locations are
    populated consistently and the conflict_audit matches."""
    candidate = EvidenceFactory._pipeline_candidate()
    sources = candidate["sources"]
    sources["clinvar"] = {"status": "not_found"}
    if candidate_transcript is not None:
        candidate["transcript"] = candidate_transcript

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


# ---------------------------------------------------------------------------
# Unit tests -- conflict_auditor in isolation
# ---------------------------------------------------------------------------

class TestTranscriptVersionMismatchSeverity:

    def test_version_only_suffix_difference_is_minor(self):
        """ENST00000350721.9 vs ENST00000350721 -- same base, suffix stripped."""
        result = _audit_direct(
            context_transcript="ENST00000350721.9",
            vep_transcript="ENST00000350721.9",
            genebe_transcript="ENST00000350721",
        )
        findings = _findings_by_type(result)
        assert "transcript_mismatch" in findings
        assert findings["transcript_mismatch"]["severity"] == "minor"

    def test_two_different_version_numbers_on_same_base_is_minor(self):
        """ENST00000350721.9 vs ENST00000350721.10 -- v9 vs v10."""
        result = _audit_direct(
            context_transcript="ENST00000350721.9",
            vep_transcript="ENST00000350721.10",
            genebe_transcript="ENST00000350721.9",
        )
        findings = _findings_by_type(result)
        assert "transcript_mismatch" in findings
        assert findings["transcript_mismatch"]["severity"] == "minor"

    def test_nm_version_only_suffix_difference_is_minor(self):
        """NM_001234.5 vs NM_001234.6 -- RefSeq-style version mismatch."""
        result = _audit_direct(
            context_transcript="NM_001234.5",
            vep_transcript="NM_001234.6",
            genebe_transcript="NM_001234.5",
        )
        findings = _findings_by_type(result)
        assert "transcript_mismatch" in findings
        assert findings["transcript_mismatch"]["severity"] == "minor"

    def test_genuinely_different_base_ids_remain_major(self):
        """ENST00000350721 vs ENST00000303395 -- completely different transcripts."""
        result = _audit_direct(
            context_transcript="ENST00000350721.9",
            vep_transcript="ENST00000350721.9",
            genebe_transcript="ENST00000303395.1",
        )
        findings = _findings_by_type(result)
        assert "transcript_mismatch" in findings
        assert findings["transcript_mismatch"]["severity"] == "major"

    def test_identical_transcripts_produce_no_mismatch_finding(self):
        """No mismatch when all three sources agree exactly."""
        result = _audit_direct(
            context_transcript="ENST00000350721.9",
            vep_transcript="ENST00000350721.9",
            genebe_transcript="ENST00000350721.9",
        )
        findings = _findings_by_type(result)
        assert "transcript_mismatch" not in findings

    def test_mixed_namespace_enst_vs_nm_remains_major(self):
        """ENST ID vs NM ID -- different namespaces must remain major."""
        result = _audit_direct(
            context_transcript="ENST00000350721.9",
            vep_transcript="ENST00000350721.9",
            genebe_transcript="NM_001184.4",
        )
        findings = _findings_by_type(result)
        assert "transcript_mismatch" in findings
        assert findings["transcript_mismatch"]["severity"] == "major"

    def test_version_only_mismatch_leaves_routing_severity_below_meaningful(self):
        """routing_severity must not enter MEANINGFUL_CONFLICT_SEVERITIES."""
        result = _audit_direct(
            context_transcript="ENST00000350721.9",
            vep_transcript="ENST00000350721.9",
            genebe_transcript="ENST00000350721",
        )
        meaningful = {"moderate", "major", "critical"}
        assert result["routing_severity"] not in meaningful

    def test_version_only_mismatch_message_mentions_version(self):
        """The finding message must distinguish version-only from a full switch."""
        result = _audit_direct(
            context_transcript="ENST00000350721.9",
            vep_transcript="ENST00000350721.9",
            genebe_transcript="ENST00000350721",
        )
        findings = _findings_by_type(result)
        assert "transcript_mismatch" in findings
        assert "version" in findings["transcript_mismatch"]["message"].lower()


# ---------------------------------------------------------------------------
# Integration tests -- end-to-end through interpret_variant
# ---------------------------------------------------------------------------

class TestVersionOnlyMismatchDoesNotForceConflictAwareMode:

    @staticmethod
    def _lb_response() -> LLMResponse:
        return LLMResponse(
            content=json.dumps({
                "ai_classification": "Likely benign",
                "interpretation": "PM2 and BP4_Strong support likely benign.",
                "conflict_assessment": "Transcript version difference is minor.",
                "phenotype_conclusion": "no supported association found",
                "warnings": [],
            }),
            model="option1-test-model",
            finish_reason="stop",
        )

    def test_version_only_mismatch_uses_standard_prompt_mode(self):
        """VEP returns .9, GeneBe returns base ID (no version).
        prompt_mode must be 'standard', not 'conflict_aware'."""
        evidence = _build_pipeline_evidence(
            vep_transcript="ENST00000350721.9",
            genebe_transcript="ENST00000350721",
            candidate_transcript="ENST00000350721.9",
        )
        interpretation = interpret_variant(
            evidence,
            client=LLMClient(FakeLLMAdapter(self._lb_response())),
            timestamp="2026-09-03T00:00:00Z",
        )
        assert interpretation["status"] == "success"
        assert interpretation["prompt_mode"] == "standard"
        assert interpretation["ai_classification"] == "Likely benign"

    def test_genuine_transcript_mismatch_still_uses_conflict_aware_mode(self):
        """VEP returns ENST00000350721, GeneBe returns a completely different
        transcript -- must activate conflict_aware mode."""
        evidence = _build_pipeline_evidence(
            vep_transcript="ENST00000350721.9",
            genebe_transcript="ENST00000303395.1",
            candidate_transcript="ENST00000350721.9",
        )
        vus = LLMResponse(
            content=json.dumps({
                "ai_classification": "Uncertain significance",
                "interpretation": "Transcript mismatch prevents classification.",
                "conflict_assessment": "VEP and GeneBe transcripts disagree.",
                "phenotype_conclusion": "no supported association found",
                "warnings": [],
            }),
            model="option1-test-model",
            finish_reason="stop",
        )
        interpretation = interpret_variant(
            evidence,
            client=LLMClient(FakeLLMAdapter(vus)),
            timestamp="2026-09-03T00:00:00Z",
        )
        assert interpretation["status"] == "success"
        assert interpretation["prompt_mode"] == "conflict_aware"
