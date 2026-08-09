"""Deterministic direct HPO-to-gene overlap fallback."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict


LOCAL_HPO_GENE_PROVIDER = "local_hpo_gene_fallback"
LOCAL_HPO_GENE_METHOD = "direct_hpo_gene_overlap"
LOCAL_HPO_GENE_METHOD_VERSION = "1.0"
HPO_ID_PATTERN = re.compile(r"HP:[0-9]{7}")
GENE_SYMBOL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class LocalHPOGeneFallbackError(ValueError):
    """Raised when local fallback input violates the bounded contract."""


class LocalHPOGeneRank(TypedDict):
    """One explainable direct-overlap gene result."""

    gene: str
    score: float
    matched_hpos: list[str]
    match_count: int
    rank: int
    provider: str
    method: str


class LocalHPOGeneFallbackResult(TypedDict):
    """Bounded local ranking with dataset and method provenance."""

    status: Literal["success", "no_match"]
    provider: str
    method: str
    method_version: str
    dataset_version: str | None
    dataset_date: str | None
    accepted_hpo_terms: list[str]
    rankings: list[LocalHPOGeneRank]


def rank_genes(
    hpo_ids: Sequence[str],
    hpo_to_genes: Mapping[str, Sequence[str]],
    *,
    dataset_version: str | None,
    dataset_date: str | None,
) -> LocalHPOGeneFallbackResult:
    """Rank genes by direct HPO overlap with deterministic tie-breaking."""

    accepted_hpo_terms = _validated_hpo_ids(hpo_ids)
    validated_mapping = _validated_mapping(
        hpo_to_genes,
        accepted_hpo_terms,
    )
    _validate_optional_provenance(dataset_version, "dataset_version")
    _validate_optional_provenance(dataset_date, "dataset_date")

    matches: dict[str, set[str]] = defaultdict(set)
    canonical_genes: dict[str, str] = {}
    for hpo_id in accepted_hpo_terms:
        for gene in validated_mapping.get(hpo_id, ()):
            gene_key = gene.casefold()
            canonical_genes.setdefault(gene_key, gene)
            matches[gene_key].add(hpo_id)

    total_hpo_count = len(accepted_hpo_terms)
    ordered = sorted(
        matches,
        key=lambda gene_key: (
            -len(matches[gene_key]),
            canonical_genes[gene_key],
        ),
    )
    rankings: list[LocalHPOGeneRank] = []
    for rank, gene_key in enumerate(ordered, start=1):
        matched_hpos = sorted(matches[gene_key])
        rankings.append(
            {
                "gene": canonical_genes[gene_key],
                "score": round(
                    len(matched_hpos) / total_hpo_count,
                    4,
                ),
                "matched_hpos": matched_hpos,
                "match_count": len(matched_hpos),
                "rank": rank,
                "provider": LOCAL_HPO_GENE_PROVIDER,
                "method": LOCAL_HPO_GENE_METHOD,
            }
        )

    return {
        "status": "success" if rankings else "no_match",
        "provider": LOCAL_HPO_GENE_PROVIDER,
        "method": LOCAL_HPO_GENE_METHOD,
        "method_version": LOCAL_HPO_GENE_METHOD_VERSION,
        "dataset_version": dataset_version,
        "dataset_date": dataset_date,
        "accepted_hpo_terms": accepted_hpo_terms,
        "rankings": rankings,
    }


def _validated_hpo_ids(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise LocalHPOGeneFallbackError("HPO IDs must be a sequence.")
    accepted: list[str] = []
    seen: set[str] = set()
    for hpo_id in value:
        if not isinstance(hpo_id, str) or HPO_ID_PATTERN.fullmatch(
            hpo_id
        ) is None:
            raise LocalHPOGeneFallbackError("Invalid HPO ID.")
        if hpo_id not in seen:
            accepted.append(hpo_id)
            seen.add(hpo_id)
    return accepted


def _validated_mapping(
    value: object,
    requested_hpo_terms: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise LocalHPOGeneFallbackError(
            "HPO-to-gene associations must be a mapping."
        )
    result: dict[str, tuple[str, ...]] = {}
    for hpo_id in requested_hpo_terms:
        genes = value.get(hpo_id, ())
        if isinstance(genes, (str, bytes)) or not isinstance(
            genes,
            Sequence,
        ):
            raise LocalHPOGeneFallbackError(
                "Associated genes must be a sequence."
            )
        canonical: dict[str, str] = {}
        for gene in genes:
            if not isinstance(gene, str) or GENE_SYMBOL_PATTERN.fullmatch(
                gene
            ) is None:
                raise LocalHPOGeneFallbackError(
                    "Association mapping contains an invalid gene symbol."
                )
            canonical.setdefault(gene.casefold(), gene)
        result[hpo_id] = tuple(
            sorted(canonical.values(), key=lambda gene: gene.casefold())
        )
    return result


def _validate_optional_provenance(value: object, field: str) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 100
        or any(ord(character) < 32 for character in value)
    ):
        raise LocalHPOGeneFallbackError(f"Invalid {field}.")
