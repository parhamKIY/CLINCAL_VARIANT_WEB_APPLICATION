"""Deterministic Stage 2 coverage for exact ERepo retrieval."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import requests

from backend.erepo import retrieve_expert_curated_context
from backend.provider_resilience import ProviderCircuitState
from backend.reference_model import build_reference_model_v2
from backend.report import build_evidence_object, validate_evidence_object


FIXTURES = Path(__file__).parent / "fixtures" / "erepo"
EXACT_HGVS = "NC_000012.12:g.102894804T>A"


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return deepcopy(self._payload)


class _Session:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.requests: list[tuple[str, dict[str, object] | None]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> _Response:
        self.requests.append((url, params))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, _Response)
        return result


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _bundle(*, include_secondary: bool = False) -> dict[str, Any]:
    provenance = [
        {
            "identifier_type": "genomic_hgv",
            "value": EXACT_HGVS,
            "source": "input_normalization",
            "scope": "allele",
            "validation": "deterministic_normalization",
        }
    ]
    clinvar_ids: list[str] = []
    transcript_hgvs: list[str] = []
    if include_secondary:
        clinvar_ids = ["102645"]
        transcript_hgvs = ["NM_000277.3:c.283A>T"]
        provenance.extend(
            (
                {
                    "identifier_type": "clinvar_variation_id",
                    "value": "102645",
                    "source": "NCBI ClinVar",
                    "scope": "allele",
                    "validation": "provider_exact_allele",
                },
                {
                    "identifier_type": "transcript_hgv",
                    "value": "NM_000277.3:c.283A>T",
                    "source": "Ensembl VEP",
                    "scope": "transcript",
                    "validation": "provider_transcript_annotation",
                },
            )
        )
    return {
        "schema_version": "1.0",
        "input_index": 0,
        "genome_build": "GRCh38",
        "chromosome": "12",
        "position": 102894804,
        "reference": "T",
        "alternate": "A",
        "minimal_representation_status": "not_needed",
        "left_normalization_status": "not_required",
        "genomic_hgvs": [EXACT_HGVS],
        "transcript_hgvs": transcript_hgvs,
        "protein_hgvs": [],
        "transcript_ids": [],
        "gene_symbol": None,
        "hgnc_id": None,
        "rsids": [],
        "clinvar_variation_ids": clinvar_ids,
        "vcv_accessions": [],
        "rcv_accessions": [],
        "mondo_ids": [],
        "omim_ids": [],
        "medgen_ids": [],
        "provenance": provenance,
    }


def _retrieve(session: _Session, bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    return retrieve_expert_curated_context(
        _bundle() if bundle is None else bundle,
        session=session,  # type: ignore[arg-type]
        circuit_state=ProviderCircuitState(),
        base_url="https://erepo.example",
        timeout=1,
        max_retries=0,
    )


def test_exact_success_is_detail_verified_and_source_attributed() -> None:
    session = _Session(
        [
            _Response(200, _fixture("summary_classification_exact_success_ca229507.json")),
            _Response(200, _fixture("detail_classification_exact_success_ca229507.json")),
        ]
    )

    result = _retrieve(session)

    context = result["expert_curated_variant_context"]
    assert result["status"] == "success"
    assert context["retrieval_state"] == "accepted_records"
    assert context["records"][0]["acceptance_state"] == "EXACT_MATCH"
    assert context["records"][0]["ca_id"] == "CA229507"
    assert context["capability_result"]["provider"] == "clingen_erepo"
    assert session.requests[0][1] == {
        "columns": "hgvs", "values": EXACT_HGVS, "matchTypes": "exact",
        "matchMode": "and", "pg": 1, "pgSize": 10,
    }
    assert "variant" not in context["records"][0]


def test_one_no_match_continues_and_all_exact_no_matches_are_final() -> None:
    no_match = _fixture("summary_classification_exact_no_match.json")
    session = _Session([_Response(404, no_match) for _ in range(3)])

    result = _retrieve(session, _bundle(include_secondary=True))

    context = result["expert_curated_variant_context"]
    assert result["status"] == "no_match"
    assert context["retrieval_state"] == "no_match"
    assert [item["status"] for item in context["strategy_results"]] == [
        "no_match", "no_match", "no_match",
    ]
    assert len(session.requests) == 3


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (_Response(403, {"status": {"code": 403}}), "forbidden"),
        (_Response(429, {"status": {"code": 429}}), "rate_limited"),
        (requests.Timeout(), "timeout"),
    ],
)
def test_operational_failures_are_retained_not_converted_to_no_match(
    response: object,
    expected_status: str,
) -> None:
    result = _retrieve(_Session([response]))

    context = result["expert_curated_variant_context"]
    assert result["status"] == expected_status
    assert context["retrieval_state"] == "operational_failure"
    assert not context["records"]


def test_malformed_success_and_identity_mismatch_are_not_no_match() -> None:
    malformed = {"data": [], "metadata": {}, "status": {"code": 200, "name": "OK"}}
    malformed_result = _retrieve(_Session([_Response(200, malformed)]))
    assert malformed_result["status"] == "invalid_response"

    mismatch = _fixture("erepo_exact_identity_cases.json")["cases"][4]["response"]
    mismatch_result = _retrieve(_Session([_Response(200, mismatch)]))
    context = mismatch_result["expert_curated_variant_context"]
    assert mismatch_result["status"] == "unavailable"
    assert context["retrieval_state"] == "identity_mismatch"
    assert context["strategy_results"][0]["candidate_rejections"] == ["assembly_mismatch"]


def test_detail_uuid_and_version_mismatches_are_rejected() -> None:
    summary = _fixture("summary_classification_exact_success_ca229507.json")
    detail = _fixture("detail_classification_exact_success_ca229507.json")
    detail["data"]["uuid"] = "wrong-uuid"
    uuid_result = _retrieve(_Session([_Response(200, summary), _Response(200, detail)]))
    assert uuid_result["expert_curated_variant_context"]["strategy_results"][0]["candidate_rejections"] == ["detail_uuid_mismatch"]

    version_detail = _fixture("detail_classification_exact_success_ca229507.json")
    version_detail["data"]["metadata"]["version"] = "1.0.1"
    version_result = _retrieve(_Session([_Response(200, summary), _Response(200, version_detail)]))
    assert version_result["expert_curated_variant_context"]["strategy_results"][0]["candidate_rejections"] == ["detail_version_mismatch"]


def test_deterministically_normalized_padded_indel_is_retained_as_equivalent() -> None:
    matrix = _fixture("erepo_exact_identity_cases.json")["cases"][5]
    bundle = _bundle()
    bundle.update(
        {
            "chromosome": "1", "position": 100, "reference": "AT",
            "alternate": "A", "minimal_representation_status": "applied",
            "left_normalization_status": "unverified_without_reference",
            "genomic_hgvs": ["NC_000001.11:g.101del"],
            "provenance": [
                {
                    "identifier_type": "genomic_hgv",
                    "value": "NC_000001.11:g.101del",
                    "source": "input_normalization",
                    "scope": "allele",
                    "validation": "deterministic_normalization",
                }
            ],
        }
    )
    detail = _fixture("detail_classification_exact_success_ca229507.json")
    detail["data"]["uuid"] = "deterministic-indel-fixture-uuid"
    detail["data"]["metadata"]["version"] = "1.0.0"
    detail["data"]["@id"] = (
        "https://cgerepoapi/evrepo/api/summary/classification/"
        "deterministic-indel-fixture-uuid/doc/sepio/version/1.0.0"
    )
    result = _retrieve(
        _Session([_Response(200, matrix["response"]), _Response(200, detail)]),
        bundle,
    )

    assert result["expert_curated_variant_context"]["records"][0]["acceptance_state"] == (
        "EXACT_MATCH_EQUIVALENT_REPRESENTATION"
    )
    assert result["expert_curated_variant_context"]["records"][0]["normalized_identity"] == {
        "schema_version": "1.0", "assembly": "GRCh38", "chromosome": "1",
        "position": 101, "reference": "T", "alternate": "",
        "normalizer": "backend.annotation._normalize_variant_edit",
        "normalization_provenance": "deterministic_normalization",
        "representation_basis": "deterministically_trimmed_vcf_padding",
    }
    equivalent_candidate = {
        "variant": {"chrom": "1", "pos": 100, "ref": "AT", "alt": "A"},
        "assembly": "GRCh38", "gene": None, "gene_id": None, "transcript": None,
        "consequence": None, "impact": None, "protein_change": None,
        "population_frequency": None, "warnings": [],
        "sources": {
            "vep": {"status": "success"}, "myvariant": {"status": "success"},
            "clinvar": {"status": "not_found", "conditions": []},
            "clingen": {"status": "not_found", "curations": []}, "erepo": result,
        },
    }
    equivalent_evidence = build_evidence_object(equivalent_candidate)
    assert validate_evidence_object(equivalent_evidence) == equivalent_evidence


def test_clinvar_id_and_transcript_discovery_are_detail_verified() -> None:
    no_match = _fixture("summary_classification_exact_no_match.json")
    summary = _fixture("summary_classification_exact_success_ca229507.json")
    detail = _fixture("detail_classification_exact_success_ca229507.json")
    cv_session = _Session([_Response(404, no_match), _Response(200, summary), _Response(200, detail)])
    cv_result = _retrieve(cv_session, _bundle(include_secondary=True))
    assert cv_result["expert_curated_variant_context"]["records"][0]["query_strategy"] == "exact_clinvar_variation_id"
    assert cv_session.requests[1][1]["columns"] == "cvId"

    transcript_bundle = _bundle(include_secondary=True)
    transcript_bundle["clinvar_variation_ids"] = []
    transcript_bundle["provenance"] = [
        item for item in transcript_bundle["provenance"]
        if item["identifier_type"] != "clinvar_variation_id"
    ]
    transcript_session = _Session([_Response(404, no_match), _Response(200, summary), _Response(200, detail)])
    transcript_result = _retrieve(transcript_session, transcript_bundle)
    assert transcript_result["expert_curated_variant_context"]["records"][0]["query_strategy"] == "exact_transcript_hgvs"
    assert transcript_session.requests[1][1]["values"] == "NM_000277.3:c.283A>T"


def _summary_with_hgvs(hgvs: list[str]) -> dict[str, Any]:
    summary = _fixture("summary_classification_exact_success_ca229507.json")
    summary["data"][0]["hgvs"] = hgvs
    return summary


@pytest.mark.parametrize(
    ("hgvs", "reason"),
    [
        (["NM_000277.3:c.283A>T"], "hgvs_mismatch"),
        (["NC_000012.12:g.102894805T>A"], "coordinate_mismatch"),
        (["NC_000012.12:g.102894804C>A"], "reference_mismatch"),
        (["NC_000012.12:g.102894804T>C"], "alternate_mismatch"),
    ],
)
def test_candidate_rejections_have_distinct_deterministic_reasons(
    hgvs: list[str], reason: str
) -> None:
    result = _retrieve(_Session([_Response(200, _summary_with_hgvs(hgvs))]))
    assert result["expert_curated_variant_context"]["strategy_results"][0]["candidate_rejections"] == [reason]


def test_retracted_schema_drift_and_unresolved_indel_representation_are_rejected() -> None:
    retracted = _summary_with_hgvs([EXACT_HGVS])
    retracted["data"][0]["retracted"] = True
    retracted_result = _retrieve(_Session([_Response(200, retracted)]))
    assert retracted_result["expert_curated_variant_context"]["strategy_results"][0]["candidate_rejections"] == ["retracted_record"]

    schema_drift = _fixture("summary_classification_exact_success_ca229507.json")
    schema_drift["status"] = {"code": 200, "name": "CHANGED"}
    schema_result = _retrieve(_Session([_Response(200, schema_drift)]))
    assert schema_result["status"] == "invalid_response"

    indel = _bundle()
    indel.update({
        "chromosome": "1", "position": 100, "reference": "AT", "alternate": "A",
        "minimal_representation_status": "applied",
        "left_normalization_status": "unverified_without_reference",
        "genomic_hgvs": ["NC_000001.11:g.101del"],
        "provenance": [{"identifier_type": "genomic_hgv", "value": "NC_000001.11:g.101del", "source": "input_normalization", "scope": "allele", "validation": "deterministic_normalization"}],
    })
    unresolved = _summary_with_hgvs(["NC_000001.11:g.100del"])
    unresolved_result = _retrieve(_Session([_Response(200, unresolved)]), indel)
    assert unresolved_result["expert_curated_variant_context"]["strategy_results"][0]["candidate_rejections"] == ["representation_unresolved"]


def test_multiple_condition_separated_records_are_retained_after_detail_verification() -> None:
    summary = _fixture("summary_classification_exact_success_ca229507.json")
    second = deepcopy(summary["data"][0])
    second.update({"uuid": "second-condition-uuid", "caId": "CA000000002", "condition": "second condition"})
    summary["data"].append(second)
    first_detail = _fixture("detail_classification_exact_success_ca229507.json")
    second_detail = deepcopy(first_detail)
    second_detail["data"]["uuid"] = "second-condition-uuid"
    second_detail["data"]["@id"] = second_detail["data"]["@id"].replace(
        "c61fa227-893e-4be0-9e02-2d7c1494af20", "second-condition-uuid"
    )
    result = _retrieve(_Session([
        _Response(200, summary), _Response(200, first_detail), _Response(200, second_detail),
    ]))
    records = result["expert_curated_variant_context"]["records"]
    assert [(record["ca_id"], record["condition"]) for record in records] == [
        ("CA229507", "phenylketonuria"), ("CA000000002", "second condition"),
    ]


def test_bundle_hgvs_not_independently_derivable_is_not_queried() -> None:
    bundle = _bundle()
    bundle["genomic_hgvs"] = ["NC_000012.12:g.102894805T>A"]
    bundle["provenance"][0]["value"] = bundle["genomic_hgvs"][0]
    session = _Session([])
    result = _retrieve(session, bundle)
    assert result["status"] == "not_applicable"
    assert result["expert_curated_variant_context"]["retrieval_state"] == "identity_unverifiable"
    assert not session.requests


def test_identity_incomplete_input_is_unattempted_without_http() -> None:
    bundle = _bundle()
    bundle["genomic_hgvs"] = []
    bundle["provenance"] = []
    session = _Session([])

    result = _retrieve(session, bundle)

    assert result["status"] == "not_applicable"
    assert result["expert_curated_variant_context"]["retrieval_state"] == "unattempted"
    assert not session.requests


def test_context_is_persistent_source_attributed_and_conflict_retained() -> None:
    erepo = _retrieve(
        _Session(
            [
                _Response(200, _fixture("summary_classification_exact_success_ca229507.json")),
                _Response(200, _fixture("detail_classification_exact_success_ca229507.json")),
            ]
        )
    )
    candidate = {
        "variant": {"chrom": "12", "pos": 102894804, "ref": "T", "alt": "A"},
        "assembly": "GRCh38",
        "gene": "PAH",
        "gene_id": None,
        "transcript": None,
        "consequence": None,
        "impact": None,
        "protein_change": None,
        "population_frequency": None,
        "sources": {
            "vep": {"status": "success"},
            "myvariant": {"status": "success"},
            "clinvar": {
                "status": "success", "clinical_significance": "Benign",
                "review_status": None, "conditions": [],
            },
            "clingen": {"status": "success", "curations": []},
            "erepo": erepo,
        },
        "warnings": [],
    }

    evidence = build_evidence_object(candidate)

    assert validate_evidence_object(evidence) == evidence
    context = evidence["pathogenicity"]["expert_curated_variant_context"]
    assert context["records"][0]["classification"] == "Likely Pathogenic"
    assert evidence["pathogenicity"]["automated_acmg_classification"] is None
    assert any(
        item["source"] == "ClinGen ERepo"
        for item in evidence["conflict_audit"]["pre_review"]["normalized_classifications"]
    )
    assert any(
        item["source"] == "ClinGen ERepo"
        and item["capability"] == "expert_curated_variant_context"
        for item in build_reference_model_v2(evidence)["data_sources"]
    )
