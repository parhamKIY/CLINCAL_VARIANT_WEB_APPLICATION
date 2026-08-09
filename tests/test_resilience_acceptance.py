"""Stage 78 multi-variant degraded-mode acceptance scenario."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import requests

import tests.test_pipeline as fixtures
from backend.database import load_pipeline_state
from backend.llm import LLMClient
from backend.pipeline import run_analysis
from config import settings


def _ensembl_population_payload(
    *,
    rsid: str,
    position: int,
) -> dict[str, object]:
    return {
        "name": rsid,
        "source": "dbSNP",
        "release": 156,
        "MAF": "0.001",
        "mappings": [
            {
                "assembly_name": "GRCh38",
                "seq_region_name": "2",
                "start": position,
                "allele_string": "C/T",
            }
        ],
        "populations": [
            {
                "population": "1000GENOMES:phase_3:EUR",
                "allele": "T",
                "frequency": 0.002,
            }
        ],
    }


def _annotation_candidate(
    variant: dict[str, object],
    *,
    rsid: str,
) -> dict[str, object]:
    candidate = fixtures.TestEvidenceObject._pipeline_candidate()
    candidate["variant"] = dict(variant)
    candidate["protein_change"] = None
    sources = candidate["sources"]
    sources["vep"]["provider"] = "Ensembl VEP"
    sources["myvariant"].update(
        {
            "provider": "MyVariant.info",
            "variant_id": (
                f"chr2:g.{variant['pos']}{variant['ref']}>{variant['alt']}"
            ),
            "rsid": rsid,
            "population_frequencies": {"gnomad_exome": 0.0001},
            "upstream_sources": ["dbSNP", "gnomAD"],
        }
    )
    sources["clinvar"].update(
        {
            "provider": "NCBI ClinVar",
            "clinical_significance": "uncertain significance",
            "review_status": "criteria provided, single submitter",
        }
    )
    sources["cspec"] = {
        "status": "success",
        "provider": "ClinGen CSpec Registry",
        "provider_role": "primary",
        "fallback_used": False,
        "primary_provider": "clingen_cspec",
        "primary_failure": None,
        "specifications": [
            {
                "specification_id": "SCN1A-EP",
                "title": "SCN1A VCEP specification",
                "version": "1.0",
                "status": "Released",
                "matched_disease_ids": ["MONDO:0100062"],
                "scope_match": "gene_and_disease",
            }
        ],
        "rule_logic_applied": False,
    }
    return candidate


@pytest.mark.stage78_acceptance
def test_multi_variant_resilience_acceptance_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove controlled degraded operation through persisted Draft Reports."""

    variants = [
        {
            "chrom": "2",
            "pos": 166848215 + index,
            "ref": "C",
            "alt": "T",
            "qual": 99.0,
            "filter": "PASS",
        }
        for index in range(2)
    ]
    rsids = ["rs121913529", "rs121913530"]

    def fake_annotate(
        normalized_variants: object,
        **_: object,
    ) -> list[dict[str, object]]:
        return [
            _annotation_candidate(dict(variant), rsid=rsids[index])
            for index, variant in enumerate(normalized_variants)  # type: ignore[arg-type]
        ]

    def fake_mydisease(
        candidates: object,
        _phenotypes: object,
        **_: object,
    ) -> dict[str, object]:
        retained = [
            deepcopy(dict(candidate))
            for candidate in candidates  # type: ignore[union-attr]
        ]
        return {
            "variants": retained,
            "status": "no_association",
            "message": "No MyDisease association was required by this fixture.",
            "request_attempts": 0,
            "local_degraded_count": 0,
        }

    monkeypatch.setattr("backend.pipeline.annotate_variants", fake_annotate)
    monkeypatch.setattr("backend.pipeline.enrich_with_mydisease", fake_mydisease)
    monkeypatch.setattr(
        "backend.conditional_enrichment.time.sleep",
        lambda _delay: None,
    )
    monkeypatch.setattr(settings, "CONDITIONAL_ENRICHMENT_MAX_RETRIES", 1)
    monkeypatch.setattr(settings, "ENABLE_GNOMAD_DEEP_LOOKUP", True)
    monkeypatch.setattr(settings, "ENABLE_LITERATURE_ENRICHMENT", True)
    monkeypatch.setattr(settings, "CONDITIONAL_ENRICHMENT_MAX_VARIANTS", 10)

    ontology_path = fixtures.TestPhenotype._write_hpo_fixture(tmp_path)
    associations_path = fixtures.TestPhenotype._write_hpo_gene_fixture(tmp_path)
    phen2gene_session = fixtures.FakePhen2GeneSession(
        [requests.Timeout("private Phen2Gene timeout")]
    )
    population_session = fixtures.FakeConditionalSession(
        post_responses=[fixtures.FakeResponse(403, {})],
        get_responses=[
            fixtures.FakeResponse(
                200,
                _ensembl_population_payload(
                    rsid=rsids[index],
                    position=int(variants[index]["pos"]),
                ),
            )
            for index in range(2)
        ],
    )
    literature_session = fixtures.FakeConditionalSession(
        get_responses=[
            fixtures.FakeResponse(500, {}),
            fixtures.FakeResponse(500, {}),
            fixtures.FakeResponse(
                200,
                {"resultList": {"result": [{"pmid": "123"}]}},
            ),
            fixtures.FakeResponse(500, {}),
            fixtures.FakeResponse(500, {}),
            fixtures.FakeResponse(
                200,
                {"resultList": {"result": [{"pmid": "456"}]}},
            ),
        ]
    )
    llm_adapter = fixtures.FakeLLMAdapter(
        fixtures._variant_interpretation_response(
            conflict_assessment=(
                "The VUS evidence requires cautious human review."
            )
        )
    )
    database_path = tmp_path / "stage78.sqlite3"

    analysis = run_analysis(
        vcf_path=None,
        manual_variants=variants,
        phenotypes=["HP:0001250"],
        input_type="manual",
        ontology_path=ontology_path,
        associations_path=associations_path,
        phen2gene_max_retries=0,
        phen2gene_session=phen2gene_session,  # type: ignore[arg-type]
        phen2gene_use_cache=False,
        population_session=population_session,  # type: ignore[arg-type]
        literature_session=literature_session,  # type: ignore[arg-type]
        llm_client=LLMClient(llm_adapter),
        database_path=database_path,
    )

    assert analysis["status"] == "partial"
    assert analysis["variant_count"] == 2
    assert len(analysis["draft_variant_reports"]) == 2
    assert len(llm_adapter.requests) == 2
    assert len(phen2gene_session.calls) == 1
    assert len(population_session.post_calls) == 1
    assert len(population_session.get_calls) == 2
    assert len(literature_session.get_calls) == 6

    for evidence in analysis["evidence_objects"]:
        capabilities = evidence["capability_results"]
        phenotype = capabilities["phenotype_gene"]
        population = capabilities["population_frequency"]
        literature = capabilities["literature"]
        annotation = capabilities["variant_annotation"]
        variant_context = capabilities["variant_context"]
        clinvar = capabilities["clinvar_evidence"]
        cspec = capabilities["cspec_context"]

        assert phenotype["provider"] == "local_hpo_gene_fallback"
        assert phenotype["provider_role"] == "fallback"
        assert phenotype["primary_failure"] == "timeout"
        assert population["provider"] == "ensembl_variation"
        assert population["provider_role"] == "fallback"
        assert population["fallback_for"] == "gnomad"
        assert population["primary_failure"] == "forbidden"
        assert literature["provider"] == "europe_pmc"
        assert literature["provider_role"] == "fallback"
        assert literature["fallback_for"] == "litvar"
        assert literature["primary_failure"] == "server_error"
        assert annotation["provider"] == "ensembl_vep"
        assert annotation["provider_role"] == "primary"
        assert annotation["fallback_used"] is False
        assert variant_context["provider"] == "myvariant"
        assert variant_context["provider_role"] == "primary"
        assert variant_context["fallback_used"] is False
        assert clinvar["provider"] == "ncbi_clinvar"
        assert clinvar["provider_role"] == "primary"
        assert clinvar["fallback_used"] is False
        assert cspec["provider"] == "clingen_cspec"
        assert cspec["provider_role"] == "primary"
        assert cspec["fallback_used"] is False

    assert [
        item["pmid"]
        for evidence in analysis["evidence_objects"]
        for item in evidence["conditional_enrichment"]["literature"]["articles"]
    ] == ["123", "456"]
    persisted = load_pipeline_state(
        analysis["analysis_id"],
        database_path=database_path,
    )
    assert len(persisted["draft_variant_reports"]) == 2
    assert [
        evidence["capability_results"]["population_frequency"]["provider"]
        for evidence in persisted["evidence_objects"]
    ] == ["ensembl_variation", "ensembl_variation"]
    assert [
        evidence["capability_results"]["literature"]["provider"]
        for evidence in persisted["evidence_objects"]
    ] == ["europe_pmc", "europe_pmc"]
    assert [
        evidence["capability_results"]["phenotype_gene"]["provider"]
        for evidence in persisted["evidence_objects"]
    ] == ["local_hpo_gene_fallback", "local_hpo_gene_fallback"]
    for report in persisted["draft_variant_reports"]:
        fallback_entries = [
            provider
            for provider in report["machine_original_report"]["provenance"][
                "providers"
            ]
            if provider.startswith("Fallback:")
        ]
        assert len(fallback_entries) == 3
        assert any("Phen2Gene" in entry for entry in fallback_entries)
        assert any("gnomAD" in entry for entry in fallback_entries)
        assert any("LitVar2" in entry for entry in fallback_entries)
    serialized = repr(persisted)
    assert "private Phen2Gene timeout" not in serialized
    assert "gnomAD" in serialized
    assert "Ensembl" in serialized
    assert "Europe PMC" in serialized
