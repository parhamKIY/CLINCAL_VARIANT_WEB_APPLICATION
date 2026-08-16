"""Deterministic tests for Stage 5 NCBI MedGen gene-disease supporting evidence."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.medgen import (
    MEDGEN_GENE_DISEASE_SCHEMA_VERSION,
    _clingen_primary_state,
    _is_local_gene_disease_support_sufficient,
    enrich_with_medgen_gene_disease,
)
from backend.report import (
    EvidenceObjectError,
    build_evidence_object,
    validate_evidence_object,
)


pytestmark = pytest.mark.stage59_testing_v3


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------

SAMPLE_CONCEPTMETA_SCN1A = (
    "<ConceptMeta>"
    "  <AssociatedGenes>"
    "    <AssociatedGene symbol='SCN1A' />"
    "  </AssociatedGenes>"
    "  <ClinicalFeatures>"
    "    <ClinicalFeature sdui='HP:0001250' name='Seizure' />"
    "  </ClinicalFeatures>"
    "  <Sources>"
    "    <Source sab='OMIM' code='607208' />"
    "    <Source sab='Orphanet' code='ORPHA:33069' />"
    "    <Source sab='MONDO' code='MONDO:0011933' />"
    "  </Sources>"
    "</ConceptMeta>"
)

SAMPLE_CONCEPTMETA_SCN1B = (
    "<ConceptMeta>"
    "  <AssociatedGenes>"
    "    <AssociatedGene symbol='SCN1B' />"
    "  </AssociatedGenes>"
    "  <Sources>"
    "    <Source sab='OMIM' code='600235' />"
    "  </Sources>"
    "</ConceptMeta>"
)

SAMPLE_CONCEPTMETA_NO_GENES = (
    "<ConceptMeta>"
    "  <ClinicalFeatures>"
    "    <ClinicalFeature sdui='HP:0001250' name='Seizure' />"
    "  </ClinicalFeatures>"
    "</ConceptMeta>"
)


def _variant(
    *,
    gene: str | None = "SCN1A",
    clingen_status: str = "unavailable",
    curations: list[dict[str, Any]] | None = None,
    local_support: dict[str, Any] | None = None,
) -> dict[str, Any]:
    v = {
        "variant": {
            "chrom": "2",
            "pos": 166848650,
            "ref": "C",
            "alt": "T",
        },
        "chrom": "2",
        "pos": 166848650,
        "ref": "C",
        "alt": "T",
        "assembly": "GRCh38",
        "gene": gene,
        "gene_id": None,
        "transcript": None,
        "consequence": None,
        "impact": None,
        "protein_change": None,
        "population_frequency": None,
        "warnings": [],
        "sources": {
            "vep": {"status": "success"},
            "myvariant": {"status": "success"},
            "clinvar": {"status": "not_found", "conditions": []},
            "clingen": {
                "status": clingen_status,
                "curations": curations or [],
            },
        },
        "phen2gene": {
            "availability": "available",
            "gene": gene,
            "gene_id": None,
            "rank": None,
            "score": None,
            "status": None,
            "hpo_terms": ["HP:0001250"],
            "weight_model": "sk",
            "provider": "Phen2Gene",
            "provider_version": None,
            "retrieved_at": "2026-08-16T12:00:00Z",
            "cache_hit": False,
            "warnings": [],
        },
        "mydisease": {"status": "available", "diseases": []},
    }
    if local_support is not None:
        v["local_gene_disease_support"] = local_support
    return v


def _mock_response(status_code: int, json_data: dict[str, Any] | None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    if json_data is not None:
        resp._content = json.dumps(json_data).encode("utf-8")
        resp.headers["content-type"] = "application/json"
    else:
        resp._content = b""
    return resp


def _make_search_payload(uids: list[str]) -> dict[str, Any]:
    return {"esearchresult": {"idlist": uids}}


def _make_summary_payload(
    uids: list[str],
    titles: dict[str, str] | None = None,
    conceptmetas: dict[str, str] | None = None,
) -> dict[str, Any]:
    titles = titles or {}
    conceptmetas = conceptmetas or {}
    result: dict[str, Any] = {"uids": uids}
    for uid in uids:
        result[uid] = {
            "uid": uid,
            "title": titles.get(uid, f"Condition for {uid}"),
            "conceptid": f"C{uid.zfill(7)}",
            "semantic_type": "Disease or Syndrome",
            "definition": f"Definition for {uid}",
            "conceptmeta": conceptmetas.get(uid, SAMPLE_CONCEPTMETA_SCN1A),
            "sources": ["OMIM", "Orphanet"],
        }
    return {"result": result}


# ---------------------------------------------------------------------------
# Unit Tests: Primary State & Target-Node Sufficiency Semantics
# ---------------------------------------------------------------------------

class TestClinGenPrimaryStateAndSufficiency:
    def test_clingen_validity_available_without_support_triggers_medgen(self) -> None:
        """GenCC validity available must NOT by itself satisfy gene_disease_support."""
        clingen = {
            "status": "success",
            "curations": [
                {
                    "disease": "Dravet syndrome",
                    "classification": "Definitive",
                    "submitter": "ClinGen",
                }
            ],
        }
        item = {"gene": "SCN1A", "sources": {"clingen": clingen}}
        state, triggered, reasons, missing = _clingen_primary_state(clingen, item)
        assert state == "available"
        # Separate semantic node: gene_disease_support remains insufficient!
        assert triggered is True
        assert reasons == ["primary_validity_available", "gene_disease_support_gap"]
        assert missing == ["gene_disease_support"]

    def test_local_support_sufficient_suppresses_medgen_when_validity_available(self) -> None:
        """When local semantically valid support is present, MedGen is NOT_NEEDED."""
        clingen = {
            "status": "success",
            "curations": [
                {"disease": "Dravet syndrome", "classification": "Definitive", "submitter": "ClinGen"}
            ],
        }
        item = {
            "gene": "SCN1A",
            "sources": {"clingen": clingen},
            "local_gene_disease_support": {
                "status": "available",
                "records": [{"disease": "Dravet syndrome", "gene": "SCN1A"}],
            },
        }
        state, triggered, reasons, missing = _clingen_primary_state(clingen, item)
        assert state == "available"
        assert triggered is False
        assert reasons == ["primary_validity_available", "local_support_sufficient"]
        assert missing == []

    def test_local_support_sufficient_suppresses_medgen_when_validity_unavailable(self) -> None:
        """When local support is present and GenCC is unavailable, MedGen is NOT_NEEDED."""
        clingen = {"status": "unavailable", "primary_failure": "500 Server Error"}
        item = {
            "gene": "SCN1A",
            "sources": {"clingen": clingen},
            "local_gene_disease_support": {
                "status": "available",
                "records": [{"disease": "Dravet syndrome", "gene": "SCN1A"}],
            },
        }
        state, triggered, reasons, missing = _clingen_primary_state(clingen, item)
        assert state == "unavailable"
        assert triggered is False
        assert reasons == ["primary_operational_failure", "local_support_sufficient"]
        assert missing == []

    def test_clingen_operational_failure_triggers_medgen(self) -> None:
        clingen = {"status": "unavailable", "primary_failure": "500 Server Error"}
        state, triggered, reasons, missing = _clingen_primary_state(clingen)
        assert state == "unavailable"
        assert triggered is True
        assert reasons == ["primary_operational_failure", "gene_disease_support_gap"]
        assert missing == ["gene_disease_support"]

    def test_clingen_valid_no_match_triggers_medgen_without_negative_evidence(self) -> None:
        clingen = {"status": "not_found", "curations": []}
        state, triggered, reasons, missing = _clingen_primary_state(clingen)
        assert state == "no_match"
        assert triggered is True
        assert reasons == ["primary_no_match", "gene_disease_support_gap"]
        assert missing == ["gene_disease_support"]

    def test_clingen_partial_triggers_medgen(self) -> None:
        clingen = {"status": "partial", "curations": []}
        state, triggered, reasons, missing = _clingen_primary_state(clingen)
        assert state == "partial"
        assert triggered is True
        assert reasons == ["primary_partial", "gene_disease_support_gap"]
        assert missing == ["gene_disease_support"]


# ---------------------------------------------------------------------------
# Unit Tests: enrich_with_medgen_gene_disease & No-Match vs Rejection Semantics
# ---------------------------------------------------------------------------

class TestEnrichWithMedGenGeneDisease:
    @patch("backend.medgen.requests.Session.get")
    def test_enrich_gene_disease_success(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = [
            _mock_response(200, _make_search_payload(["1001", "1002"])),
            _mock_response(
                200,
                _make_summary_payload(
                    ["1001", "1002"],
                    titles={"1001": "Dravet syndrome", "1002": "GEFS+"},
                    conceptmetas={
                        "1001": SAMPLE_CONCEPTMETA_SCN1A,
                        "1002": SAMPLE_CONCEPTMETA_SCN1A,
                    },
                ),
            ),
        ]

        variants = [_variant(gene="SCN1A", clingen_status="unavailable")]

        result = enrich_with_medgen_gene_disease(variants)
        assert result["status"] == "success"
        assert result["queried_gene_count"] == 1
        assert len(result["variants"]) == 1

        gd = result["variants"][0]["medgen_gene_disease_context"]
        assert gd["schema_version"] == MEDGEN_GENE_DISEASE_SCHEMA_VERSION
        assert gd["provider_id"] == "ncbi_medgen"
        assert gd["provider_role"] == "supporting"
        assert gd["primary_provider"] == "clingen"
        assert gd["status"] == "success"
        assert gd["query_gene"] == "SCN1A"
        assert len(gd["records"]) == 2
        assert gd["records"][0]["title"] == "Dravet syndrome"
        assert gd["records"][0]["gene_association_match_state"] == "exact_gene_association"
        assert "OMIM:607208" in gd["records"][0]["upstream_sources"]

    @patch("backend.medgen.requests.Session.get")
    def test_true_no_match_when_esearch_empty(self, mock_get: MagicMock) -> None:
        """Case A: ESearch returns 0 UIDs -> valid no_match with no diagnostics."""
        mock_get.side_effect = [
            _mock_response(200, _make_search_payload([])),
        ]

        variants = [_variant(gene="NONEXISTENTGENE", clingen_status="not_found")]

        result = enrich_with_medgen_gene_disease(variants)
        assert result["status"] == "no_match"

        gd = result["variants"][0]["medgen_gene_disease_context"]
        assert gd["status"] == "no_match"
        assert gd["retrieval_state"] == "no_match"
        assert gd["records"] == []
        assert gd["candidate_diagnostics"] == []

    @patch("backend.medgen.requests.Session.get")
    def test_rejected_candidates_when_conceptmeta_fails_exact_gene(self, mock_get: MagicMock) -> None:
        """Case B: ESearch returns candidates but none pass exact-gene verification."""
        mock_get.side_effect = [
            _mock_response(200, _make_search_payload(["1002", "1003"])),
            _mock_response(
                200,
                _make_summary_payload(
                    ["1002", "1003"],
                    titles={
                        "1002": "Brugada syndrome",
                        "1003": "Generic epilepsy",
                    },
                    conceptmetas={
                        "1002": SAMPLE_CONCEPTMETA_SCN1B,  # mismatch (SCN1B vs SCN1A)
                        "1003": SAMPLE_CONCEPTMETA_NO_GENES,  # unverified (no AssociatedGenes)
                    },
                ),
            ),
        ]

        variants = [_variant(gene="SCN1A", clingen_status="not_found")]

        result = enrich_with_medgen_gene_disease(variants)
        assert result["status"] == "no_match"

        gd = result["variants"][0]["medgen_gene_disease_context"]
        assert gd["status"] == "no_match"
        assert gd["retrieval_state"] == "no_verified_gene_association"
        assert gd["records"] == []

        # Diagnostics contain the rejected concepts
        assert len(gd["candidate_diagnostics"]) == 2
        diag_uids = {d["medgen_uid"]: d for d in gd["candidate_diagnostics"]}
        assert diag_uids["1002"]["gene_association_match_state"] == "gene_mismatch"
        assert diag_uids["1002"]["rejection_reason"] == "conceptmeta_associated_genes_no_exact_match"
        assert diag_uids["1003"]["gene_association_match_state"] == "gene_association_unverified"
        assert diag_uids["1003"]["rejection_reason"] == "conceptmeta_associated_genes_unavailable"

    @patch("backend.medgen.requests.Session.get")
    def test_multi_variant_same_gene_deduplication(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = [
            # SCN1A
            _mock_response(200, _make_search_payload(["1001"])),
            _mock_response(
                200,
                _make_summary_payload(
                    ["1001"],
                    titles={"1001": "Dravet syndrome"},
                    conceptmetas={"1001": SAMPLE_CONCEPTMETA_SCN1A},
                ),
            ),
            # FBN1
            _mock_response(200, _make_search_payload(["2001"])),
            _mock_response(
                200,
                _make_summary_payload(
                    ["2001"],
                    titles={"2001": "Marfan syndrome"},
                    conceptmetas={
                        "2001": (
                            "<ConceptMeta>"
                            "  <AssociatedGenes><AssociatedGene symbol='FBN1' /></AssociatedGenes>"
                            "  <Sources><Source sab='OMIM' code='154700' /></Sources>"
                            "</ConceptMeta>"
                        )
                    },
                ),
            ),
        ]

        variants = [
            _variant(gene="SCN1A", clingen_status="unavailable"),
            _variant(gene="FBN1", clingen_status="unavailable"),
            _variant(gene="SCN1A", clingen_status="unavailable"),
            _variant(gene="SCN1A", clingen_status="unavailable"),
        ]

        result = enrich_with_medgen_gene_disease(variants)
        assert result["status"] == "success"
        # Exactly 2 unique queries (SCN1A, FBN1)
        assert result["queried_gene_count"] == 2
        assert mock_get.call_count == 4  # 2 esearch + 2 esummary

        # Input order and cardinality preserved
        assert len(result["variants"]) == 4
        assert result["variants"][0]["medgen_gene_disease_context"]["query_gene"] == "SCN1A"
        assert result["variants"][1]["medgen_gene_disease_context"]["query_gene"] == "FBN1"
        assert result["variants"][2]["medgen_gene_disease_context"]["query_gene"] == "SCN1A"
        assert result["variants"][3]["medgen_gene_disease_context"]["query_gene"] == "SCN1A"
        assert result["variants"][0]["medgen_gene_disease_context"]["records"][0]["title"] == "Dravet syndrome"
        assert result["variants"][1]["medgen_gene_disease_context"]["records"][0]["title"] == "Marfan syndrome"

    @patch("backend.medgen.requests.Session.get")
    def test_local_support_sufficient_suppresses_network_calls(self, mock_get: MagicMock) -> None:
        variants = [
            _variant(
                gene="SCN1A",
                clingen_status="success",
                curations=[{"disease": "Dravet syndrome", "classification": "Definitive"}],
                local_support={"status": "available", "records": [{"disease": "Dravet syndrome"}]},
            )
        ]

        result = enrich_with_medgen_gene_disease(variants)
        assert result["queried_gene_count"] == 0
        assert mock_get.call_count == 0
        gd = result["variants"][0]["medgen_gene_disease_context"]
        assert gd["retrieval_state"] == "not_needed"
        assert gd["enrichment_decision"]["triggered"] is False
        assert gd["enrichment_decision"]["reason_codes"] == ["primary_validity_available", "local_support_sufficient"]

    def test_missing_gene_unattempted(self) -> None:
        variants = [_variant(gene=None, clingen_status="unavailable")]
        result = enrich_with_medgen_gene_disease(variants)
        gd = result["variants"][0]["medgen_gene_disease_context"]
        assert gd["status"] == "not_applicable"
        assert gd["retrieval_state"] == "unattempted"
        assert gd["enrichment_decision"]["reason_codes"] == ["missing_gene"]
        assert gd["enrichment_decision"]["required_fields_missing"] == ["gene"]

    def test_disabled_unattempted(self) -> None:
        variants = [_variant(gene="SCN1A", clingen_status="unavailable")]
        result = enrich_with_medgen_gene_disease(variants, enabled=False)
        gd = result["variants"][0]["medgen_gene_disease_context"]
        assert gd["status"] == "not_applicable"
        assert gd["retrieval_state"] == "unattempted"
        assert gd["enrichment_decision"]["reason_codes"] == ["provider_disabled"]


# ---------------------------------------------------------------------------
# Unit Tests: Semantic Isolation, Correlation & EvidenceObject Validation
# ---------------------------------------------------------------------------

class TestSemanticIsolationAndEvidenceObject:
    @patch("backend.medgen.requests.Session.get")
    def test_semantic_boundaries_not_violated(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = [
            _mock_response(200, _make_search_payload(["1001"])),
            _mock_response(
                200,
                _make_summary_payload(
                    ["1001"],
                    titles={"1001": "Dravet syndrome"},
                    conceptmetas={"1001": SAMPLE_CONCEPTMETA_SCN1A},
                ),
            ),
        ]

        candidate = _variant(gene="SCN1A", clingen_status="unavailable")

        # Enrich variant
        enrich_result = enrich_with_medgen_gene_disease([candidate])
        enriched_candidate = enrich_result["variants"][0]

        # 1. Verify sources.clingen was not mutated or given fabricated validity
        assert enriched_candidate["sources"]["clingen"]["status"] == "unavailable"
        assert enriched_candidate["sources"]["clingen"]["curations"] == []

        # 2. Build EvidenceObject
        evidence = build_evidence_object(enriched_candidate)

        # 3. Validate EvidenceObject schema
        validate_evidence_object(evidence)

        # 4. Check pathogenicity section: clingen_context remains empty, medgen_gene_disease_context is populated
        pathogenicity = evidence["pathogenicity"]
        assert pathogenicity["clingen_context"] == []
        assert "medgen_gene_disease_context" in pathogenicity
        gd = pathogenicity["medgen_gene_disease_context"]
        assert gd["schema_version"] == "1.0"
        assert gd["query_gene"] == "SCN1A"
        assert len(gd["records"]) == 1
        assert gd["records"][0]["title"] == "Dravet syndrome"

        # 5. Check lineage record
        lineage = evidence["provenance"]["lineage"]
        gd_lineage = next(
            (r for r in lineage if r["evidence_path"] == "pathogenicity.medgen_gene_disease_context"),
            None,
        )
        assert gd_lineage is not None
        assert gd_lineage["provider"] == "NCBI MedGen"
        assert gd_lineage["evidence_present"] is True

    def test_historical_evidence_object_without_medgen_gene_disease_is_valid(self) -> None:
        candidate = _variant(gene="SCN1A", clingen_status="not_found")
        evidence = build_evidence_object(candidate)
        assert "medgen_gene_disease_context" not in evidence["pathogenicity"]
        # Must pass schema validation
        validate_evidence_object(evidence)

    @patch("backend.medgen.requests.Session.get")
    def test_exact_upstream_identifier_overlap_creates_shared_correlation(self, mock_get: MagicMock) -> None:
        """Exact identifier overlap (e.g. OMIM:607208) creates a shared upstream group."""
        mock_get.side_effect = [
            _mock_response(200, _make_search_payload(["1001"])),
            _mock_response(
                200,
                _make_summary_payload(
                    ["1001"],
                    titles={"1001": "Dravet syndrome"},
                    conceptmetas={"1001": SAMPLE_CONCEPTMETA_SCN1A},  # contains OMIM:607208
                ),
            ),
        ]

        candidate = _variant(gene="SCN1A", clingen_status="unavailable")
        # Add another provider with exact same deterministic identifier OMIM:607208
        candidate["mydisease"]["diseases"] = [{"upstream_sources": ["OMIM:607208"]}]

        enrich_result = enrich_with_medgen_gene_disease([candidate])
        evidence = build_evidence_object(enrich_result["variants"][0])

        shared_groups = evidence["provenance"]["shared_upstream_groups"]
        omim_group = next(
            (g for g in shared_groups if g["upstream_source"] == "OMIM:607208"),
            None,
        )
        assert omim_group is not None
        assert "pathogenicity.medgen_gene_disease_context" in omim_group["evidence_paths"]
        assert "phenotype_relationship.mydisease" in omim_group["evidence_paths"]

    @patch("backend.medgen.requests.Session.get")
    def test_same_database_without_same_identifier_does_not_create_shared_correlation(self, mock_get: MagicMock) -> None:
        """Same database name without same identifier does NOT claim shared biological upstream evidence."""
        mock_get.side_effect = [
            _mock_response(200, _make_search_payload(["1001"])),
            _mock_response(
                200,
                _make_summary_payload(
                    ["1001"],
                    titles={"1001": "Dravet syndrome"},
                    conceptmetas={"1001": SAMPLE_CONCEPTMETA_SCN1A},  # contains OMIM:607208
                ),
            ),
        ]

        candidate = _variant(gene="SCN1A", clingen_status="unavailable")
        # Add another provider with a DIFFERENT OMIM identifier OMIM:123456
        candidate["mydisease"]["diseases"] = [{"upstream_sources": ["OMIM:123456"]}]

        enrich_result = enrich_with_medgen_gene_disease([candidate])
        evidence = build_evidence_object(enrich_result["variants"][0])

        shared_groups = evidence["provenance"]["shared_upstream_groups"]
        # Neither OMIM:607208 nor OMIM:123456 should have a shared group (no overlap)
        assert not any("OMIM" in g["upstream_source"] for g in shared_groups)
