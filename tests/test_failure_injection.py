"""Stage 75 deterministic provider failure-injection acceptance suite."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import requests

import tests.test_pipeline as fixtures
from backend.annotation import annotate_variants
from backend.conditional_enrichment import (
    fetch_literature_evidence,
    fetch_population_evidence_with_fallback,
)
from backend.fallback_transparency import build_fallback_notices
from backend.final_clinical_report import (
    compose_final_clinical_report,
    render_final_clinical_report_markdown,
)
from backend.llm import LLMClient
from backend.phenotype import enrich_with_phen2gene
from backend.pipeline import PIPELINE_SCHEMA_VERSION
from backend.provider_resilience import ProviderCircuitState
from backend.report import build_evidence_object
from backend.variant_interpretation import interpret_variant
from backend.variant_report import build_draft_variant_report
from config import settings


def _final_report_asserts_degraded_source(
    evidence: dict[str, object],
    *,
    capability: str,
    provider: str,
    method: str,
    primary_failure: str,
) -> None:
    result = evidence["capability_results"][capability]
    assert result["status"] == "success"
    assert result["status"] != "no_match"
    assert result["fallback_used"] is True
    assert result["provider"] == provider
    assert result["method"] == method
    assert result["primary_failure"] == primary_failure
    notices = build_fallback_notices(evidence["capability_results"])
    notice = next(item for item in notices if item["capability"] == capability)
    expected_provider_entry = f"Fallback: {notice['message']}"

    interpretation = dict(
        interpret_variant(
            evidence,
            client=LLMClient(
                fixtures.FakeLLMAdapter(
                    fixtures._variant_interpretation_response()
                )
            ),
        )
    )
    draft = build_draft_variant_report(
        evidence,
        interpretation,
        variant_index=0,
    )
    final_report = compose_final_clinical_report(
        {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "analysis_id": None,
            "variant_count": 1,
            "draft_variant_reports": [draft],
            "reviewed_evidence_packages": [
                {
                    "variant_index": 0,
                    "package_id": f"reviewed-package-{'a' * 24}",
                    "confirmed_at": "2026-08-09T00:00:00Z",
                }
            ],
        },
        timestamp="2026-08-09T01:00:00Z",
    )

    providers = final_report["method_data_sources"]["providers"]
    assert providers.count(expected_provider_entry) == 1
    markdown = render_final_clinical_report_markdown(final_report)
    assert markdown.count(notice["message"]) == 1


def _candidate_with_enrichment(
    candidate: dict[str, object],
    *,
    population: dict[str, object] | None = None,
    literature: dict[str, object] | None = None,
) -> dict[str, object]:
    enriched = deepcopy(candidate)
    enriched["conditional_enrichment"] = {
        "triggered": True,
        "triggers": [
            "population_evidence_ambiguity"
            if population is not None
            else "literature_evidence_need"
        ],
        "population_frequency": population
        or {
            "status": "not_triggered",
            "provider": "gnomAD",
            "populations": [],
        },
        "literature": literature
        or {
            "status": "not_triggered",
            "providers": {},
            "articles": [],
        },
        "myvariant_fallback": {
            "used": False,
            "status": "not_needed",
            "independent_evidence": False,
        },
        "warnings": [],
    }
    return enriched


class TestStage75FailureInjection:
    """Exercise every required outage-to-fallback transition offline."""

    def test_phen2gene_timeout_retries_then_uses_local_overlap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        phenotype_case = fixtures.TestPhenotype()
        ontology_path = phenotype_case._write_hpo_fixture(tmp_path)
        associations_path = phenotype_case._write_hpo_gene_fixture(tmp_path)
        session = fixtures.FakePhen2GeneSession(
            [
                requests.Timeout("first private timeout"),
                requests.Timeout("second private timeout"),
            ]
        )
        delays: list[float] = []
        monkeypatch.setattr(
            "backend.provider_resilience.time.sleep",
            delays.append,
        )
        candidate = fixtures.TestEvidenceObject._complete_candidate()

        result = enrich_with_phen2gene(
            [candidate],
            ["HP:0001250"],
            ontology_path=ontology_path,
            associations_path=associations_path,
            max_retries=1,
            session=session,  # type: ignore[arg-type]
            use_cache=False,
        )

        assert len(session.calls) == 2
        assert delays == [1.0]
        assert result["fallback_used"] is True
        assert result["primary_failure"] == "timeout"
        assert len(result["variants"]) == 1
        enriched = result["variants"][0]
        phenotype = enriched["phen2gene"]
        assert phenotype["provider"] == "local_hpo_gene_fallback"
        assert phenotype["matched_hpos"] == ["HP:0001250"]
        assert phenotype["score"] == 1.0
        evidence = build_evidence_object(enriched)
        _final_report_asserts_degraded_source(
            evidence,
            capability="phenotype_gene",
            provider="local_hpo_gene_fallback",
            method="direct_hpo_gene_overlap",
            primary_failure="timeout",
        )

    def test_gnomad_forbidden_opens_circuit_and_falls_back_for_remaining(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.conditional_enrichment.time.sleep",
            lambda _delay: None,
        )
        evidence_case = fixtures.TestEvidenceObject()
        session = fixtures.FakeConditionalSession(
            post_responses=[fixtures.FakeResponse(403, {})],
            get_responses=[
                fixtures.FakeResponse(
                    200,
                    evidence_case._ensembl_population_payload(),
                ),
                fixtures.FakeResponse(
                    200,
                    evidence_case._ensembl_population_payload(),
                ),
            ],
        )
        circuit = ProviderCircuitState()
        candidate = evidence_case._candidate_with_rsid()

        results = [
            fetch_population_evidence_with_fallback(
                candidate,
                session=session,  # type: ignore[arg-type]
                circuit_state=circuit,
            )
            for _ in range(2)
        ]

        assert len(session.post_calls) == 1
        assert len(session.get_calls) == 2
        assert results[1]["primary_request_attempts"] == 0
        assert all(item["fallback_used"] is True for item in results)
        assert all(item["primary_failure"] == "forbidden" for item in results)
        assert all(item["source"] == "ensembl_variation" for item in results)
        evidence = build_evidence_object(
            _candidate_with_enrichment(candidate, population=results[0])
        )
        _final_report_asserts_degraded_source(
            evidence,
            capability="population_frequency",
            provider="ensembl_variation",
            method="exact_mapping_population_lookup",
            primary_failure="forbidden",
        )

    def test_clinvar_connection_failure_uses_derived_myvariant_once(
        self,
    ) -> None:
        annotation_case = fixtures.TestAnnotation()
        session = fixtures.FakeSession(
            [fixtures.FakeResponse(200, [annotation_case._vep_response()])],
            get_responses=[
                fixtures.FakeResponse(
                    200,
                    annotation_case._myvariant_clinvar_response(),
                )
            ],
            clinvar_responses=[requests.ConnectionError("private failure")],
        )

        annotation = annotate_variants(
            [annotation_case._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        clinvar = annotation["sources"]["clinvar"]
        assert clinvar["provider"] == "MyVariant.info"
        assert clinvar["independent_evidence"] is False
        assert clinvar["primary_failure"] == "unavailable"
        assert clinvar["clinical_significance"] == "Pathogenic"
        evidence = build_evidence_object(annotation)
        clinvar_lineage = [
            record
            for record in evidence["provenance"]["lineage"]
            if "ClinVar" in record["upstream_sources"]
        ]
        assert len(clinvar_lineage) == 1
        _final_report_asserts_degraded_source(
            evidence,
            capability="clinvar_evidence",
            provider="myvariant",
            method="derived_fallback",
            primary_failure="unavailable",
        )

    def test_litvar_server_error_retries_then_uses_europe_pmc(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            settings,
            "CONDITIONAL_ENRICHMENT_MAX_RETRIES",
            1,
        )
        monkeypatch.setattr(
            "backend.conditional_enrichment.time.sleep",
            lambda _delay: None,
        )
        candidate = fixtures.TestEvidenceObject._candidate_with_rsid()
        session = fixtures.FakeConditionalSession(
            get_responses=[
                fixtures.FakeResponse(500, {}),
                fixtures.FakeResponse(500, {}),
                fixtures.FakeResponse(
                    200,
                    {"resultList": {"result": [{"pmid": "123"}]}},
                ),
            ]
        )

        literature = fetch_literature_evidence(
            candidate,
            session=session,  # type: ignore[arg-type]
        )

        assert len(session.get_calls) == 3
        assert literature["providers"]["litvar"]["status"] == "unavailable"
        europe = literature["providers"]["europe_pmc"]
        assert europe["provider_role"] == "fallback"
        assert europe["primary_failure"] == "server_error"
        assert [item["pmid"] for item in literature["articles"]] == ["123"]
        evidence = build_evidence_object(
            _candidate_with_enrichment(candidate, literature=literature)
        )
        _final_report_asserts_degraded_source(
            evidence,
            capability="literature",
            provider="europe_pmc",
            method="bounded_literature_search_chain",
            primary_failure="server_error",
        )

    def test_europe_pmc_timeout_uses_pubmed_without_duplicate_article(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            settings,
            "CONDITIONAL_ENRICHMENT_MAX_RETRIES",
            0,
        )
        candidate = fixtures.TestEvidenceObject._candidate_with_rsid()
        session = fixtures.FakeConditionalSession(
            get_responses=[
                requests.Timeout("litvar unavailable"),
                requests.Timeout("europe unavailable"),
                fixtures.FakeResponse(
                    200,
                    {"esearchresult": {"idlist": ["456"]}},
                ),
                fixtures.FakeResponse(
                    200,
                    {"result": {"456": {"title": "Article"}}},
                ),
            ]
        )

        literature = fetch_literature_evidence(
            candidate,
            session=session,  # type: ignore[arg-type]
        )

        pubmed = literature["providers"]["pubmed"]
        assert pubmed["provider_role"] == "fallback"
        assert pubmed["fallback_for"] == "europe_pmc"
        assert pubmed["primary_failure"] == "timeout"
        assert [item["pmid"] for item in literature["articles"]] == ["456"]
        evidence = build_evidence_object(
            _candidate_with_enrichment(candidate, literature=literature)
        )
        _final_report_asserts_degraded_source(
            evidence,
            capability="literature",
            provider="pubmed",
            method="bounded_literature_search_chain",
            primary_failure="timeout",
        )

    def test_cspec_network_failure_uses_exact_local_cache(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            settings,
            "CSPEC_LKG_CACHE_PATH",
            tmp_path / "cspec_lkg.json",
        )
        annotation_case = fixtures.TestAnnotation()
        live_session = fixtures.FakeSession(
            [fixtures.FakeResponse(200, [annotation_case._vep_response()])],
            clingen_responses=[
                fixtures.FakeResponse(200, annotation_case._clingen_response())
            ],
            cspec_responses=[
                fixtures.FakeResponse(
                    200,
                    annotation_case._cspec_gene_response(),
                ),
                fixtures.FakeResponse(
                    200,
                    annotation_case._cspec_disease_response(),
                ),
            ],
        )
        live = annotate_variants(
            [annotation_case._variant()],
            session=live_session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]
        expected = deepcopy(live["sources"]["cspec"]["specifications"])
        outage_session = fixtures.FakeSession(
            [fixtures.FakeResponse(200, [annotation_case._vep_response()])],
            clingen_responses=[
                fixtures.FakeResponse(200, annotation_case._clingen_response())
            ],
            cspec_responses=[requests.ConnectionError("private outage")],
        )

        annotation = annotate_variants(
            [annotation_case._variant()],
            session=outage_session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        cspec = annotation["sources"]["cspec"]
        assert cspec["primary_failure"] == "unavailable"
        assert cspec["specifications"] == expected
        assert cspec["rule_logic_applied"] is False
        assert all("criteria" not in item for item in cspec["specifications"])
        evidence = build_evidence_object(annotation)
        _final_report_asserts_degraded_source(
            evidence,
            capability="cspec_context",
            provider="cached_cspec",
            method="last_known_good_cache",
            primary_failure="unavailable",
        )

    def test_vep_unavailable_uses_variantvalidator_without_consequence(
        self,
    ) -> None:
        annotation_case = fixtures.TestAnnotation()
        session = fixtures.FakeSession(
            [fixtures.FakeResponse(503, {"error": "unavailable"})],
            variantvalidator_responses=[
                fixtures.FakeResponse(
                    200,
                    annotation_case._variantvalidator_response(),
                )
            ],
        )

        annotation = annotate_variants(
            [annotation_case._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        vep = annotation["sources"]["vep"]
        assert vep["primary_failure"] == "server_error"
        assert vep["provider"] == "VariantValidator"
        assert annotation["consequence"] is None
        assert annotation["impact"] is None
        assert vep["transcript_consequences"] == []
        evidence = build_evidence_object(annotation)
        _final_report_asserts_degraded_source(
            evidence,
            capability="variant_annotation",
            provider="variantvalidator",
            method="validation_mapping_fallback",
            primary_failure="server_error",
        )
