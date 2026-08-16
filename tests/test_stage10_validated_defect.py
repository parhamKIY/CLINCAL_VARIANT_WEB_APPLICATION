"""Deterministic Stage 10 regression proving the rejected-MedGen LLM leak."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.evidence_readiness import build_evidence_readiness_audit
from backend.evidence_coverage import build_evidence_coverage
from backend.medgen import enrich_with_medgen_gene_disease
from backend.report import build_evidence_object
from backend.shadow_composition import shadow_free_evidence_for_llm
from backend.variant_interpretation import _build_prompt
from tests.test_medgen_gene_disease import (
    SAMPLE_CONCEPTMETA_NO_GENES,
    SAMPLE_CONCEPTMETA_SCN1B,
    _make_search_payload,
    _make_summary_payload,
    _mock_response,
    _variant,
)


pytestmark = pytest.mark.stage59_testing_v3


def _prompt_contains_rejected_medgen_diagnostics(prompt: str) -> bool:
    """Avoid dumping an unredacted prompt if the audited boundary fails."""

    return '"candidate_diagnostics"' in prompt


@patch("backend.medgen.requests.Session.get")
def test_rejected_medgen_candidates_do_not_reach_the_interpretation_prompt(
    mock_get: MagicMock,
) -> None:
    mock_get.side_effect = [
        _mock_response(200, _make_search_payload(["1002", "1003"])),
        _mock_response(
            200,
            _make_summary_payload(
                ["1002", "1003"],
                titles={"1002": "Rejected condition A", "1003": "Rejected condition B"},
                conceptmetas={
                    "1002": SAMPLE_CONCEPTMETA_SCN1B,
                    "1003": SAMPLE_CONCEPTMETA_NO_GENES,
                },
            ),
        ),
    ]
    enriched = enrich_with_medgen_gene_disease(
        [_variant(gene="SCN1A", clingen_status="not_found")]
    )["variants"][0]
    evidence = build_evidence_object(enriched)
    upstream = evidence["pathogenicity"]["medgen_gene_disease_context"]
    assert upstream["candidate_diagnostics"]
    coverage = build_evidence_coverage(evidence)
    support = next(
        item
        for item in coverage["semantic_capabilities"]
        if item["semantic_target"] == "gene_disease_support"
    )
    assert support["state"] == "UNAVAILABLE"
    assert support["diagnostic_paths"] == [
        "pathogenicity.medgen_gene_disease_context.candidate_diagnostics"
    ]
    readiness = build_evidence_readiness_audit(evidence, variant_index=0)
    prompt = _build_prompt(
        evidence,
        prompt_mode="standard",
        readiness_audit=readiness,
    )

    assert _prompt_contains_rejected_medgen_diagnostics(prompt) is False


def test_canonical_projection_removes_candidate_diagnostics_from_all_medgen_roles() -> None:
    evidence = {
        "pathogenicity": {
            "medgen_gene_disease_context": {
                "records": [{"title": "Accepted gene-disease support"}],
                "candidate_diagnostics": [{"rejection_reason": "gene_mismatch"}],
            },
            "clingen_context": [{"classification": "Definitive"}],
            "expert_curated_variant_context": {"records": [{"ca_id": "CA1"}]},
        },
        "phenotype_relationship": {
            "medgen_disease_hpo_context": {
                "records": [{"title": "Accepted disease/HPO context"}],
                "candidate_diagnostics": [{"rejection_reason": "gene_mismatch"}],
            },
            "medgen_phenotype_gene_context": [
                {
                    "records": [{"title": "Accepted phenotype-gene support"}],
                    "candidate_diagnostics": [{"rejection_reason": "gene_mismatch"}],
                }
            ],
        },
        "clinvar_significance": "Pathogenic",
    }

    projection = shadow_free_evidence_for_llm(evidence)

    assert evidence["pathogenicity"]["medgen_gene_disease_context"]["candidate_diagnostics"]
    assert "candidate_diagnostics" not in projection["pathogenicity"]["medgen_gene_disease_context"]
    assert "candidate_diagnostics" not in projection["phenotype_relationship"]["medgen_disease_hpo_context"]
    assert "candidate_diagnostics" not in projection["phenotype_relationship"]["medgen_phenotype_gene_context"][0]
    assert projection["pathogenicity"]["medgen_gene_disease_context"]["records"]
    assert projection["pathogenicity"]["clingen_context"]
    assert projection["pathogenicity"]["expert_curated_variant_context"]["records"]
    assert projection["clinvar_significance"] == "Pathogenic"
