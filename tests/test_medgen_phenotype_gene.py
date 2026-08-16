"""Deterministic Stage 4 MedGen phenotype-gene supporting evidence coverage."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
import requests

from backend.medgen import enrich_with_medgen_phenotype_gene
from backend.report import build_evidence_object, validate_evidence_object


pytestmark = pytest.mark.stage59_testing_v3


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        return deepcopy(self.payload)


class _Session:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, *, params: dict[str, object], **_kwargs: object) -> _Response:
        self.requests.append((url, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, _Response)
        return response


def _variant(
    gene: str = "SCN1A",
    availability: str = "unavailable",
) -> dict[str, Any]:
    return {
        "variant": {"chrom": "2", "pos": 166848839, "ref": "C", "alt": "T"},
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
            "clingen": {"status": "not_found", "curations": []},
        },
        "phen2gene": {
            "availability": availability,
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


def _search(ids: list[str]) -> dict[str, object]:
    return {"esearchresult": {"count": str(len(ids)), "idlist": ids}}


def _conceptmeta(
    *,
    genes: tuple[str, ...] = ("SCN1A",),
    hpo_id: str | None = None,
    hpo_name: str = "Seizure",
) -> str:
    gene_xml = "".join(f"<Gene>{gene}</Gene>" for gene in genes)
    hpo_xml = (
        f'<ClinicalFeatures><ClinicalFeature id="{hpo_id}" name="{hpo_name}" />'
        "</ClinicalFeatures>"
        if hpo_id
        else ""
    )
    return (
        '<ConceptMeta><Names><Name SAB="OMIM" CODE="607208" '
        'SCUI="607208" TTY="PT">Dravet syndrome</Name></Names>'
        f"<AssociatedGenes>{gene_xml}</AssociatedGenes>{hpo_xml}</ConceptMeta>"
    )


def _summary(
    *identifiers: str,
    genes: tuple[str, ...] = ("SCN1A",),
    hpo_id: str | None = None,
    hpo_name: str = "Seizure",
    include_conceptmeta: bool = True,
) -> dict[str, object]:
    return {
        "result": {
            "uids": list(identifiers),
            **{
                identifier: {
                    "uid": identifier,
                    "conceptid": f"C{identifier}",
                    "title": f"Condition {identifier}",
                    "sources": ["OMIM", "Orphanet"],
                    **(
                        {
                            "conceptmeta": _conceptmeta(
                                genes=genes,
                                hpo_id=hpo_id,
                                hpo_name=hpo_name,
                            )
                        }
                        if include_conceptmeta
                        else {}
                    ),
                }
                for identifier in identifiers
            },
        }
    }


def test_primary_sufficient_is_not_queried() -> None:
    item = _variant(availability="available")
    session = _Session([])
    result = enrich_with_medgen_phenotype_gene(
        [item],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    context = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert context["retrieval_state"] == "not_needed"
    assert context["enrichment_decision"]["reason_codes"] == ["primary_sufficient"]
    assert not session.requests


def test_unique_gene_hpo_query_is_deduplicated_and_fanned_out() -> None:
    session = _Session([
        _Response(200, _search(["101", "102"])),
        _Response(200, _summary("101", "102", hpo_id="HP:0001250")),
    ])
    result = enrich_with_medgen_phenotype_gene(
        [_variant(), _variant(gene="scn1a")],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    contexts = [
        item["medgen_phenotype_gene_context"][0]
        for item in result["variants"]
    ]
    assert result["queried_tuple_count"] == 1
    assert len(session.requests) == 2
    assert contexts[0]["records"] == contexts[1]["records"]
    assert contexts[0]["records"][0]["concept_id"] == "C101"
    assert contexts[0]["enrichment_decision"]["target_semantic_node"] == "phenotype_gene_context"


def test_partial_primary_evidence_triggers_enrichment() -> None:
    result = enrich_with_medgen_phenotype_gene(
        [_variant(availability="partial")],
        session=_Session([
            _Response(200, _search(["101"])),
            _Response(200, _summary("101", hpo_id="HP:0001250")),
        ]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    decision = result["variants"][0]["medgen_phenotype_gene_context"][0]["enrichment_decision"]
    assert decision["triggered"] is True
    assert "primary_partial" in decision["reason_codes"]


def test_primary_operational_failure_triggers_enrichment() -> None:
    result = enrich_with_medgen_phenotype_gene(
        [_variant(availability="unavailable")],
        session=_Session([
            _Response(200, _search(["101"])),
            _Response(200, _summary("101", hpo_id="HP:0001250")),
        ]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    decision = result["variants"][0]["medgen_phenotype_gene_context"][0]["enrichment_decision"]
    assert decision["triggered"] is True
    assert "primary_operational_failure" in decision["reason_codes"]


def test_missing_gene_and_disabled_provider_are_not_triggered() -> None:
    missing_gene = enrich_with_medgen_phenotype_gene(
        [_variant(gene="")],
        session=_Session([]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    disabled = enrich_with_medgen_phenotype_gene(
        [_variant()],
        session=_Session([]),
        enabled=False,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    assert missing_gene["variants"][0]["medgen_phenotype_gene_context"][0]["enrichment_decision"]["reason_codes"] == ["missing_gene"]
    assert disabled["variants"][0]["medgen_phenotype_gene_context"][0]["enrichment_decision"]["reason_codes"] == ["provider_disabled"]


def test_no_accepted_hpo_prerequisite_not_met() -> None:
    no_hpo = enrich_with_medgen_phenotype_gene(
        [_variant()],
        session=_Session([]),
        enabled=True,
        accepted_hpo_terms=[],
    )
    ctx = no_hpo["variants"][0]["medgen_phenotype_gene_context"][0]
    assert ctx["retrieval_state"] == "prerequisite_not_met"
    assert ctx["enrichment_decision"]["reason_codes"] == ["no_accepted_hpo_with_label"]
    assert ctx["enrichment_decision"]["triggered"] is False


def test_scn1a_seizure_support_match() -> None:
    result = enrich_with_medgen_phenotype_gene(
        [_variant(gene="SCN1A")],
        session=_Session([
            _Response(200, _search(["607208"])),
            _Response(200, _summary("607208", genes=("SCN1A",), hpo_id="HP:0001250")),
        ]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    ctx = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert ctx["status"] == "success"
    assert ctx["retrieval_state"] == "accepted_records"
    assert len(ctx["records"]) == 1
    assert ctx["records"][0]["gene_association_match_state"] == "exact_gene_association"
    assert ctx["records"][0]["hpo_match_method"] == "exact_hpo_id_conceptmeta"
    assert ctx["records"][0]["matched_hpo_terms"] == [{"hpo_id": "HP:0001250", "label": "Seizure"}]


def test_fbn1_ectopia_lentis_multi_hpo_support() -> None:
    session = _Session([
        # Query 1: FBN1 + Ectopia lentis
        _Response(200, _search(["154700"])),
        _Response(200, _summary("154700", genes=("FBN1",), hpo_id="HP:0001083", hpo_name="Ectopia lentis")),
        # Query 2: FBN1 + Tall stature
        _Response(200, _search(["154700"])),
        _Response(200, _summary("154700", genes=("FBN1",), hpo_id="HP:0000098", hpo_name="Tall stature")),
    ])
    result = enrich_with_medgen_phenotype_gene(
        [_variant(gene="FBN1")],
        session=session,
        enabled=True,
        accepted_hpo_terms=[
            {"hpo_id": "HP:0001083", "label": "Ectopia lentis"},
            {"hpo_id": "HP:0000098", "label": "Tall stature"},
        ],
    )
    variant_contexts = result["variants"][0]["medgen_phenotype_gene_context"]
    assert len(variant_contexts) == 2
    assert variant_contexts[0]["query_hpo_id"] == "HP:0001083"
    assert variant_contexts[0]["status"] == "success"
    assert variant_contexts[1]["query_hpo_id"] == "HP:0000098"
    assert variant_contexts[1]["status"] == "success"


def test_cftr_malabsorption_valid_no_match() -> None:
    result = enrich_with_medgen_phenotype_gene(
        [_variant(gene="CFTR")],
        session=_Session([_Response(200, _search([]))]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0002024", "label": "Malabsorption"}],
    )
    ctx = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert ctx["status"] == "no_match"
    assert ctx["retrieval_state"] == "no_match"
    assert ctx["records"] == []
    assert result["status"] == "no_match"


def test_medgen_timeout_and_errors_remain_distinct() -> None:
    no_match = enrich_with_medgen_phenotype_gene(
        [_variant()],
        session=_Session([_Response(200, _search([]))]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    assert no_match["variants"][0]["medgen_phenotype_gene_context"][0]["status"] == "no_match"

    forbidden = enrich_with_medgen_phenotype_gene(
        [_variant()],
        session=_Session([_Response(403, {})]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    assert forbidden["variants"][0]["medgen_phenotype_gene_context"][0]["status"] == "forbidden"

    limited = enrich_with_medgen_phenotype_gene(
        [_variant()],
        session=_Session([_Response(429, {}), _Response(429, {})]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    assert limited["variants"][0]["medgen_phenotype_gene_context"][0]["status"] == "rate_limited"

    timeout = enrich_with_medgen_phenotype_gene(
        [_variant()],
        session=_Session([requests.Timeout(), requests.Timeout()]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    assert timeout["variants"][0]["medgen_phenotype_gene_context"][0]["status"] == "timeout"


@pytest.mark.parametrize(
    "responses",
    [
        [_Response(200, {"esearchresult": {"idlist": "not-a-list"}})],
        [_Response(200, _search(["101"])), _Response(200, {"result": {"uids": ["101"]}})],
    ],
)
def test_malformed_esearch_and_esummary_are_invalid_response(
    responses: list[object],
) -> None:
    result = enrich_with_medgen_phenotype_gene(
        [_variant()],
        session=_Session(responses),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    assert result["variants"][0]["medgen_phenotype_gene_context"][0]["status"] == "invalid_response"


def test_exact_gene_association_required_and_mismatch_rejected() -> None:
    mismatch = enrich_with_medgen_phenotype_gene(
        [_variant(gene="SCN1A")],
        session=_Session([
            _Response(200, _search(["101"])),
            _Response(200, _summary("101", genes=("FBN1",), hpo_id="HP:0001250")),
        ]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )["variants"][0]["medgen_phenotype_gene_context"][0]
    assert mismatch["status"] == "no_match"
    assert mismatch["records"] == []
    assert mismatch["candidate_diagnostics"][0]["gene_association_match_state"] == "gene_mismatch"

    unverified = enrich_with_medgen_phenotype_gene(
        [_variant(gene="SCN1A")],
        session=_Session([
            _Response(200, _search(["101"])),
            _Response(200, _summary("101", include_conceptmeta=False)),
        ]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )["variants"][0]["medgen_phenotype_gene_context"][0]
    assert unverified["candidate_diagnostics"][0]["gene_association_match_state"] == "gene_association_unverified"


def test_serialized_context_is_source_separated_and_non_semantic() -> None:
    original = _variant()
    enriched = enrich_with_medgen_phenotype_gene(
        [original],
        session=_Session([
            _Response(200, _search(["101"])),
            _Response(200, _summary("101", hpo_id="HP:0001250")),
        ]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )["variants"][0]
    evidence = build_evidence_object(enriched)
    assert validate_evidence_object(evidence) == evidence
    pg_context = evidence["phenotype_relationship"]["medgen_phenotype_gene_context"]
    assert isinstance(pg_context, list)
    assert len(pg_context) == 1
    assert pg_context[0]["provider_id"] == "ncbi_medgen"
    assert pg_context[0]["primary_provider"] == "phen2gene"
    # Semantic boundaries: no Phen2Gene rank/score modification
    assert evidence["phenotype_relationship"]["phen2gene"]["rank"] is None
    assert evidence["phenotype_relationship"]["phen2gene"]["score"] is None
    assert evidence["pathogenicity"]["automated_acmg_classification"] is None
    assert "rank" not in pg_context[0]
    assert "gene_disease_validity" not in evidence["pathogenicity"]


def test_lineage_and_shared_upstream_sources_retained() -> None:
    item = _variant()
    item["mydisease"]["status"] = "partial"
    item["mydisease"]["diseases"] = [{"upstream_sources": ["OMIM"]}]
    enriched = enrich_with_medgen_phenotype_gene(
        [item],
        session=_Session([
            _Response(200, _search(["101"])),
            _Response(200, _summary("101", hpo_id="HP:0001250")),
        ]),
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )["variants"][0]
    evidence = build_evidence_object(enriched)
    lineage = evidence["provenance"]["lineage"]
    assert any(
        record["evidence_path"].startswith("phenotype_relationship.medgen_phenotype_gene_context")
        for record in lineage
    )
    shared_groups = evidence["provenance"]["shared_upstream_groups"]
    assert any(
        group["upstream_source"] == "OMIM"
        and any(
            p.startswith("phenotype_relationship.medgen_phenotype_gene_context")
            for p in group["evidence_paths"]
        )
        for group in shared_groups
    )


def test_historical_evidence_object_without_medgen_pg_remains_valid() -> None:
    original = _variant()
    evidence = build_evidence_object(original)
    assert "medgen_phenotype_gene_context" not in evidence["phenotype_relationship"]
    assert validate_evidence_object(evidence) == evidence


# ---------------------------------------------------------------------------
# Corrective Tests: Phen2Gene valid no-match & Target-node sufficiency
# ---------------------------------------------------------------------------

def test_phen2gene_valid_no_match_triggers_medgen_enrichment() -> None:
    """A valid Phen2Gene no-match is an explicit evidence gap that triggers MedGen."""
    item = _variant(gene="SCN1A", availability="no_match")
    item["phen2gene"]["status"] = "not_found"
    item["matched_patient_hpo_terms"] = []
    item["local_phenotype_score"] = None
    session = _Session([
        _Response(200, _search(["607208"])),
        _Response(200, _summary("607208", genes=("SCN1A",), hpo_id="HP:0001250")),
    ])
    result = enrich_with_medgen_phenotype_gene(
        [item],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    context = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert context["status"] == "success"
    assert context["retrieval_state"] == "accepted_records"
    decision = context["enrichment_decision"]
    assert decision["triggered"] is True
    assert decision["primary_retrieval_state"] == "no_match"
    assert "primary_no_match" in decision["reason_codes"]
    assert "phenotype_gene_support_gap" in decision["reason_codes"]
    assert decision["required_fields_missing"] == ["phenotype_gene_support"]
    assert len(context["records"]) == 1


def test_phen2gene_unavailable_local_support_sufficient_not_triggered() -> None:
    """When Phen2Gene fails operationally but local support is sufficient, MedGen is NOT_NEEDED."""
    item = _variant(gene="SCN1A", availability="unavailable")
    # Provide sufficient local phenotype-gene support on the variant
    item["matched_patient_hpo_terms"] = ["HP:0001250"]
    item["local_phenotype_score"] = 0.8
    session = _Session([])
    result = enrich_with_medgen_phenotype_gene(
        [item],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    context = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert context["status"] == "not_applicable"
    assert context["retrieval_state"] == "not_needed"
    decision = context["enrichment_decision"]
    assert decision["triggered"] is False
    assert decision["primary_retrieval_state"] == "unavailable"
    assert "local_support_sufficient" in decision["reason_codes"]
    assert not session.requests


def test_phen2gene_unavailable_local_support_insufficient_triggered() -> None:
    """When Phen2Gene fails and local support is insufficient, MedGen is triggered."""
    item = _variant(gene="SCN1A", availability="unavailable")
    item["matched_patient_hpo_terms"] = []
    item["local_phenotype_score"] = None
    session = _Session([
        _Response(200, _search(["607208"])),
        _Response(200, _summary("607208", genes=("SCN1A",), hpo_id="HP:0001250")),
    ])
    result = enrich_with_medgen_phenotype_gene(
        [item],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    context = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert context["status"] == "success"
    decision = context["enrichment_decision"]
    assert decision["triggered"] is True
    assert "primary_operational_failure" in decision["reason_codes"]
    assert "phenotype_gene_support_gap" in decision["reason_codes"]


def test_phen2gene_valid_no_match_support_node_insufficient_triggered() -> None:
    """When Phen2Gene reports no match and local support is empty, MedGen is triggered."""
    item = _variant(gene="SCN1A", availability="no_match")
    item["matched_patient_hpo_terms"] = []
    session = _Session([
        _Response(200, _search(["607208"])),
        _Response(200, _summary("607208", genes=("SCN1A",), hpo_id="HP:0001250")),
    ])
    result = enrich_with_medgen_phenotype_gene(
        [item],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    context = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert context["status"] == "success"
    assert context["enrichment_decision"]["triggered"] is True
    assert "primary_no_match" in context["enrichment_decision"]["reason_codes"]


def test_phen2gene_fallback_match_is_sufficient_and_does_not_fabricate_ranking() -> None:
    """Local HPO-gene fallback match sufficiently populates support; rank/score are not fabricated."""
    item = _variant(gene="SCN1A", availability="available")
    item["phen2gene"] = {
        "availability": "available",
        "gene": "SCN1A",
        "gene_id": None,
        "rank": 1,
        "score": 0.5,
        "status": "direct_match",
        "hpo_terms": ["HP:0001250"],
        "weight_model": "not_applicable",
        "provider": "local_hpo_gene_fallback",
        "provider_version": None,
        "retrieved_at": "2026-08-16T12:00:00Z",
        "cache_hit": False,
        "warnings": [],
        "fallback_used": True,
        "matched_hpos": ["HP:0001250"],
    }
    session = _Session([])
    result = enrich_with_medgen_phenotype_gene(
        [item],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    context = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert context["retrieval_state"] == "not_needed"
    assert context["enrichment_decision"]["triggered"] is False
    assert "local_support_sufficient" in context["enrichment_decision"]["reason_codes"]
    assert not session.requests

    evidence = build_evidence_object(result["variants"][0])
    assert evidence["phenotype_relationship"]["phen2gene"]["provider"] == "local_hpo_gene_fallback"
    assert evidence["phenotype_relationship"]["phen2gene"]["weight_model"] == "not_applicable"


def test_same_query_gene_hpo_deduplicated_across_multiple_variants_order_preserved() -> None:
    """Same normalized (gene, HPO) is queried once and fanned back with preserved order and cardinality."""
    v1 = _variant(gene="SCN1A")
    v1["variant"]["pos"] = 100
    v2 = _variant(gene="SCN1A")
    v2["variant"]["pos"] = 200
    v3 = _variant(gene="FBN1")
    v3["variant"]["pos"] = 300
    v4 = _variant(gene="SCN1A")
    v4["variant"]["pos"] = 400

    session = _Session([
        # 1st unique tuple: (scn1a, HP:0001250)
        _Response(200, _search(["607208"])),
        _Response(200, _summary("607208", genes=("SCN1A",), hpo_id="HP:0001250")),
        # 2nd unique tuple: (fbn1, HP:0001250) - mismatch in conceptmeta
        _Response(200, _search(["154700"])),
        _Response(200, _summary("154700", genes=("FBN1",), hpo_id="HP:0001083")),
    ])

    result = enrich_with_medgen_phenotype_gene(
        [v1, v2, v3, v4],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )

    # Dedup verification: exactly 2 tuples queried (SCN1A, FBN1)
    assert result["queried_tuple_count"] == 2
    assert len(session.requests) == 4  # 2 esearch + 2 esummary

    # Cardinality & order verification
    variants = result["variants"]
    assert len(variants) == 4
    assert [v["variant"]["pos"] for v in variants] == [100, 200, 300, 400]
    assert [v["gene"] for v in variants] == ["SCN1A", "SCN1A", "FBN1", "SCN1A"]

    # Content verification: SCN1A variants (0, 1, 3) have identical accepted records
    ctx1 = variants[0]["medgen_phenotype_gene_context"][0]
    ctx2 = variants[1]["medgen_phenotype_gene_context"][0]
    ctx3 = variants[2]["medgen_phenotype_gene_context"][0]
    ctx4 = variants[3]["medgen_phenotype_gene_context"][0]

    assert ctx1["status"] == "success"
    assert ctx1["records"] == ctx2["records"] == ctx4["records"]
    assert len(ctx1["records"]) == 1
    assert ctx1["records"][0]["concept_id"] == "C607208"

    # FBN1 variant (2) has its own distinct context
    assert ctx3["query_gene"] == "FBN1"


def test_multi_root_conceptmeta_and_sdui_attribute_parsing() -> None:
    """Multi-root ConceptMeta XML and SDUI HPO attribute are parsed deterministically."""
    # Live MedGen XML snippet for SCN1A UID 400655
    raw_multi_root_conceptmeta = (
        '<Names><Name SAB="OMIM" CODE="609634">MIGRAINE, FAMILIAL HEMIPLEGIC, 3</Name></Names>'
        '<AssociatedGenes><Gene gene_id="6323" chromosome="2">SCN1A</Gene></AssociatedGenes>'
        '<ClinicalFeatures>'
        '<ClinicalFeature uid="20693" CUI="C0036572" TUI="T184" SDUI="HP:0001250" />'
        '</ClinicalFeatures>'
    )
    session = _Session([
        _Response(200, _search(["400655"])),
        _Response(200, {
            "result": {
                "uids": ["400655"],
                "400655": {
                    "uid": "400655",
                    "conceptid": "C1864987",
                    "title": "Migraine, familial hemiplegic, 3",
                    "sources": ["OMIM", "GTR"],
                    "conceptmeta": raw_multi_root_conceptmeta,
                },
            }
        }),
    ])
    result = enrich_with_medgen_phenotype_gene(
        [_variant(gene="SCN1A")],
        session=session,
        enabled=True,
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
    )
    ctx = result["variants"][0]["medgen_phenotype_gene_context"][0]
    assert ctx["status"] == "success"
    assert ctx["retrieval_state"] == "accepted_records"
    assert len(ctx["records"]) == 1
    record = ctx["records"][0]
    assert record["concept_id"] == "C1864987"
    assert record["gene_association_match_state"] == "exact_gene_association"
    assert record["hpo_match_method"] == "exact_hpo_id_conceptmeta"
    assert record["matched_hpo_terms"] == [{"hpo_id": "HP:0001250", "label": "Seizure"}]


