"""Offline contract tests for the pre-analysis provider-readiness catalog."""

from __future__ import annotations

import pytest

from backend.provider_readiness import (
    FALLBACK_CHAINS,
    ProviderReadinessTarget,
    configured_provider_readiness_targets,
    provider_readiness_target_map,
)
from tools.provider_reachability import configured_targets


def test_catalog_covers_active_providers_and_the_existing_probe_targets() -> None:
    targets = configured_provider_readiness_targets()

    assert {target.provider for target in targets} == {
        "vep",
        "variantvalidator",
        "genebe",
        "myvariant",
        "clinvar",
        "clingen",
        "cspec",
        "phen2gene",
        "mydisease",
        "gnomad",
        "ensembl_variation",
        "litvar2",
        "europe_pmc",
        "pubmed",
        "llm",
    }
    assert {target.provider for target in configured_targets()} == {
        target.provider for target in targets if target.provider != "llm"
    }


def test_fallback_chains_preserve_the_existing_provider_contract() -> None:
    assert FALLBACK_CHAINS == {
        "variant_annotation": ("vep", "variantvalidator"),
        "variant_context": ("myvariant", "ensembl_variation"),
        "clinvar_evidence": ("clinvar", "myvariant"),
        "cspec_context": ("cspec", "local_cspec_cache"),
        "phenotype_gene_context": ("phen2gene", "local_hpo_gene"),
        "disease_hpo_context": ("mydisease", "local_hpo_disease"),
        "population_frequency": ("gnomad", "ensembl_variation"),
        "variant_literature": ("litvar2", "europe_pmc", "pubmed"),
        "gene_disease_literature": ("europe_pmc", "pubmed"),
        "variant_interpretation": ("llm", "llm_fallback_model"),
    }


def test_catalog_marks_credentials_without_exposing_any_secret() -> None:
    targets = provider_readiness_target_map()

    assert targets["llm"].credential_mode == "required"
    assert targets["llm"].probe_kind == "authenticated"
    assert targets["genebe"].credential_mode == "optional"
    assert all("key" not in repr(target).casefold() for target in targets.values())


@pytest.mark.parametrize(
    "url",
    ("http://example.org", "https:///missing-host", "not-a-url"),
)
def test_target_rejects_non_https_or_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ProviderReadinessTarget(
            "example",
            "Example",
            url,
            ("example_capability",),
        )
