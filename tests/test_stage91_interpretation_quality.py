"""Stage 91 evidence-based interpretation-model quality gate checks."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from backend.interpretation_quality import (
    MODEL_QUALITY_CASE_IDS,
    ModelQualityError,
    evaluate_interpretation_models,
    validate_model_case_evaluation,
)
from config import settings


pytestmark = pytest.mark.stage91_interpretation_quality
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _cases(*, score: int = 5, cost: float | None = 0.001) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    phenotype_cases = set(MODEL_QUALITY_CASE_IDS[:3])
    for case_id in MODEL_QUALITY_CASE_IDS:
        cases.append(
            {
                "case_id": case_id,
                "status": "success",
                "groundedness_score": score,
                "hallucination_count": 0,
                "clinical_style_score": score,
                "conflict_handling_score": (
                    score if case_id == "conflicting_clinvar_evidence" else None
                ),
                "phenotype_restraint_score": (
                    score if case_id in phenotype_cases else None
                ),
                "structured_output_reliable": True,
                "latency_ms": 500,
                "input_tokens": 1_000,
                "output_tokens": 250,
                "estimated_cost_usd": cost,
                "reviewer_notes": "Reviewed against bounded evidence.",
            }
        )
    return cases


def _candidate(
    model: str,
    *,
    score: int = 5,
    mode: str = "live_provider",
    human_reviewed: bool = True,
    cost: float | None = 0.001,
) -> dict[str, object]:
    return {
        "model": model,
        "execution_mode": mode,
        "human_reviewed": human_reviewed,
        "cases": _cases(score=score, cost=cost),
    }


def test_case_manifest_is_exact_and_task_specific() -> None:
    manifest = json.loads(
        (PROJECT_ROOT / "data/benchmarks/stage91_cases.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["task"] == "variant_interpretation"
    assert tuple(item["case_id"] for item in manifest["cases"]) == (
        MODEL_QUALITY_CASE_IDS
    )
    assert all(item["required_review"] for item in manifest["cases"])


def test_complete_live_review_selects_stronger_eligible_model() -> None:
    current = _candidate("current-model", score=4)
    candidate = _candidate("candidate-model", score=5)

    decision = evaluate_interpretation_models(
        [current, candidate], current_default_model="current-model"
    )

    assert decision["task"] == "variant_interpretation"
    assert decision["status"] == "recommended"
    assert decision["recommended_model"] == "candidate-model"
    metrics = decision["candidate_summaries"][1]["metrics"]
    assert set(metrics) == {
        "groundedness",
        "hallucination_rate",
        "clinical_style_coherence",
        "conflict_handling",
        "phenotype_restraint",
        "structured_output_reliability",
        "mean_latency_ms",
        "total_estimated_cost_usd",
        "total_input_tokens",
        "total_output_tokens",
        "composite_quality",
    }


def test_hallucination_disqualifies_candidate() -> None:
    current = _candidate("current-model", score=4)
    candidate = _candidate("candidate-model")
    cases = candidate["cases"]
    assert isinstance(cases, list)
    cases[0]["hallucination_count"] = 1

    decision = evaluate_interpretation_models(
        [current, candidate], current_default_model="current-model"
    )

    candidate_summary = decision["candidate_summaries"][1]
    assert candidate_summary["eligible"] is False
    assert "hallucination_detected" in candidate_summary["disqualifiers"]
    assert decision["recommended_model"] == "current-model"


def test_offline_fixtures_cannot_select_or_change_default() -> None:
    phenotype_model_before = settings.PHENOTYPE_EXTRACTION_MODEL
    variant_model_before = settings.VARIANT_INTERPRETATION_MODEL
    candidates = [
        _candidate("current-model", mode="offline_fixture"),
        _candidate("candidate-model", mode="offline_fixture"),
    ]

    decision = evaluate_interpretation_models(
        candidates, current_default_model="current-model"
    )

    assert decision["status"] == "no_recommendation"
    assert decision["recommended_model"] is None
    assert settings.PHENOTYPE_EXTRACTION_MODEL == phenotype_model_before
    assert settings.VARIANT_INTERPRETATION_MODEL == variant_model_before


@pytest.mark.parametrize("bad_case", ["missing", "duplicate"])
def test_each_candidate_requires_seven_unique_cases(bad_case: str) -> None:
    candidate = _candidate("current-model")
    cases = candidate["cases"]
    assert isinstance(cases, list)
    if bad_case == "missing":
        cases.pop()
    else:
        cases[-1] = deepcopy(cases[0])

    with pytest.raises(ModelQualityError, match="seven unique"):
        evaluate_interpretation_models(
            [candidate, _candidate("other-model")],
            current_default_model="current-model",
        )


def test_specialized_scores_are_required_only_for_relevant_cases() -> None:
    conflict_case = _cases()[3]
    conflict_case["conflict_handling_score"] = None
    with pytest.raises(ModelQualityError, match="requires conflict"):
        validate_model_case_evaluation(conflict_case)

    sparse_case = _cases()[4]
    sparse_case["phenotype_restraint_score"] = 5
    with pytest.raises(ModelQualityError, match="only for phenotype"):
        validate_model_case_evaluation(sparse_case)


def test_missing_cost_prevents_evidence_based_recommendation() -> None:
    candidates = [
        _candidate("current-model", cost=None),
        _candidate("candidate-model", cost=None),
    ]

    decision = evaluate_interpretation_models(
        candidates, current_default_model="current-model"
    )

    assert decision["status"] == "no_recommendation"
    assert all(
        "cost_not_measured" in summary["disqualifiers"]
        for summary in decision["candidate_summaries"]
    )


def test_comparison_requires_two_unique_models_including_current_default() -> None:
    with pytest.raises(ModelQualityError, match="at least two unique"):
        evaluate_interpretation_models(
            [_candidate("current-model")],
            current_default_model="current-model",
        )

    with pytest.raises(ModelQualityError, match="current default"):
        evaluate_interpretation_models(
            [_candidate("model-a"), _candidate("model-b")],
            current_default_model="current-model",
        )
