"""Reviewer-facing source-status projection with retained technical semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict


class ReviewerSourceStatus(TypedDict):
    """One source-specific status intended for the primary review surface."""

    category: str
    message: str
    recovery: str | None


PhenotypeStatusCategory = Literal[
    "supported",
    "no_supported_association",
    "not_assessed",
    "unavailable",
]


_OPERATIONAL_FAILURES = frozenset(
    {
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
        "configuration_error",
        "connection_error",
        "http_429",
        "http_5xx",
        "authentication_error",
    }
)
_AVAILABLE = frozenset(
    {
        "available",
        "success",
        "available_via_fallback",
        "supported",
        "strong_match",
        "partial_match",
        "partially_supported",
    }
)
_NOT_QUERIED = frozenset(
    {"not_assessed", "not_triggered", "not_applicable", "skipped"}
)
_EXPECTED_ABSENCE_CAPABILITIES = frozenset(
    {
        "cspec_context", "literature", "phenotype_gene", "disease_context",
        "expert_curated_variant_context",
    }
)
_CAPABILITY_LABELS = {
    "variant_annotation": "variant annotation",
    "variant_context": "variant-context",
    "clinvar_evidence": "ClinVar",
    "gene_disease_validity": "gene-disease",
    "automated_classification": "automated classification",
    "cspec_context": "ClinGen CSpec",
    "expert_curated_variant_context": "ClinGen ERepo expert-curated context",
    "phenotype_gene": "phenotype-gene",
    "disease_context": "disease-context",
    "population_frequency": "population-frequency",
    "literature": "literature",
}


def _text(value: object, fallback: str = "Source") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _normalized(value: object) -> str:
    return str(value or "").strip().casefold().replace(" ", "_")


def phenotype_status_category(value: object) -> PhenotypeStatusCategory:
    """Map current and historical phenotype states to one UI vocabulary."""

    status = _normalized(value)
    if status in {
        "exact_match",
        "partial_match",
        "supported",
        "strong_match",
        "partially_supported",
    }:
        return "supported"
    if status in {
        "no_exact_match",
        "no_match",
        "not_supported",
        "unrelated",
    }:
        return "no_supported_association"
    if status in {
        "",
        "not_applicable",
        "not_assessed",
        "not_triggered",
        "skipped",
    }:
        return "not_assessed"
    return "unavailable"


def _capability_from_source(source: str) -> str | None:
    normalized = source.casefold()
    if "erepo" in normalized:
        return "expert_curated_variant_context"
    if "clinvar" in normalized:
        return "clinvar_evidence"
    if "cspec" in normalized:
        return "cspec_context"
    if "literature" in normalized or "pubmed" in normalized or "litvar" in normalized:
        return "literature"
    if "phen2gene" in normalized or "phenotype-gene" in normalized:
        return "phenotype_gene"
    if "mydisease" in normalized or "disease context" in normalized:
        return "disease_context"
    if "gnomad" in normalized or "population" in normalized:
        return "population_frequency"
    if "vep" in normalized or "variantvalidator" in normalized:
        return "variant_annotation"
    if "myvariant" in normalized:
        return "variant_context"
    if "genebe" in normalized:
        return "automated_classification"
    if "clingen" in normalized or "gencc" in normalized:
        return "gene_disease_validity"
    return None


def _no_match_message(capability: str, source: str) -> str:
    if capability == "clinvar_evidence":
        return "No exact ClinVar allele record was found by the primary lookup."
    if capability == "cspec_context":
        return (
            "No applicable ClinGen CSpec specification was identified for "
            "the current gene/disease scope."
        )
    if capability == "literature":
        return (
            "No qualifying variant-specific literature was found after the "
            "configured search strategy."
        )
    if capability in {"phenotype_gene", "disease_context"}:
        return "No supported phenotype association was identified."
    label = _CAPABILITY_LABELS.get(capability, source)
    return f"No matching {label} record was found after the configured lookup."


def build_reviewer_source_status(value: Mapping[str, object]) -> ReviewerSourceStatus:
    """Translate one retained provider record into reviewer-safe wording."""

    source = _text(value.get("source"))
    capability = _normalized(value.get("capability"))
    status = _normalized(value.get("status"))
    operational_status = _normalized(value.get("operational_status"))
    if capability == "phenotype_gene":
        phenotype_category = phenotype_status_category(status)
        if phenotype_category == "supported":
            status = "available"
        elif phenotype_category == "no_supported_association":
            status = "no_match"
        elif phenotype_category == "not_assessed":
            status = "not_assessed"
    if status in {"not_found", "no_exact_match"}:
        status = "no_match"
    if operational_status in {"not_found", "no_exact_match"}:
        operational_status = "no_match"
    fallback_used = value.get("fallback_used") is True
    provider_role = _normalized(value.get("provider_role"))
    is_fallback = fallback_used or provider_role == "fallback"

    if is_fallback and status in _AVAILABLE:
        primary_label = _CAPABILITY_LABELS.get(capability, "primary source")
        return {
            "category": "Evidence rescue completed",
            "message": f"Secondary evidence was recovered from {source}.",
            "recovery": (
                f"The primary {primary_label} lookup was unavailable; the "
                f"retained evidence is source-attributed to {source}."
            ),
        }
    if is_fallback and status == "no_match":
        return {
            "category": "Evidence rescue completed: no secondary match",
            "message": (
                f"The configured rescue search through {source} completed, "
                "but found no qualifying secondary evidence."
            ),
            "recovery": "The primary and rescue outcomes remain separately recorded.",
        }
    if status == "no_match" or (
        capability in {"phenotype_gene", "disease_context"}
        and status in {"not_supported", "unrelated"}
    ):
        return {
            "category": (
                "Expected absence"
                if capability in _EXPECTED_ABSENCE_CAPABILITIES
                else "Primary lookup: no exact match"
            ),
            "message": _no_match_message(capability, source),
            "recovery": None,
        }
    if operational_status in _NOT_QUERIED or status in _NOT_QUERIED:
        return {
            "category": "Not queried",
            "message": (
                f"{source} was not queried because its configured trigger "
                "conditions were not met."
            ),
            "recovery": None,
        }
    if operational_status in _OPERATIONAL_FAILURES or status in _OPERATIONAL_FAILURES:
        return {
            "category": "Provider unavailable",
            "message": (
                f"{source} could not complete the configured lookup; evidence "
                "from other completed sources was preserved."
            ),
            "recovery": None,
        }
    if status in _AVAILABLE:
        return {
            "category": "Evidence available",
            "message": f"{source} supplied usable source-attributed evidence.",
            "recovery": None,
        }
    if status == "unsupported":
        return {
            "category": "Not supported by source",
            "message": f"{source} does not support this evidence capability.",
            "recovery": None,
        }
    readable = status.replace("_", " ").strip() or "not recorded"
    return {
        "category": "Status recorded",
        "message": f"{source} status: {readable}.",
        "recovery": None,
    }


def build_reviewer_section_status(
    *,
    source: str,
    status: object,
    data_sources: Sequence[Mapping[str, object]],
) -> ReviewerSourceStatus:
    """Project an evidence section using its matching provider provenance."""

    capability = _capability_from_source(source)
    candidates = [
        item
        for item in data_sources
        if capability is not None
        and _normalized(item.get("capability")) == capability
    ]
    if candidates:
        if _normalized(status) == "available_via_fallback":
            fallback = next(
                (
                    item
                    for item in candidates
                    if item.get("fallback_used") is True
                    or _normalized(item.get("provider_role")) == "fallback"
                ),
                None,
            )
            if fallback is not None:
                return build_reviewer_source_status(fallback)
        return build_reviewer_source_status(candidates[0])
    return build_reviewer_source_status(
        {
            "source": source,
            "capability": capability or "",
            "status": status,
            "operational_status": status,
            "provider_role": "primary",
            "fallback_used": False,
        }
    )


__all__ = [
    "PhenotypeStatusCategory",
    "ReviewerSourceStatus",
    "build_reviewer_section_status",
    "build_reviewer_source_status",
    "phenotype_status_category",
]
