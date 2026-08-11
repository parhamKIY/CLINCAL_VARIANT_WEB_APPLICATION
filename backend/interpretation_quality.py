"""Stage 91 evidence-based Variant Interpretation Model benchmark."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from math import isfinite
from statistics import fmean
from typing import Literal, TypedDict, cast


MODEL_QUALITY_SCHEMA_VERSION = "1.0"
MODEL_QUALITY_TASK = "variant_interpretation"
MODEL_QUALITY_CASE_IDS = (
    "strong_phenotype_match",
    "partial_phenotype_match",
    "irrelevant_phenotype",
    "conflicting_clinvar_evidence",
    "sparse_evidence",
    "rich_literature",
    "no_literature",
)
MODEL_QUALITY_CASE_ID_SET = frozenset(MODEL_QUALITY_CASE_IDS)
MIN_GROUNDEDNESS = 0.80
MAX_HALLUCINATION_RATE = 0.0
MIN_CLINICAL_STYLE_COHERENCE = 0.80
MIN_CONFLICT_HANDLING = 0.80
MIN_PHENOTYPE_RESTRAINT = 0.80
MIN_STRUCTURED_OUTPUT_RELIABILITY = 1.0

EvaluationMode = Literal["live_provider", "offline_fixture"]
DecisionStatus = Literal["recommended", "no_recommendation"]


class ModelQualityError(ValueError):
    """Raised when a model-quality evaluation is incomplete or unsafe."""


class ModelCaseEvaluation(TypedDict):
    """One human-reviewed candidate result for one fixed benchmark case."""

    case_id: str
    status: Literal["success", "failed"]
    groundedness_score: int
    hallucination_count: int
    clinical_style_score: int
    conflict_handling_score: int | None
    phenotype_restraint_score: int | None
    structured_output_reliable: bool
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    reviewer_notes: str


class ModelCandidateEvaluation(TypedDict):
    """Complete seven-case evidence for one candidate model."""

    model: str
    execution_mode: EvaluationMode
    human_reviewed: bool
    cases: list[ModelCaseEvaluation]


class ModelQualityMetrics(TypedDict):
    groundedness: float
    hallucination_rate: float
    clinical_style_coherence: float
    conflict_handling: float
    phenotype_restraint: float
    structured_output_reliability: float
    mean_latency_ms: float
    total_estimated_cost_usd: float | None
    total_input_tokens: int
    total_output_tokens: int
    composite_quality: float


class ModelCandidateSummary(TypedDict):
    model: str
    execution_mode: EvaluationMode
    human_reviewed: bool
    eligible: bool
    disqualifiers: list[str]
    metrics: ModelQualityMetrics


class ModelQualityDecision(TypedDict):
    schema_version: str
    task: str
    current_default_model: str
    status: DecisionStatus
    recommended_model: str | None
    reason: str
    evaluated_case_ids: list[str]
    candidate_summaries: list[ModelCandidateSummary]


CASE_FIELDS = frozenset(ModelCaseEvaluation.__required_keys__)
CANDIDATE_FIELDS = frozenset(ModelCandidateEvaluation.__required_keys__)


def _text(value: object, path: str, *, maximum: int = 2_000) -> str:
    if not isinstance(value, str):
        raise ModelQualityError(f"{path} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"}
        for character in normalized
    ):
        raise ModelQualityError(f"{path} is invalid.")
    return normalized


def _score(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ModelQualityError(f"{path} must be an integer from 1 to 5.")
    return value


def _optional_score(value: object, path: str) -> int | None:
    return None if value is None else _score(value, path)


def _non_negative_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelQualityError(f"{path} must be a non-negative integer.")
    return value


def validate_model_case_evaluation(
    value: object,
) -> ModelCaseEvaluation:
    if not isinstance(value, dict) or set(value) != CASE_FIELDS:
        raise ModelQualityError("Model case evaluation has invalid fields.")
    case_id = _text(value["case_id"], "case_id", maximum=64)
    if case_id not in MODEL_QUALITY_CASE_ID_SET:
        raise ModelQualityError("case_id is not in the Stage 91 benchmark.")
    if value["status"] not in {"success", "failed"}:
        raise ModelQualityError("status is invalid.")
    groundedness = _score(value["groundedness_score"], "groundedness_score")
    hallucinations = _non_negative_int(
        value["hallucination_count"], "hallucination_count"
    )
    clinical_style = _score(
        value["clinical_style_score"], "clinical_style_score"
    )
    conflict_score = _optional_score(
        value["conflict_handling_score"], "conflict_handling_score"
    )
    phenotype_score = _optional_score(
        value["phenotype_restraint_score"], "phenotype_restraint_score"
    )
    if case_id == "conflicting_clinvar_evidence" and conflict_score is None:
        raise ModelQualityError(
            "The conflicting ClinVar case requires conflict handling review."
        )
    if case_id != "conflicting_clinvar_evidence" and conflict_score is not None:
        raise ModelQualityError(
            "Conflict handling score is only valid for the conflict case."
        )
    phenotype_cases = {
        "strong_phenotype_match",
        "partial_phenotype_match",
        "irrelevant_phenotype",
    }
    if (case_id in phenotype_cases) != (phenotype_score is not None):
        raise ModelQualityError(
            "Phenotype restraint scores are required only for phenotype cases."
        )
    if not isinstance(value["structured_output_reliable"], bool):
        raise ModelQualityError("structured_output_reliable must be boolean.")
    latency = _non_negative_int(value["latency_ms"], "latency_ms")
    if latency == 0:
        raise ModelQualityError("latency_ms must be greater than zero.")
    input_tokens = _non_negative_int(value["input_tokens"], "input_tokens")
    output_tokens = _non_negative_int(value["output_tokens"], "output_tokens")
    cost = value["estimated_cost_usd"]
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not isfinite(cost)
        or cost < 0
    ):
        raise ModelQualityError("estimated_cost_usd is invalid.")
    notes = _text(value["reviewer_notes"], "reviewer_notes")
    return cast(
        ModelCaseEvaluation,
        {
            "case_id": case_id,
            "status": value["status"],
            "groundedness_score": groundedness,
            "hallucination_count": hallucinations,
            "clinical_style_score": clinical_style,
            "conflict_handling_score": conflict_score,
            "phenotype_restraint_score": phenotype_score,
            "structured_output_reliable": value[
                "structured_output_reliable"
            ],
            "latency_ms": latency,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": None if cost is None else float(cost),
            "reviewer_notes": notes,
        },
    )


def validate_model_candidate_evaluation(
    value: object,
) -> ModelCandidateEvaluation:
    if not isinstance(value, dict) or set(value) != CANDIDATE_FIELDS:
        raise ModelQualityError("Model candidate evaluation has invalid fields.")
    model = _text(value["model"], "model", maximum=200)
    mode = value["execution_mode"]
    if mode not in {"live_provider", "offline_fixture"}:
        raise ModelQualityError("execution_mode is invalid.")
    if not isinstance(value["human_reviewed"], bool):
        raise ModelQualityError("human_reviewed must be boolean.")
    raw_cases = value["cases"]
    if isinstance(raw_cases, (str, bytes)) or not isinstance(raw_cases, Sequence):
        raise ModelQualityError("cases must be a sequence.")
    if any(not isinstance(item, Mapping) for item in raw_cases):
        raise ModelQualityError("Each case must be an object.")
    cases = [validate_model_case_evaluation(dict(item)) for item in raw_cases]
    case_ids = [item["case_id"] for item in cases]
    if len(cases) != len(MODEL_QUALITY_CASE_IDS) or set(case_ids) != (
        MODEL_QUALITY_CASE_ID_SET
    ) or len(set(case_ids)) != len(case_ids):
        raise ModelQualityError(
            "Each candidate requires all seven unique Stage 91 cases."
        )
    ordered = sorted(
        cases,
        key=lambda item: MODEL_QUALITY_CASE_IDS.index(item["case_id"]),
    )
    return {
        "model": model,
        "execution_mode": cast(EvaluationMode, mode),
        "human_reviewed": value["human_reviewed"],
        "cases": ordered,
    }


def _metrics(cases: list[ModelCaseEvaluation]) -> ModelQualityMetrics:
    conflict_scores = [
        cast(int, item["conflict_handling_score"])
        for item in cases
        if item["conflict_handling_score"] is not None
    ]
    phenotype_scores = [
        cast(int, item["phenotype_restraint_score"])
        for item in cases
        if item["phenotype_restraint_score"] is not None
    ]
    groundedness = fmean(item["groundedness_score"] for item in cases) / 5
    coherence = fmean(item["clinical_style_score"] for item in cases) / 5
    conflict = fmean(conflict_scores) / 5
    phenotype = fmean(phenotype_scores) / 5
    reliability = fmean(
        1.0
        if item["status"] == "success" and item["structured_output_reliable"]
        else 0.0
        for item in cases
    )
    hallucination_rate = fmean(
        1.0 if item["hallucination_count"] > 0 else 0.0 for item in cases
    )
    costs = [item["estimated_cost_usd"] for item in cases]
    total_cost = (
        None
        if any(item is None for item in costs)
        else sum(cast(float, item) for item in costs)
    )
    composite = fmean(
        (groundedness, coherence, conflict, phenotype, reliability)
    )
    return {
        "groundedness": round(groundedness, 4),
        "hallucination_rate": round(hallucination_rate, 4),
        "clinical_style_coherence": round(coherence, 4),
        "conflict_handling": round(conflict, 4),
        "phenotype_restraint": round(phenotype, 4),
        "structured_output_reliability": round(reliability, 4),
        "mean_latency_ms": round(fmean(item["latency_ms"] for item in cases), 2),
        "total_estimated_cost_usd": (
            None if total_cost is None else round(total_cost, 6)
        ),
        "total_input_tokens": sum(item["input_tokens"] for item in cases),
        "total_output_tokens": sum(item["output_tokens"] for item in cases),
        "composite_quality": round(composite, 4),
    }


def _summary(candidate: ModelCandidateEvaluation) -> ModelCandidateSummary:
    metrics = _metrics(candidate["cases"])
    disqualifiers: list[str] = []
    if candidate["execution_mode"] != "live_provider":
        disqualifiers.append("not_live_provider_evidence")
    if not candidate["human_reviewed"]:
        disqualifiers.append("human_review_incomplete")
    if metrics["groundedness"] < MIN_GROUNDEDNESS:
        disqualifiers.append("groundedness_below_threshold")
    if metrics["hallucination_rate"] > MAX_HALLUCINATION_RATE:
        disqualifiers.append("hallucination_detected")
    if metrics["clinical_style_coherence"] < MIN_CLINICAL_STYLE_COHERENCE:
        disqualifiers.append("clinical_style_below_threshold")
    if metrics["conflict_handling"] < MIN_CONFLICT_HANDLING:
        disqualifiers.append("conflict_handling_below_threshold")
    if metrics["phenotype_restraint"] < MIN_PHENOTYPE_RESTRAINT:
        disqualifiers.append("phenotype_restraint_below_threshold")
    if metrics["structured_output_reliability"] < (
        MIN_STRUCTURED_OUTPUT_RELIABILITY
    ):
        disqualifiers.append("structured_output_unreliable")
    if metrics["total_estimated_cost_usd"] is None:
        disqualifiers.append("cost_not_measured")
    return {
        "model": candidate["model"],
        "execution_mode": candidate["execution_mode"],
        "human_reviewed": candidate["human_reviewed"],
        "eligible": not disqualifiers,
        "disqualifiers": disqualifiers,
        "metrics": metrics,
    }


def evaluate_interpretation_models(
    candidates: Sequence[Mapping[str, object]],
    *,
    current_default_model: str,
) -> ModelQualityDecision:
    """Compare complete candidates without changing runtime configuration."""

    current = _text(
        current_default_model,
        "current_default_model",
        maximum=200,
    )
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise ModelQualityError("candidates must be a sequence.")
    if any(not isinstance(item, Mapping) for item in candidates):
        raise ModelQualityError("Each candidate must be an object.")
    validated = [validate_model_candidate_evaluation(dict(item)) for item in candidates]
    models = [item["model"] for item in validated]
    if len(validated) < 2 or len(set(models)) != len(models):
        raise ModelQualityError(
            "Stage 91 requires at least two unique candidate models."
        )
    if current not in models:
        raise ModelQualityError(
            "The current default model must be included in the comparison."
        )
    summaries = [_summary(item) for item in validated]
    eligible = [item for item in summaries if item["eligible"]]
    if not eligible:
        status: DecisionStatus = "no_recommendation"
        recommended = None
        reason = (
            "No candidate has complete live, human-reviewed quality, "
            "reliability, latency, and cost evidence. Keep the configured "
            "default unchanged."
        )
    else:
        status = "recommended"
        recommended_summary = sorted(
            eligible,
            key=lambda item: (
                -item["metrics"]["composite_quality"],
                item["metrics"]["hallucination_rate"],
                -item["metrics"]["structured_output_reliability"],
                item["metrics"]["mean_latency_ms"],
                cast(float, item["metrics"]["total_estimated_cost_usd"]),
                item["model"],
            ),
        )[0]
        recommended = recommended_summary["model"]
        reason = (
            "Recommended from complete live, human-reviewed benchmark "
            "evidence; runtime configuration is not changed automatically."
        )
    return {
        "schema_version": MODEL_QUALITY_SCHEMA_VERSION,
        "task": MODEL_QUALITY_TASK,
        "current_default_model": current,
        "status": status,
        "recommended_model": recommended,
        "reason": reason,
        "evaluated_case_ids": list(MODEL_QUALITY_CASE_IDS),
        "candidate_summaries": deepcopy(summaries),
    }


__all__ = [
    "MODEL_QUALITY_CASE_IDS",
    "MODEL_QUALITY_SCHEMA_VERSION",
    "MODEL_QUALITY_TASK",
    "ModelCandidateEvaluation",
    "ModelCaseEvaluation",
    "ModelQualityDecision",
    "ModelQualityError",
    "evaluate_interpretation_models",
    "validate_model_candidate_evaluation",
    "validate_model_case_evaluation",
]
