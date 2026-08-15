"""Offline contract tests for the pre-analysis provider-readiness catalog."""

from __future__ import annotations

import pytest

from backend.provider_readiness import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    FALLBACK_CHAINS,
    ProviderReadinessTarget,
    ProviderReadinessResult,
    QUALITY_SOURCE_CHAINS,
    probe_provider_readiness,
    recommend_provider_readiness,
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
        "ucsc_gnomad",
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
        "population_frequency": (
            "gnomad",
            "ucsc_gnomad",
            "ensembl_variation",
        ),
        "variant_literature": ("litvar2", "europe_pmc", "pubmed"),
        "gene_disease_literature": ("europe_pmc", "pubmed"),
        "variant_interpretation": ("llm", "llm_fallback_model"),
    }
    assert QUALITY_SOURCE_CHAINS["classification_context"] == ("genebe",)
    assert QUALITY_SOURCE_CHAINS["normal_population_frequency"] == (
        "myvariant",
        "ucsc_gnomad",
    )
    assert QUALITY_SOURCE_CHAINS["phenotype_extraction"] == ("llm",)


def test_normal_population_frequency_is_distinct_from_verification() -> None:
    targets = provider_readiness_target_map()

    assert "normal_population_frequency" in targets["myvariant"].capabilities
    assert "normal_population_frequency" in targets["ucsc_gnomad"].capabilities
    assert targets["gnomad"].capabilities == ("population_frequency",)
    assert targets["ucsc_gnomad"].fallback_for == (
        "normal_population_frequency",
        "population_frequency",
    )
    assert FALLBACK_CHAINS["population_frequency"] == (
        "gnomad",
        "ucsc_gnomad",
        "ensembl_variation",
    )


def _readiness_result(
    provider: str,
    *,
    state: str,
    latency_ms: float | None,
) -> ProviderReadinessResult:
    return ProviderReadinessResult(
        provider=provider,
        state=state,  # type: ignore[arg-type]
        dns_status="resolved",
        http_status=200 if state == "reachable" else None,
        latency_ms=latency_ms,
        failure_category="none" if state == "reachable" else "timeout",
        probe_kind="transport",
    )


def test_recommendation_keeps_the_higher_quality_source_when_reachable() -> None:
    recommendation = recommend_provider_readiness(
        "variant_annotation",
        (
            _readiness_result("vep", state="reachable", latency_ms=100),
            _readiness_result(
                "variantvalidator",
                state="reachable",
                latency_ms=5,
            ),
        ),
    )

    assert recommendation.provider == "vep"
    assert recommendation.state == "preferred"
    assert recommendation.latency_ms == 100


def test_recommendation_uses_latency_only_inside_an_equal_quality_tier() -> None:
    recommendation = recommend_provider_readiness(
        "variant_annotation",
        (
            _readiness_result("vep", state="reachable", latency_ms=100),
            _readiness_result(
                "variantvalidator",
                state="reachable",
                latency_ms=5,
            ),
        ),
        quality_tiers=(("vep", "variantvalidator"),),
    )

    assert recommendation.provider == "variantvalidator"
    assert recommendation.reason == "Highest evidence-quality reachable source."


def test_recommendation_prefers_a_reachable_fallback_after_primary_outage() -> None:
    recommendation = recommend_provider_readiness(
        "variant_annotation",
        (
            _readiness_result("vep", state="unreachable", latency_ms=None),
            _readiness_result(
                "variantvalidator",
                state="reachable",
                latency_ms=5,
            ),
        ),
    )

    assert recommendation.provider == "variantvalidator"
    assert recommendation.state == "preferred"
    assert "Higher-quality source is unreachable" in recommendation.reason


def test_recommendation_waits_for_an_unchecked_higher_quality_source() -> None:
    recommendation = recommend_provider_readiness(
        "variant_annotation",
        (_readiness_result("variantvalidator", state="reachable", latency_ms=5),),
    )

    assert recommendation.provider == "vep"
    assert recommendation.state == "awaiting_check"


def test_catalog_marks_credentials_without_exposing_any_secret() -> None:
    targets = provider_readiness_target_map()

    assert targets["llm"].credential_mode == "required"
    assert targets["llm"].probe_kind == "authenticated"
    assert targets["genebe"].credential_mode == "optional"
    assert all("key" not in repr(target).casefold() for target in targets.values())


def test_readiness_uses_a_bounded_connection_and_read_timeout() -> None:
    class FakeResponse:
        status_code = 404

        def close(self) -> None:
            return None

    class FakeSession:
        def __init__(self) -> None:
            self.timeout: object = None

        def head(self, _url: str, **kwargs: object) -> FakeResponse:
            self.timeout = kwargs["timeout"]
            return FakeResponse()

        def get(self, _url: str, **_kwargs: object) -> FakeResponse:
            raise AssertionError("transport probe must use HEAD")

    session = FakeSession()
    target = ProviderReadinessTarget(
        "example",
        "Example",
        "https://example.org/ready",
        ("example_capability",),
    )
    clock_values = iter((0.0, 3.5))

    result = probe_provider_readiness(
        target,
        session=session,  # type: ignore[arg-type]
        timeout=DEFAULT_READINESS_TIMEOUT_SECONDS,
        resolver=lambda *_args, **_kwargs: [],
        clock=lambda: next(clock_values),
    )

    assert DEFAULT_READINESS_TIMEOUT_SECONDS == 5.0
    assert session.timeout == (3.0, 5.0)
    assert result.state == "reachable"
    assert result.latency_ms == 3500.0


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
