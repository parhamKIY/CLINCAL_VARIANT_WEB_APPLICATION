"""Stage 74 fallback transparency regression tests."""

from __future__ import annotations

from contextlib import nullcontext

import pytest

from backend.fallback_transparency import (
    FallbackTransparencyError,
    build_fallback_notices,
)
from backend.provider_resilience import build_capability_result
from frontend.results import _render_fallback_notices


def _primary_result() -> dict[str, object]:
    return build_capability_result(
        capability="variant_annotation",
        status="success",
        provider="ensembl_vep",
        method="vep_annotation",
    )


def _phenotype_fallback() -> dict[str, object]:
    return build_capability_result(
        capability="phenotype_gene",
        status="success",
        provider="local_hpo_gene_fallback",
        provider_role="fallback",
        fallback_for="phen2gene",
        primary_failure="timeout",
        method="direct_hpo_gene_overlap",
    )


def test_notice_is_concise_and_preserves_exact_provenance() -> None:
    notices = build_fallback_notices(
        {
            "variant_annotation": _primary_result(),
            "phenotype_gene": _phenotype_fallback(),
        }
    )

    assert notices == [
        {
            "capability": "phenotype_gene",
            "capability_label": "Phenotype-gene",
            "primary_provider": "phen2gene",
            "primary_provider_label": "Phen2Gene",
            "fallback_provider": "local_hpo_gene_fallback",
            "fallback_provider_label": "local HPO-gene",
            "primary_failure": "timeout",
            "method": "direct_hpo_gene_overlap",
            "method_label": "direct HPO-gene overlap",
            "message": (
                "Phenotype-gene: Phen2Gene unavailable — local HPO-gene "
                "fallback used via direct HPO-gene overlap."
            ),
        }
    ]


def test_population_notice_names_gnomad_and_ensembl() -> None:
    result = build_capability_result(
        capability="population_frequency",
        status="success",
        provider="ensembl_variation",
        provider_role="fallback",
        fallback_for="gnomad",
        primary_failure="forbidden",
        method="exact_mapping_population_lookup",
    )

    notice = build_fallback_notices(
        {"population_frequency": result}
    )[0]

    assert notice["message"] == (
        "Population frequency: gnomAD unavailable — Ensembl Variation "
        "fallback used via exact mapped-allele lookup."
    )
    assert notice["primary_failure"] == "forbidden"


def test_primary_results_do_not_create_degraded_mode_notices() -> None:
    assert build_fallback_notices(
        {"variant_annotation": _primary_result()}
    ) == []


def test_invalid_capability_results_are_rejected() -> None:
    with pytest.raises(FallbackTransparencyError):
        build_fallback_notices(
            {"variant_annotation": {"fallback_used": True}}
        )


def test_evidence_ui_renders_notice_and_provenance_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []
    tables: list[object] = []
    monkeypatch.setattr(
        "frontend.results.st.container",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        "frontend.results.st.expander",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        "frontend.results.st.markdown",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "frontend.results.st.caption",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "frontend.results.st.write",
        lambda value: writes.append(value),
    )
    monkeypatch.setattr(
        "frontend.results.st.table",
        lambda value: tables.append(value),
    )

    _render_fallback_notices(
        {"capability_results": {"phenotype_gene": _phenotype_fallback()}}
    )

    assert writes == [
        "- Phenotype-gene: Phen2Gene unavailable — local HPO-gene "
        "fallback used via direct HPO-gene overlap."
    ]
    assert tables[0][0] == {
        "Capability": "Phenotype-gene",
        "Primary": "Phen2Gene",
        "Fallback": "local HPO-gene",
        "Primary failure": "timeout",
        "Method": "direct HPO-gene overlap",
    }
