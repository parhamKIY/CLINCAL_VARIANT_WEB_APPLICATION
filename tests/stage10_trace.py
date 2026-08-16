"""Bounded, redacted runtime trace helpers for the Stage 10 audit."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from typing import Any

from backend.evidence_coverage import build_evidence_coverage
from backend.final_disposition import build_final_disposition
from backend.shadow_composition import shadow_free_evidence_for_llm


TRACE_SCHEMA_VERSION = "1.0"


def _capability(coverage: Mapping[str, object], target: str) -> Mapping[str, object]:
    records = coverage["semantic_capabilities"]
    assert isinstance(records, list)
    return next(item for item in records if item["semantic_target"] == target)


def _paths(value: object, *, prefix: str = "") -> list[str]:
    if isinstance(value, Mapping):
        return [
            path
            for key, item in value.items()
            for path in _paths(item, prefix=f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(value, list):
        return [path for item in value for path in _paths(item, prefix=f"{prefix}[]")]
    return [prefix]


def build_redacted_trace(
    *,
    scenario_id: str,
    evidence: Mapping[str, object],
    readiness: Mapping[str, object],
    first_loss_stage: str,
    cause: str,
    loss_expected: bool,
    interpretation: Mapping[str, object] | None = None,
    persistence: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Trace only normalized evidence state; never retain prompt/provider payloads."""

    coverage = build_evidence_coverage(evidence)
    disposition = build_final_disposition(evidence, readiness, coverage=coverage)
    llm_evidence = shadow_free_evidence_for_llm(evidence)
    raw_paths = set(_paths(evidence))
    llm_paths = set(_paths(llm_evidence))
    excluded = sorted(
        path for path in raw_paths - llm_paths
        if "candidate_diagnostics" in path or path.split(".")[0] in {"shadow_composition", "annotation_promotion"}
    )
    included = sorted(path for path in llm_paths if path.endswith("records[]"))
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "variant": deepcopy(evidence["variant"]),
        "first_loss_stage": first_loss_stage,
        "cause": cause,
        "loss_expected": loss_expected,
        "evidence_paths": sorted(path for path in raw_paths if path.endswith("records[]")),
        "coverage": {
            "overall_state": coverage["overall_state"],
            "annotation": _capability(coverage, "annotation")["state"],
            "gene_disease_validity": _capability(coverage, "gene_disease_validity")["state"],
            "gene_disease_support": _capability(coverage, "gene_disease_support")["state"],
        },
        "readiness": readiness["readiness_after_rescue"],
        "final_disposition": disposition["state"],
        "llm_manifest": {
            "accepted_records": "INCLUDED" if included else "NOT_PRESENT",
            "candidate_diagnostics": "EXCLUDED_BY_DESIGN" if excluded else "NOT_PRESENT",
            "shadow_composition": "EXCLUDED_BY_DESIGN" if "shadow_composition" in evidence else "NOT_PRESENT",
            "stage7_coverage": "EXCLUDED_BY_DESIGN",
            "stage8_technical_diagnostics": "EXCLUDED_BY_DESIGN",
            "raw_provider_payload": "EXCLUDED_BY_DESIGN",
            "raw_vcf_sample_genotype": "EXCLUDED_BY_DESIGN",
            "included_record_paths": included,
            "excluded_paths": excluded,
        },
        "llm_payload_sha256": sha256(repr(llm_evidence).encode("utf-8")).hexdigest(),
        "interpretation_status": None if interpretation is None else interpretation.get("status"),
        "persistence": {} if persistence is None else deepcopy(dict(persistence)),
    }
