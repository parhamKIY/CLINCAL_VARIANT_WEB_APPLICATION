"""Deterministic Stage 28 MyDisease.info provider tests."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
import requests

from backend.mydisease import (
    MYDISEASE_QUERY_FIELDS,
    clear_mydisease_cache,
    enrich_with_mydisease,
)
from backend.report import build_evidence_objects
from config import settings
from frontend.results import (
    build_mydisease_rows,
    build_monarch_rows,
    build_phenotype_rows,
)


pytestmark = [
    pytest.mark.stage43_testing_v2,
    pytest.mark.stage59_testing_v3,
]


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, FakeResponse)
        return response

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def isolate_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_mydisease_cache()
    monkeypatch.setattr(settings, "MYDISEASE_MAX_RETRIES", 0)
    monkeypatch.setattr(
        settings,
        "MYDISEASE_MAX_DISEASES_PER_GENE",
        20,
    )
    monkeypatch.setattr(
        settings,
        "MYDISEASE_MAX_HPO_TERMS_PER_DISEASE",
        50,
    )


def variant(
    gene: object = "SCN1A",
    hgnc_id: object = 10585,
) -> dict[str, object]:
    return {
        "variant": {
            "chrom": "2",
            "pos": 166848215,
            "ref": "C",
            "alt": "T",
        },
        "gene": gene,
        "phen2gene": {
            "availability": "available",
            "rank": 1,
            "score": 0.95,
        },
        "clinvar_significance": "Pathogenic",
        "sources": {
            "genebe": {
                "status": "success",
                "gene": gene,
                "gene_hgnc_id": hgnc_id,
                "acmg_classification": "Pathogenic",
            }
        },
    }


def metadata() -> FakeResponse:
    return FakeResponse(200, {"build_version": "20260720"})


def direct_hit(
    disease_id: str = "MONDO:0012320",
    *,
    gene_id: str = "HGNC:10585",
    hpo_terms: list[dict[str, object]] | None = None,
    score: float = 99.0,
) -> dict[str, object]:
    relation_id = gene_id.split(":", 1)[1]
    return {
        "_id": disease_id,
        "_score": score,
        "mondo": {
            "mondo": disease_id,
            "label": "familial hemiplegic migraine 3",
            "synonym": {
                "exact": ["SCN1A-related migraine"],
            },
            "has_material_basis_in_germline_mutation_in": {
                "http": [
                    "http://identifiers.org/hgnc/"
                    f"{relation_id}"
                ]
            },
            "xrefs": {
                "doid": ["DOID:0111198"],
                "omim": ["OMIM:609634"],
                "medgen": ["C1865322"],
                "gard": ["0018661"],
                "mesh": ["C565810"],
                "umls": ["C1865322"],
            },
        },
        "disease_ontology": {
            "doid": "DOID:0111198",
            "name": "familial hemiplegic migraine type 3",
            "xrefs": {"mim": ["609634"]},
        },
        "hpo": {
            "omim": "609634",
            "phenotype_related_to_disease": (
                hpo_terms
                if hpo_terms is not None
                else [
                    {
                        "hpo_id": "HP:0001250",
                        "hpo_name": "Seizure",
                        "evidence": "PCS",
                        "numeric_freq": 0.5,
                        "freq_numerator": 8,
                        "freq_denominator": 16,
                        "original_disease_id": "OMIM:609634",
                        "omim_refs": "OMIM:609634",
                        "pmid_refs": "PMID:16054936",
                        "biocuration": {
                            "name": "HPO:probinson",
                            "date": "2022-07-20",
                        },
                    }
                ]
            ),
            "inheritance": {
                "hpo_id": "HP:0000006",
                "hpo_name": "Autosomal dominant inheritance",
            },
            "clinical_course": {
                "hpo_id": "HP:0003621",
                "hpo_name": "Juvenile onset",
            },
        },
        "ctd": {
            "pathway_related_to_disease": {
                "inference_gene_symbol": "SCN1A",
                "kegg_pathway_id": "hsa04725",
                "pathway_name": "Cholinergic synapse",
            }
        },
        "umls": {"umls": ["C1865322"]},
    }


def query_response(*hits: object) -> FakeResponse:
    return FakeResponse(
        200,
        {"total": len(hits), "hits": list(hits)},
    )


def run(
    responses: list[object],
    *,
    variants: list[dict[str, object]] | None = None,
    hpo: list[str] | None = None,
) -> tuple[dict[str, object], FakeSession]:
    session = FakeSession(responses)
    result = enrich_with_mydisease(
        variants or [variant()],
        hpo or [],
        session=session,  # type: ignore[arg-type]
    )
    return result, session


def test_contract_query_and_complete_normalization() -> None:
    result, session = run(
        [metadata(), query_response(direct_hit())],
        hpo=["HP:0001250"],
    )

    evidence = result["variants"][0]["mydisease"]
    disease = evidence["diseases"][0]
    assert result["status"] == "available"
    assert evidence["provider"] == "MyDisease.info"
    assert evidence["provider_version"] == "20260720"
    assert evidence["query"] == "mondo.synonym.exact:SCN1A*"
    assert evidence["query_gene_id"] == "HGNC:10585"
    assert disease["disease_id"] == "MONDO:0012320"
    assert disease["cross_references"] == {
        "doid": ["DOID:0111198"],
        "omim": ["609634"],
        "medgen": ["C1865322"],
        "gard": ["0018661"],
        "mesh": ["C565810"],
        "umls": ["C1865322"],
    }
    relation = disease["gene_disease_relation"]
    assert relation["association_type"] == "direct_gene_disease"
    assert relation["matched_gene_id"] == "HGNC:10585"
    assert relation["upstream_source"] == "MONDO"
    phenotype = disease["supporting_hpo_terms"][0]
    assert phenotype["hpo_id"] == "HP:0001250"
    assert phenotype["evidence_code"] == "PCS"
    assert phenotype["numeric_frequency"] == 0.5
    assert phenotype["pmid_refs"] == ["PMID:16054936"]
    assert disease["matched_patient_hpo_terms"] == ["HP:0001250"]
    assert disease["phenotype_match_status"] == "exact_match"
    assert disease["inheritance"][0]["hpo_id"] == "HP:0000006"
    assert session.calls[0]["url"] == (
        "https://mydisease.info/v1/metadata"
    )
    assert session.calls[1]["url"] == (
        "https://mydisease.info/v1/query"
    )
    params = session.calls[1]["params"]
    assert params["q"] == "mondo.synonym.exact:SCN1A*"
    assert params["fields"] == ",".join(MYDISEASE_QUERY_FIELDS)
    assert params["from"] == 0
    assert params["size"] <= 100


def test_partial_and_no_exact_hpo_matching_across_diseases() -> None:
    second = direct_hit(
        "MONDO:0011461",
        hpo_terms=[
            {"hpo_id": "HP:0002123", "hpo_name": "Febrile seizure"}
        ],
        score=0.01,
    )
    result, _ = run(
        [metadata(), query_response(direct_hit(), second)],
        hpo=["HP:0001250", "HP:0002123", "HP:9999999"],
    )

    diseases = result["variants"][0]["mydisease"]["diseases"]
    statuses = {
        item["disease_id"]: item["phenotype_match_status"]
        for item in diseases
    }
    assert statuses == {
        "MONDO:0011461": "partial_match",
        "MONDO:0012320": "partial_match",
    }
    assert all(
        "HP:9999999" in item["unmatched_patient_hpo_terms"]
        for item in diseases
    )


def test_no_match_and_no_disease_hpo_annotations_are_missingness() -> None:
    no_hpo = direct_hit("MONDO:0011461", hpo_terms=[])
    result, _ = run(
        [metadata(), query_response(direct_hit(), no_hpo)],
        hpo=["HP:0009999"],
    )

    diseases = result["variants"][0]["mydisease"]["diseases"]
    statuses = {
        item["disease_id"]: item["phenotype_match_status"]
        for item in diseases
    }
    assert statuses["MONDO:0012320"] == "no_exact_match"
    assert statuses["MONDO:0011461"] == (
        "no_disease_hpo_annotations"
    )
    assert "unrelated" not in json.dumps(result).casefold()


def test_wrong_hgnc_text_only_and_ctd_are_not_direct() -> None:
    wrong = direct_hit(gene_id="HGNC:99999")
    wrong["mondo"]["definition"] = "SCN1A appears in free text"
    result, _ = run([metadata(), query_response(wrong)])

    evidence = result["variants"][0]["mydisease"]
    assert evidence["status"] == "no_association"
    assert evidence["diseases"] == []
    assert evidence["inferred_pathway_context"][0][
        "association_type"
    ] == "inferred_pathway_context"
    assert evidence["inferred_pathway_context"][0][
        "classification_effect"
    ] == "context_only"


def test_empty_response_is_successful_no_association() -> None:
    result, _ = run([metadata(), query_response()])

    evidence = result["variants"][0]["mydisease"]
    assert result["status"] == "no_association"
    assert evidence["status"] == "no_association"
    assert evidence["http_status"] == 200
    assert evidence["provider_total"] == 0
    assert result["unavailable_count"] == 0
    rows = build_phenotype_rows(result["variants"])
    assert rows[0]["MyDisease result"] == "no_association"


def test_unsupported_gene_is_not_queried() -> None:
    result, session = run(
        [metadata()],
        variants=[variant("SCN1A", None)],
    )

    evidence = result["variants"][0]["mydisease"]
    assert result["status"] == "unsupported"
    assert evidence["status"] == "unsupported"
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("failure", "status", "http_status"),
    [
        (requests.Timeout("private timeout"), "unavailable", None),
        (FakeResponse(400, {"error": "bad query"}), "unavailable", 400),
        (FakeResponse(503, {"error": "busy"}), "unavailable", 503),
        (FakeResponse(200, ValueError("private JSON")), "invalid_response", 200),
        (FakeResponse(200, {"unexpected": []}), "invalid_response", 200),
    ],
)
def test_failure_statuses_are_explicit(
    failure: object,
    status: str,
    http_status: int | None,
) -> None:
    result, _ = run([metadata(), failure])

    evidence = result["variants"][0]["mydisease"]
    assert evidence["status"] == status
    assert evidence["http_status"] == http_status
    assert evidence["diseases"] == []
    assert "private" not in json.dumps(result)


def test_retryable_error_retries_but_nonretryable_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MYDISEASE_MAX_RETRIES", 1)
    monkeypatch.setattr("backend.mydisease.time.sleep", lambda _: None)
    result, session = run(
        [
            metadata(),
            FakeResponse(503, {"error": "busy"}),
            query_response(direct_hit()),
        ]
    )
    assert result["status"] == "available"
    assert len(session.calls) == 3

    clear_mydisease_cache()
    result, session = run(
        [metadata(), FakeResponse(400, {"error": "bad"})]
    )
    assert result["status"] == "unavailable"
    assert len(session.calls) == 2


def test_duplicate_genes_query_once_and_cache_is_bounded_context() -> None:
    first = variant()
    second = deepcopy(first)
    second["variant"]["pos"] = 166848216
    result, session = run(
        [metadata(), query_response(direct_hit())],
        variants=[first, second],
    )
    assert len(session.calls) == 2
    assert [
        item["variant"]["pos"] for item in result["variants"]
    ] == [166848215, 166848216]
    assert all(
        item["mydisease"]["cache_state"] == "miss"
        for item in result["variants"]
    )

    cached, session = run([metadata()])
    assert len(session.calls) == 1
    assert cached["variants"][0]["mydisease"]["cache_state"] == "hit"


def test_disease_and_hpo_limits_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "MYDISEASE_MAX_DISEASES_PER_GENE",
        1,
    )
    monkeypatch.setattr(
        settings,
        "MYDISEASE_MAX_HPO_TERMS_PER_DISEASE",
        1,
    )
    first = direct_hit(
        "MONDO:0012320",
        hpo_terms=[
            {"hpo_id": "HP:0001250"},
            {"hpo_id": "HP:0002123"},
        ],
    )
    second = direct_hit("MONDO:0011461")
    result, _ = run(
        [metadata(), query_response(first, second)],
        hpo=["HP:0002123"],
    )

    diseases = result["variants"][0]["mydisease"]["diseases"]
    assert len(diseases) == 1
    assert diseases[0]["disease_id"] == "MONDO:0012320"
    assert len(diseases[0]["supporting_hpo_terms"]) == 1


def test_no_ranking_score_or_classification_mutation() -> None:
    original = variant()
    result, _ = run(
        [
            metadata(),
            query_response(
                direct_hit("MONDO:0012320", score=-100),
            ),
        ],
        variants=[original],
    )

    enriched = result["variants"][0]
    assert enriched["phen2gene"] == original["phen2gene"]
    assert enriched["clinvar_significance"] == "Pathogenic"
    assert enriched["sources"]["genebe"]["acmg_classification"] == (
        "Pathogenic"
    )
    assert "_score" not in json.dumps(result)
    assert "rank" not in enriched["mydisease"]


def test_failure_isolated_per_gene_and_preserves_previous_evidence() -> None:
    other = variant("MTHFR", 7436)
    result, _ = run(
        [
            metadata(),
            query_response(direct_hit()),
            requests.ConnectionError("private failure"),
        ],
        variants=[variant(), other],
    )

    assert result["status"] == "partial"
    assert [item["gene"] for item in result["variants"]] == [
        "SCN1A",
        "MTHFR",
    ]
    assert result["variants"][0]["mydisease"]["status"] == "available"
    assert result["variants"][1]["mydisease"]["status"] == "unavailable"
    assert all("phen2gene" in item for item in result["variants"])


def test_raw_response_is_not_stored_or_forwarded_to_evidence_object() -> None:
    result, _ = run([metadata(), query_response(direct_hit())])
    serialized = json.dumps(result)
    assert "_score" not in serialized
    assert "raw_api_payload" not in serialized

    candidate = deepcopy(result["variants"][0])
    candidate.update(
        {
            "assembly": "GRCh38",
            "gene_id": "ENSG00000144285",
            "transcript": "ENST00000303395",
            "consequence": "missense_variant",
            "impact": "MODERATE",
            "protein_change": "p.Ala100Thr",
            "population_frequency": 0.0001,
            "references": [],
            "warnings": [],
            "phenotype_score": 1.0,
            "phenotype_match_count": 1,
            "matched_hpo_terms": ["HP:0001250"],
            "hpo_terms": ["HP:0001250"],
        }
    )
    candidate["sources"] = {
        "vep": {"status": "success"},
        "myvariant": {"status": "success"},
        "clinvar": {
            "status": "success",
            "accession": "VCV000012345",
            "clinical_significance": "Pathogenic",
            "review_status": "reviewed by expert panel",
            "conditions": [],
        },
        "clingen": {
            "status": "not_found",
            "curations": [],
        },
    }
    evidence = build_evidence_objects([candidate])[0]
    assert "mydisease" not in evidence
    mydisease = evidence["phenotype_relationship"]["mydisease"]
    assert mydisease["diseases"][0]["disease_id"] == (
        "MONDO:0012320"
    )
    assert "query" not in mydisease
    assert '"_score"' not in json.dumps(evidence)


def test_active_and_legacy_display_namespaces_are_not_relabelled() -> None:
    result, _ = run([metadata(), query_response(direct_hit())])
    assert build_mydisease_rows(result["variants"])[0][
        "Disease ID"
    ] == "MONDO:0012320"

    legacy = deepcopy(result["variants"][0])
    legacy["monarch"] = {
        "provider_version": "legacy-release",
        "associations": [
            {
                "gene_id": "HGNC:10585",
                "disease_id": "MONDO:0005027",
                "disease_name": "legacy disease",
                "direction": "direct",
            }
        ],
    }
    rows = build_monarch_rows([legacy])
    assert rows[0]["Disease ID"] == "MONDO:0005027"
    assert rows[0]["Monarch release"] == "legacy-release"
    assert "mydisease" in legacy
    assert "monarch" in legacy
