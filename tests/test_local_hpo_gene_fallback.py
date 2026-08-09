"""Stage 65 direct local HPO-to-gene fallback tests."""

from __future__ import annotations

import pytest

from backend.local_hpo_gene_fallback import (
    LOCAL_HPO_GENE_METHOD,
    LOCAL_HPO_GENE_METHOD_VERSION,
    LOCAL_HPO_GENE_PROVIDER,
    LocalHPOGeneFallbackError,
    rank_genes,
)
from frontend.results import build_phenotype_rows


def run_rank(
    hpo_ids: list[str],
    associations: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    return rank_genes(
        hpo_ids,
        associations,
        dataset_version="hp/releases/2026-07-13",
        dataset_date="2026-07-13",
    )


def test_one_hpo_one_gene() -> None:
    result = run_rank(
        ["HP:0001250"],
        {"HP:0001250": ("SCN1A",)},
    )
    assert result["status"] == "success"
    assert result["rankings"] == [
        {
            "gene": "SCN1A",
            "score": 1.0,
            "matched_hpos": ["HP:0001250"],
            "match_count": 1,
            "rank": 1,
            "provider": LOCAL_HPO_GENE_PROVIDER,
            "method": LOCAL_HPO_GENE_METHOD,
        }
    ]


def test_one_hpo_multiple_genes_are_sorted() -> None:
    result = run_rank(
        ["HP:0001250"],
        {"HP:0001250": ("SCN2A", "SCN1A")},
    )
    assert [item["gene"] for item in result["rankings"]] == [
        "SCN1A",
        "SCN2A",
    ]


def test_multiple_hpos_use_direct_overlap_fraction() -> None:
    result = run_rank(
        ["HP:0001250", "HP:0001263", "HP:0004322"],
        {
            "HP:0001250": ("SCN1A", "SCN2A"),
            "HP:0001263": ("SCN1A",),
        },
    )
    first, second = result["rankings"]
    assert first["gene"] == "SCN1A"
    assert first["score"] == 0.6667
    assert first["matched_hpos"] == ["HP:0001250", "HP:0001263"]
    assert second["gene"] == "SCN2A"
    assert second["score"] == 0.3333


def test_duplicate_hpos_and_associations_are_collapsed() -> None:
    result = run_rank(
        ["HP:0001250", "HP:0001250"],
        {"HP:0001250": ("SCN1A", "SCN1A", "scn1a")},
    )
    assert result["accepted_hpo_terms"] == ["HP:0001250"]
    assert len(result["rankings"]) == 1
    assert result["rankings"][0]["score"] == 1.0


def test_unknown_and_empty_hpo_sets_return_no_match() -> None:
    unknown = run_rank(
        ["HP:9999999"],
        {"HP:0001250": ("SCN1A",)},
    )
    empty = run_rank([], {"HP:0001250": ("SCN1A",)})
    assert unknown["status"] == "no_match"
    assert unknown["rankings"] == []
    assert empty["status"] == "no_match"
    assert empty["rankings"] == []


def test_ties_are_deterministic_across_mapping_order() -> None:
    first = run_rank(
        ["HP:0001250"],
        {"HP:0001250": ("PAX3", "BRCA1")},
    )
    second = run_rank(
        ["HP:0001250"],
        {"HP:0001250": ("BRCA1", "PAX3")},
    )
    assert first["rankings"] == second["rankings"]


def test_method_and_dataset_provenance_are_explicit() -> None:
    result = run_rank(
        ["HP:0001250"],
        {"HP:0001250": ("SCN1A",)},
    )
    assert result["provider"] == LOCAL_HPO_GENE_PROVIDER
    assert result["method"] == LOCAL_HPO_GENE_METHOD
    assert result["method_version"] == LOCAL_HPO_GENE_METHOD_VERSION
    assert result["dataset_version"] == "hp/releases/2026-07-13"
    assert result["dataset_date"] == "2026-07-13"


def test_frontend_wording_keeps_local_method_distinct() -> None:
    rows = build_phenotype_rows(
        [
            {
                "variant": {
                    "chrom": "2",
                    "pos": 166848215,
                    "ref": "C",
                    "alt": "T",
                },
                "gene": "SCN1A",
                "phen2gene": {
                    "availability": "available",
                    "provider": LOCAL_HPO_GENE_PROVIDER,
                    "score": 1.0,
                    "rank": 1,
                    "method": LOCAL_HPO_GENE_METHOD,
                    "status": "direct_match",
                    "fallback_used": True,
                    "primary_failure": "timeout",
                },
            }
        ]
    )
    assert rows[0]["Phenotype-gene provider"] == LOCAL_HPO_GENE_PROVIDER
    assert rows[0]["Phenotype-gene method"] == LOCAL_HPO_GENE_METHOD
    assert rows[0]["Fallback used"] is True
    assert "Phen2Gene score" not in rows[0]


@pytest.mark.parametrize(
    ("hpo_ids", "associations"),
    [
        (["bad"], {}),
        (["HP:0001250"], {"HP:0001250": ("bad gene",)}),
    ],
)
def test_malformed_inputs_fail_closed(
    hpo_ids: list[str],
    associations: dict[str, tuple[str, ...]],
) -> None:
    with pytest.raises(LocalHPOGeneFallbackError):
        run_rank(hpo_ids, associations)
