"""Deterministic guardrails for the Stage 1 ERepo contract and fixtures."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "erepo"


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _case_map(matrix: dict[str, object]) -> dict[str, dict[str, object]]:
    cases = matrix["cases"]
    assert isinstance(cases, list)
    return {
        case["id"]: case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("id"), str)
    }


def test_stage1_discovery_strategies_are_bounded_and_non_accepting() -> None:
    matrix = _load_fixture("erepo_exact_identity_cases.json")
    assert matrix["schema_version"] == "1.1"
    strategies = matrix["approved_discovery_strategies"]
    assert isinstance(strategies, list)
    by_id = {strategy["id"]: strategy for strategy in strategies}

    assert [strategy["priority"] for strategy in strategies] == [1, 2, 3, 4]
    assert by_id["exact_genomic_hgvs"]["query_column"] == "hgvs"
    assert by_id["exact_clinvar_variation_id"]["query_column"] == "cvId"
    assert by_id["exact_transcript_hgvs"]["query_column"] == "hgvs"
    assert by_id["future_validated_caid"]["enabled"] is False
    assert all(
        isinstance(strategy["max_values_per_variant"], int)
        and 1 <= strategy["max_values_per_variant"] <= 3
        for strategy in strategies
    )


def test_stage1_final_no_match_requires_all_eligible_strategies() -> None:
    cases = _case_map(_load_fixture("erepo_exact_identity_cases.json"))
    partial = cases["continue_after_one_strategy_no_match"]
    complete = cases["final_no_match_after_all_eligible_strategies"]

    assert partial["expected"]["provider_final_state"] == "continue_discovery"
    assert partial["expected"]["final_no_match"] is False
    assert complete["expected"]["provider_final_state"] == "no_match"
    assert complete["expected"]["final_no_match"] is True
    assert {
        result["strategy_id"] for result in complete["strategy_results"]
    } == set(complete["eligible_strategy_ids"])
    assert all(
        result["outcome"] == "no_match"
        for result in complete["strategy_results"]
    )


def test_stage1_no_match_fixture_is_structured_and_has_no_data_envelope() -> None:
    no_match = _load_fixture("summary_classification_exact_no_match.json")

    assert "data" not in no_match
    assert no_match["status"] == {
        "code": 404,
        "msg": "No records were found for given query",
        "name": "Not Found",
    }


def test_stage1_exact_and_equivalent_acceptance_cases_are_distinct() -> None:
    cases = _case_map(_load_fixture("erepo_exact_identity_cases.json"))
    exact = cases["accept_grch38_exact_hgvs"]
    equivalent = cases[
        "accept_deterministic_padded_indel_equivalent_representation"
    ]

    assert exact["expected"]["acceptance_state"] == "EXACT_MATCH"
    assert equivalent["expected"]["acceptance_state"] == (
        "EXACT_MATCH_EQUIVALENT_REPRESENTATION"
    )
    proof = equivalent["deterministic_equivalence_proof"]
    assert proof["normalizer"] == "backend.annotation._normalize_variant_edit"
    assert proof["formatted_genomic_hgvs"] == "NC_000001.11:g.101del"
    assert equivalent["expected"]["left_shift_inferred"] is False


def test_stage1_detailed_document_version_is_verified_by_metadata_version() -> None:
    summary = _load_fixture("summary_classification_exact_success_ca229507.json")
    detail = _load_fixture("detail_classification_exact_success_ca229507.json")
    summary_record = summary["data"][0]
    detail_data = detail["data"]

    assert detail_data["uuid"] == summary_record["uuid"]
    assert detail_data["metadata"]["version"] == summary_record["docVersion"]
    assert detail_data["@id"].endswith(
        f"/doc/sepio/version/{detail_data['metadata']['version']}"
    )

    cases = _case_map(_load_fixture("erepo_exact_identity_cases.json"))
    mismatch = cases["reject_detailed_document_version_mismatch"]
    assert mismatch["detail_response"]["data"]["metadata"]["version"] != (
        mismatch["summary"]["docVersion"]
    )
    assert mismatch["expected"]["candidate_rejection_reasons"] == [
        "detail_version_mismatch"
    ]
