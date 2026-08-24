"""Shared report-safe interpretation narrative projection helpers."""

from __future__ import annotations


_NORMALIZED_PHENOTYPE_PREFIXES = frozenset(
    {
        "Phenotype conclusion: no supported association found.",
        "Phenotype conclusion: phenotype evidence unavailable.",
    }
)


def substantive_interpretation_narrative(value: str) -> str:
    """Return the model narrative without its deterministic context prefix."""

    normalized = value.strip()
    paragraphs = [item.strip() for item in normalized.split("\n\n") if item.strip()]
    if (
        len(paragraphs) > 1
        and paragraphs[0] in _NORMALIZED_PHENOTYPE_PREFIXES
    ):
        return "\n\n".join(paragraphs[1:])
    return normalized


__all__ = ["substantive_interpretation_narrative"]
