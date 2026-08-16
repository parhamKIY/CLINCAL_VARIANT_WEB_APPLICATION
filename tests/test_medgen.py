"""Deterministic Stage 3 MedGen disease/HPO context coverage."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
import requests

from backend.medgen import enrich_with_medgen
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


def _variant(gene: str = "SCN1A", status: str = "unavailable") -> dict[str, Any]:
    return {
        "variant": {"chrom": "2", "pos": 166848839, "ref": "C", "alt": "T"},
        "assembly": "GRCh38", "gene": gene, "gene_id": None, "transcript": None,
        "consequence": None, "impact": None, "protein_change": None,
        "population_frequency": None, "warnings": [],
        "sources": {
            "vep": {"status": "success"}, "myvariant": {"status": "success"},
            "clinvar": {"status": "not_found", "conditions": []},
            "clingen": {"status": "not_found", "curations": []},
        },
        "mydisease": {"status": status, "diseases": []},
    }


def _search(ids: list[str]) -> dict[str, object]:
    return {"esearchresult": {"count": str(len(ids)), "idlist": ids}}


def _conceptmeta(
    *,
    genes: tuple[str, ...] = ("SCN1A",),
    hpo_id: str | None = None,
) -> str:
    gene_xml = "".join(f"<Gene>{gene}</Gene>" for gene in genes)
    hpo_xml = (
        f'<ClinicalFeatures><ClinicalFeature id="{hpo_id}" name="Seizure" />'
        "</ClinicalFeatures>"
        if hpo_id else ""
    )
    return (
        '<ConceptMeta><Names><Name SAB="OMIM" CODE="182389" '
        'SCUI="182389" TTY="PT">Condition</Name></Names>'
        f"<AssociatedGenes>{gene_xml}</AssociatedGenes>{hpo_xml}</ConceptMeta>"
    )


def _summary(
    *identifiers: str,
    genes: tuple[str, ...] = ("SCN1A",),
    hpo_id: str | None = None,
    include_conceptmeta: bool = True,
) -> dict[str, object]:
    return {
        "result": {
            "uids": list(identifiers),
            **{
                identifier: {
                    "uid": identifier, "conceptid": f"C{identifier}",
                    "title": f"Condition {identifier}", "sources": ["OMIM", "Orphanet"],
                    **(
                        {"conceptmeta": _conceptmeta(genes=genes, hpo_id=hpo_id)}
                        if include_conceptmeta else {}
                    ),
                }
                for identifier in identifiers
            },
        }
    }


def test_primary_sufficient_is_not_queried() -> None:
    item = _variant(status="available")
    item["mydisease"]["diseases"] = [{"disease_id": "MONDO:0000001"}]
    session = _Session([])
    result = enrich_with_medgen([item], session=session, enabled=True)
    context = result["variants"][0]["medgen_disease_hpo_context"]
    assert context["retrieval_state"] == "not_needed"
    assert context["enrichment_decision"]["reason_codes"] == ["primary_sufficient"]
    assert not session.requests


def test_unique_gene_query_is_deduplicated_and_fanned_out() -> None:
    session = _Session([_Response(200, _search(["101", "102"])), _Response(200, _summary("101", "102"))])
    result = enrich_with_medgen([_variant(), _variant(gene="scn1a")], session=session, enabled=True)
    contexts = [item["medgen_disease_hpo_context"] for item in result["variants"]]
    assert result["queried_gene_count"] == 1
    assert len(session.requests) == 2
    assert contexts[0]["records"] == contexts[1]["records"]
    assert contexts[0]["records"][0]["concept_id"] == "C101"
    assert contexts[0]["enrichment_decision"]["target_semantic_node"] == "disease_hpo_context"


def test_partial_primary_context_triggers_enrichment() -> None:
    result = enrich_with_medgen(
        [_variant(status="partial")],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101"))]),
        enabled=True,
    )
    decision = result["variants"][0]["medgen_disease_hpo_context"]["enrichment_decision"]
    assert decision["triggered"] is True
    assert "primary_required_fields_missing" in decision["reason_codes"]


@pytest.mark.parametrize("status", ["no_association", "not_found"])
def test_primary_valid_no_match_triggers_evidence_gap(status: str) -> None:
    result = enrich_with_medgen(
        [_variant(status=status)],
        session=_Session([_Response(200, _search([]))]), enabled=True,
    )
    decision = result["variants"][0]["medgen_disease_hpo_context"]["enrichment_decision"]
    assert decision["triggered"] is True
    assert decision["reason_codes"] == ["primary_no_match", "primary_required_fields_missing"]
    assert decision["required_fields_missing"] == ["diseases"]


def test_missing_gene_and_disabled_provider_are_not_triggered() -> None:
    missing_gene = enrich_with_medgen([_variant(gene="")], session=_Session([]), enabled=True)
    disabled = enrich_with_medgen([_variant()], session=_Session([]), enabled=False)
    assert missing_gene["variants"][0]["medgen_disease_hpo_context"]["enrichment_decision"]["reason_codes"] == ["missing_gene"]
    assert disabled["variants"][0]["medgen_disease_hpo_context"]["enrichment_decision"]["reason_codes"] == ["provider_disabled"]


def test_valid_no_match_and_provider_failures_remain_distinct() -> None:
    no_match = enrich_with_medgen([_variant()], session=_Session([_Response(200, _search([]))]), enabled=True)
    assert no_match["variants"][0]["medgen_disease_hpo_context"]["status"] == "no_match"

    forbidden = enrich_with_medgen([_variant()], session=_Session([_Response(403, {})]), enabled=True)
    assert forbidden["variants"][0]["medgen_disease_hpo_context"]["status"] == "forbidden"

    limited = enrich_with_medgen([_variant()], session=_Session([_Response(429, {}), _Response(429, {})]), enabled=True)
    assert limited["variants"][0]["medgen_disease_hpo_context"]["status"] == "rate_limited"

    timeout = enrich_with_medgen([_variant()], session=_Session([requests.Timeout(), requests.Timeout()]), enabled=True)
    assert timeout["variants"][0]["medgen_disease_hpo_context"]["status"] == "timeout"


@pytest.mark.parametrize(
    "responses",
    [
        [_Response(200, {"esearchresult": {"idlist": "not-a-list"}})],
        [_Response(200, _search(["101"])), _Response(200, {"result": {"uids": ["101"]}})],
    ],
)
def test_malformed_esearch_and_esummary_are_invalid_response(responses: list[object]) -> None:
    result = enrich_with_medgen([_variant()], session=_Session(responses), enabled=True)
    assert result["variants"][0]["medgen_disease_hpo_context"]["status"] == "invalid_response"


def test_schema_drift_is_invalid_response() -> None:
    result = enrich_with_medgen(
        [_variant()],
        session=_Session([_Response(200, _search(["101"])), _Response(200, {"result": {"101": "drift"}})]),
        enabled=True,
    )
    assert result["variants"][0]["medgen_disease_hpo_context"]["status"] == "invalid_response"


def test_exact_gene_association_is_required_and_rejections_are_diagnostic() -> None:
    exact = enrich_with_medgen(
        [_variant()],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101"))]),
        enabled=True,
    )["variants"][0]["medgen_disease_hpo_context"]
    assert exact["status"] == "success"
    assert exact["records"][0]["gene_association_match_state"] == "exact_gene_association"

    mismatch = enrich_with_medgen(
        [_variant()],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101", genes=("FBN1",)))]),
        enabled=True,
    )["variants"][0]["medgen_disease_hpo_context"]
    assert mismatch["status"] == "no_match"
    assert mismatch["records"] == []
    assert mismatch["candidate_diagnostics"][0]["gene_association_match_state"] == "gene_mismatch"

    unverified = enrich_with_medgen(
        [_variant()],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101", include_conceptmeta=False))]),
        enabled=True,
    )["variants"][0]["medgen_disease_hpo_context"]
    assert unverified["candidate_diagnostics"][0]["gene_association_match_state"] == "gene_association_unverified"


def test_accepted_hpo_context_and_conceptmeta_provenance_are_retained() -> None:
    context = enrich_with_medgen(
        [_variant()],
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101", hpo_id="HP:0001250"))]),
        enabled=True,
    )["variants"][0]["medgen_disease_hpo_context"]
    record = context["records"][0]
    assert context["queried_hpo_terms"] == [{"hpo_id": "HP:0001250", "label": "Seizure"}]
    assert record["matched_hpo_terms"] == [{"hpo_id": "HP:0001250", "label": "Seizure"}]
    assert record["hpo_match_method"] == "exact_hpo_id_conceptmeta"
    assert record["source_metadata"] == [{"database": "OMIM", "code": "182389", "scui": "182389", "term_type": "PT"}]


def test_no_accepted_hpo_prerequisite_keeps_gene_only_context() -> None:
    context = enrich_with_medgen(
        [_variant()], session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101"))]), enabled=True,
    )["variants"][0]["medgen_disease_hpo_context"]
    assert context["queried_hpo_terms"] == []
    assert context["records"][0]["hpo_match_method"] == "not_available"


def test_unmatched_accepted_hpo_is_not_fabricated() -> None:
    context = enrich_with_medgen(
        [_variant()],
        accepted_hpo_terms=[{"hpo_id": "HP:0001250", "label": "Seizure"}],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101", hpo_id="HP:0004322"))]),
        enabled=True,
    )["variants"][0]["medgen_disease_hpo_context"]
    assert context["records"][0]["matched_hpo_terms"] == []
    assert context["records"][0]["hpo_match_method"] == "not_available"


def test_serialized_context_is_source_separated_and_non_semantic() -> None:
    original = _variant()
    enriched = enrich_with_medgen(
        [original],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101"))]),
        enabled=True,
    )["variants"][0]
    evidence = build_evidence_object(enriched)
    context = evidence["phenotype_relationship"]["medgen_disease_hpo_context"]
    assert validate_evidence_object(evidence) == evidence
    assert context["provider_id"] == "ncbi_medgen"
    assert evidence["pathogenicity"]["automated_acmg_classification"] is None
    assert "rank" not in context
    assert "gene_disease_validity" not in evidence["pathogenicity"]
    assert enriched["mydisease"] == original["mydisease"]
    assert any(record["provider"] == "NCBI MedGen" for record in evidence["provenance"]["lineage"])


def test_exposed_upstream_source_is_retained_as_shared_lineage() -> None:
    item = _variant(status="partial")
    item["mydisease"]["diseases"] = [{"upstream_sources": ["OMIM"]}]
    enriched = enrich_with_medgen(
        [item],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101"))]),
        enabled=True,
    )["variants"][0]
    evidence = build_evidence_object(enriched)
    assert any(
        group["upstream_source"] == "OMIM"
        and "phenotype_relationship.medgen_disease_hpo_context" in group["evidence_paths"]
        for group in evidence["provenance"]["shared_upstream_groups"]
    )


def test_legacy_medgen_context_v1_remains_readable() -> None:
    enriched = enrich_with_medgen(
        [_variant()],
        session=_Session([_Response(200, _search(["101"])), _Response(200, _summary("101"))]),
        enabled=True,
    )["variants"][0]
    evidence = build_evidence_object(enriched)
    legacy = deepcopy(evidence)
    context = legacy["phenotype_relationship"]["medgen_disease_hpo_context"]
    assert isinstance(context, dict)
    context["schema_version"] = "1.0"
    context.pop("candidate_diagnostics")
    context.pop("queried_hpo_terms")
    records = context["records"]
    assert isinstance(records, list)
    for record in records:
        assert isinstance(record, dict)
        for field in (
            "gene_association_match_state", "gene_association_basis",
            "matched_hpo_terms", "hpo_match_method",
        ):
            record.pop(field)
        record["source_metadata"] = ["OMIM"]
    assert validate_evidence_object(legacy) == legacy
