"""Focused Stage 11 guardrails for current evidence-resilience documentation."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_authoritative_documents_record_current_versions_and_closeout_state() -> None:
    declaration = _read("docs/PROJECT_DECLARATION.md")
    handoff = _read("docs/AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md")
    roadmap = _read("docs/EVIDENCE_RESILIENCE_IMPLEMENTATION_ROADMAP.md")
    readme = _read("README.md")

    for document in (declaration, handoff, roadmap, readme):
        assert "EvidenceObject" in document or "Evidence Object" in document
    assert "EvidenceObject remains `2.5`" in roadmap
    assert "SQLite remains `4`" in roadmap
    assert "pipeline was\n`3.2` at closeout" in roadmap
    assert "Pipeline schema: `3.3`" in readme
    assert "Active pipeline schema: `3.3`" in declaration
    assert "Stages 1" in handoff and "COMPLETE / APPROVED" in handoff
    assert "Stages 1" in declaration and "COMPLETE /" in declaration


def test_current_handoff_records_implemented_semantic_and_llm_boundaries() -> None:
    handoff = _read("docs/AI_HANDOFF_MASTER_EVIDENCE_GRAPH.md")

    for required in (
        "`expert_curated_variant_context`",
        "`gene_disease_validity`",
        "`gene_disease_support`",
        "`phenotype_gene_ranking`",
        "`phenotype_gene_support`",
        "`shadow_free_evidence_for_llm()`",
        "`candidate_diagnostics`",
        "applicable critical fields",
    ):
        assert required in handoff

    for stale_current_claim in (
        "VEP outage annotation composition: NOT YET IMPLEMENTED",
        "ERepo integration: NEXT",
        "MedGen integration: NEXT",
        "Evidence Coverage Calculator: NEXT",
    ):
        assert stale_current_claim not in handoff


def test_configuration_template_documents_approved_provider_controls_without_test_switches() -> None:
    environment = _read(".env.example")

    for required in (
        "ENABLE_EREPO=true",
        "EREPO_TIMEOUT=30",
        "EREPO_MAX_RETRIES=1",
        "ENABLE_MEDGEN=true",
        "MEDGEN_TIMEOUT=30",
        "MEDGEN_MAX_RETRIES=1",
    ):
        assert required in environment
    assert "RUN_LIVE_PROVIDER_TESTS" not in environment
