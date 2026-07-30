"""Run an end-to-end smoke test against the local Stage 6 HPO data."""

import argparse
import sys
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.phenotype import (
    HPODataError,
    PhenotypeError,
    get_diseases_for_hpo,
    get_genes_for_hpo,
    match_phenotypes,
    normalize_phenotypes,
    search_hpo_terms,
)


DEFAULT_QUERY = "Seizure"
DEFAULT_HPO_IDS = ["HP:0001250", "HP:0001263"]
DEFAULT_GENE = "SCN1A"


def _parse_arguments() -> argparse.Namespace:
    """Read optional phenotype inputs for the local smoke test."""
    parser = argparse.ArgumentParser(
        description="Test the complete local Stage 6 phenotype workflow.",
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"Phenotype search phrase (default: {DEFAULT_QUERY}).",
    )
    parser.add_argument(
        "--hpo",
        action="append",
        dest="hpo_ids",
        help=(
            "Patient HPO ID; repeat this option for multiple terms "
            "(defaults: HP:0001250 and HP:0001263)."
        ),
    )
    parser.add_argument(
        "--gene",
        default=DEFAULT_GENE,
        help=f"Candidate gene symbol (default: {DEFAULT_GENE}).",
    )
    return parser.parse_args()


def check_stage_6(
    query: str,
    hpo_ids: list[str],
    gene: str,
) -> None:
    """Exercise search, normalization, associations, and scoring."""
    started_at = perf_counter()
    suggestions = search_hpo_terms(query, limit=5)
    if not suggestions:
        raise RuntimeError(
            f"No HPO suggestions were found for {query!r}."
        )

    terms = normalize_phenotypes(hpo_ids)
    primary_hpo_id = terms[0]["id"]
    gene_result = get_genes_for_hpo(primary_hpo_id)
    disease_result = get_diseases_for_hpo(primary_hpo_id)
    scored_candidates = match_phenotypes(
        [
            {"candidate": "selected_gene", "gene": gene},
            {"candidate": "missing_gene", "gene": None},
        ],
        [term["id"] for term in terms],
    )
    elapsed_seconds = perf_counter() - started_at

    selected_candidate = scored_candidates[0]
    missing_gene_candidate = scored_candidates[1]
    score = selected_candidate["phenotype_score"]
    if not isinstance(score, float) or not 0.0 <= score <= 1.0:
        raise RuntimeError("Phenotype score is outside the 0.0-1.0 range.")
    if missing_gene_candidate["phenotype_score"] != 0.0:
        raise RuntimeError(
            "A candidate without a gene received a phenotype score."
        )

    print(f"Search query: {query}")
    print(
        "Top suggestion: "
        f"{suggestions[0]['id']} ({suggestions[0]['name']})"
    )
    print(
        "Normalized HPO terms: "
        + ", ".join(term["id"] for term in terms)
    )
    print(
        f"Genes associated with {primary_hpo_id}: "
        f"{gene_result['gene_count']}"
    )
    print(
        f"Diseases associated with {primary_hpo_id}: "
        f"{disease_result['disease_count']}"
    )
    print(f"Candidate gene: {selected_candidate['gene']}")
    print(
        "Matched HPO terms: "
        + (
            ", ".join(selected_candidate["matched_hpo_terms"])
            or "none"
        )
    )
    print(f"Phenotype score: {score:.4f}")
    print(f"Response time: {elapsed_seconds:.2f} seconds")


def main() -> int:
    """Run the complete local phenotype smoke test."""
    arguments = _parse_arguments()
    hpo_ids = arguments.hpo_ids or DEFAULT_HPO_IDS

    try:
        check_stage_6(arguments.query, hpo_ids, arguments.gene)
    except (HPODataError, PhenotypeError, RuntimeError) as exc:
        print(f"\nPhenotype smoke test: FAILED ({exc})", file=sys.stderr)
        return 1

    print("\nPhenotype smoke test: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
