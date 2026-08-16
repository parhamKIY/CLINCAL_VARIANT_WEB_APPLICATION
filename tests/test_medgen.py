"""Deterministic Stage 3 MedGen disease/HPO context coverage."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
import requests

from backend.medgen import enrich_with_medgen
from backend.report import build_evidence_object, validate_evidence_object


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


def _summary(*identifiers: str) -> dict[str, object]:
    return {
        "result": {
            "uids": list(identifiers),
            **{
                identifier: {
                    "uid": identifier, "conceptid": f"C{identifier}",
                    "title": f"Condition {identifier}", "sources": ["OMIM", "Orphanet"],
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


def test_serialized_context_is_source_separated_and_non_semantic() -> None:
    enriched = enrich_with_medgen(
        [_variant()],
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
