"""Reviewer-facing fallback notices derived from capability provenance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

from backend.provider_resilience import (
    ProviderContractError,
    ProviderStatus,
    validate_capability_result,
)


CAPABILITY_LABELS = {
    "variant_annotation": "Variant annotation",
    "variant_context": "Variant context",
    "clinvar_evidence": "ClinVar evidence",
    "cspec_context": "CSpec context",
    "phenotype_gene": "Phenotype-gene",
    "disease_context": "Disease context",
    "population_frequency": "Population frequency",
    "literature": "Literature",
}
PROVIDER_LABELS = {
    "ensembl_vep": "Ensembl VEP",
    "variantvalidator": "VariantValidator",
    "myvariant": "MyVariant.info",
    "ensembl_variation": "Ensembl Variation",
    "ncbi_clinvar": "NCBI ClinVar",
    "clingen_cspec": "ClinGen CSpec",
    "cached_cspec": "local CSpec last-known-good cache",
    "phen2gene": "Phen2Gene",
    "local_hpo_gene_fallback": "local HPO-gene",
    "mydisease": "MyDisease.info",
    "local_hpo_disease_fallback": "local HPO disease-context",
    "gnomad": "gnomAD",
    "litvar": "LitVar2",
    "europe_pmc": "Europe PMC",
    "pubmed": "PubMed",
    "literature_fallback_chain": "literature provider chain",
}
METHOD_LABELS = {
    "validation_mapping_fallback": "validation and HGVS mapping",
    "validation_mapping": "validation and HGVS mapping",
    "overlapping_variant_context_fallback": "exact overlap context",
    "overlapping_variant_context": "exact overlap context",
    "myvariant_clinvar_derivation": "MyVariant-derived ClinVar context",
    "last_known_good_cache": "last-known-good metadata cache",
    "direct_hpo_gene_overlap": "direct HPO-gene overlap",
    "accepted_hpo_local_disease_context": (
        "accepted-HPO local disease context"
    ),
    "exact_mapping_population_lookup": "exact mapped-allele lookup",
    "bounded_literature_search_chain": "bounded literature search",
}
MAX_FALLBACK_NOTICES = 16


class FallbackTransparencyError(ValueError):
    """Raised when capability results cannot produce safe notices."""


class FallbackNotice(TypedDict):
    """One concise fallback disclosure with exact machine provenance."""

    capability: str
    capability_label: str
    primary_provider: str
    primary_provider_label: str
    fallback_provider: str
    fallback_provider_label: str
    primary_failure: ProviderStatus
    method: str
    method_label: str
    message: str


def _display_label(value: str, labels: Mapping[str, str]) -> str:
    return labels.get(value, value.replace("_", " "))


def build_fallback_notices(value: object) -> list[FallbackNotice]:
    """Build bounded, non-alarming notices for every fallback capability."""

    if not isinstance(value, Mapping):
        raise FallbackTransparencyError(
            "Capability results must be a mapping."
        )
    notices: list[FallbackNotice] = []
    for capability, raw_result in value.items():
        if not isinstance(capability, str):
            raise FallbackTransparencyError(
                "Capability result keys must be strings."
            )
        try:
            result = validate_capability_result(raw_result)
        except ProviderContractError as exc:
            raise FallbackTransparencyError(
                f"Invalid {capability} capability result."
            ) from exc
        if result["capability"] != capability:
            raise FallbackTransparencyError(
                "Capability result key does not match its capability."
            )
        if not result["fallback_used"]:
            continue
        primary_provider = result["fallback_for"]
        primary_failure = result["primary_failure"]
        if primary_provider is None or primary_failure is None:
            raise FallbackTransparencyError(
                "Fallback result is missing primary provenance."
            )
        capability_label = _display_label(
            capability,
            CAPABILITY_LABELS,
        )
        primary_label = _display_label(
            primary_provider,
            PROVIDER_LABELS,
        )
        fallback_label = _display_label(
            result["provider"],
            PROVIDER_LABELS,
        )
        method_label = _display_label(result["method"], METHOD_LABELS)
        notices.append(
            {
                "capability": capability,
                "capability_label": capability_label,
                "primary_provider": primary_provider,
                "primary_provider_label": primary_label,
                "fallback_provider": result["provider"],
                "fallback_provider_label": fallback_label,
                "primary_failure": primary_failure,
                "method": result["method"],
                "method_label": method_label,
                "message": (
                    f"{capability_label}: {primary_label} unavailable — "
                    f"{fallback_label} fallback used via {method_label}."
                ),
            }
        )
        if len(notices) >= MAX_FALLBACK_NOTICES:
            break
    return notices


__all__ = [
    "FallbackNotice",
    "FallbackTransparencyError",
    "build_fallback_notices",
]
