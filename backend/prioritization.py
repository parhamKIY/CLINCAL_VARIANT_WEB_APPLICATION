"""Candidate variant selection for the MVP pipeline."""

import random
from collections.abc import Iterable

from backend.vcf_processing import VariantData
from config import settings


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------

# Prioritization receives standardized objects from vcf_processing.py.
# Additional annotation fields can be added later without changing this
# minimum contract.
REQUIRED_VARIANT_FIELDS = {
    "chrom",
    "pos",
    "ref",
    "alt",
}


class PrioritizationError(ValueError):
    """Raised when candidate selection receives invalid input or settings."""


def _validate_top_n(top_n: int | None) -> int:
    """Resolve and validate the requested number of candidates."""
    resolved_top_n = settings.TOP_VARIANTS if top_n is None else top_n

    if (
        isinstance(resolved_top_n, bool)
        or not isinstance(resolved_top_n, int)
        or resolved_top_n <= 0
    ):
        raise PrioritizationError(
            "top_n must be a positive integer."
        )

    return resolved_top_n


def _validate_variant(
    variant: VariantData,
    index: int,
) -> None:
    """Require the standardized fields produced by VCF processing."""
    if not isinstance(variant, dict):
        raise PrioritizationError(
            f"Variant at index {index} must be a dictionary."
        )

    missing_fields = REQUIRED_VARIANT_FIELDS.difference(variant)
    if missing_fields:
        missing_text = ", ".join(sorted(missing_fields))
        raise PrioritizationError(
            f"Variant at index {index} is missing: {missing_text}."
        )

    if not variant["alt"]:
        raise PrioritizationError(
            f"Variant at index {index} has no ALT allele."
        )


# ---------------------------------------------------------------------------
# Temporary random-selection strategy
# ---------------------------------------------------------------------------

def _select_random_candidates(
    variants: Iterable[VariantData],
    top_n: int,
    rng: random.Random,
) -> list[VariantData]:
    """Select a uniform random sample while keeping memory bounded.

    Reservoir sampling gives every input variant the same selection
    probability without loading the complete VCF into memory.
    """
    candidates: list[VariantData] = []

    for seen_count, variant in enumerate(variants, start=1):
        _validate_variant(variant, seen_count - 1)

        # Fill the reservoir with the first top_n valid variants.
        if len(candidates) < top_n:
            candidates.append(variant)
            continue

        # Replace an existing candidate with decreasing probability. This
        # produces a uniform sample across all variants seen in the stream.
        replacement_index = rng.randrange(seen_count)
        if replacement_index < top_n:
            candidates[replacement_index] = variant

    # Randomize output order as well as membership. Later scoring strategies
    # can replace this with a clinically meaningful ranked order.
    rng.shuffle(candidates)
    return candidates


# ---------------------------------------------------------------------------
# Public entry point used by the pipeline
# ---------------------------------------------------------------------------

def prioritize_variants(
    variants: Iterable[VariantData],
    top_n: int | None = None,
    seed: int | None = None,
) -> list[VariantData]:
    """Select candidate variants using the current MVP strategy.

    Random selection is a temporary engineering placeholder and must not be
    interpreted as clinical prioritization. The pipeline depends only on this
    public function, so the internal strategy can later be replaced with
    filtering, scoring, phenotype matching, or inheritance-aware ranking.

    Args:
        variants: A list or streaming iterable of standardized variants.
        top_n: Candidate count. Defaults to settings.TOP_VARIANTS.
        seed: Optional deterministic seed for tests and reproducible demos.
    """
    candidate_limit = _validate_top_n(top_n)
    rng = random.Random(seed)

    return _select_random_candidates(
        variants=variants,
        top_n=candidate_limit,
        rng=rng,
    )
