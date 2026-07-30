"""Phenotype and Human Phenotype Ontology input handling."""

import re


HPO_ID_PATTERN = re.compile(r"HP:[0-9]{7}")


class PhenotypeError(ValueError):
    """Raised when phenotype input is invalid."""


def validate_hpo_id(hpo_id: str) -> str:
    """Validate and return one canonical Human Phenotype Ontology ID.

    This function validates syntax only. Confirming that the term exists in
    the configured HPO dataset belongs to the next phenotype implementation
    step.
    """
    if not isinstance(hpo_id, str):
        raise PhenotypeError(
            "HPO ID must be a string in HP:0000000 format."
        )

    normalized_id = hpo_id.strip()
    if HPO_ID_PATTERN.fullmatch(normalized_id) is None:
        raise PhenotypeError(
            "HPO ID must use HP: followed by exactly seven digits."
        )

    return normalized_id
