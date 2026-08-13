"""Local validation and explicit acceptance of model-suggested HPO terms."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, TypedDict

from backend.phenotype import (
    MAX_PATIENT_HPO_TERMS,
    HPOTerm,
    PhenotypeError,
    lookup_hpo_term,
    normalize_phenotypes,
    search_hpo_terms,
    validate_hpo_id,
)
from backend.phenotype_llm import MAX_PHENOTYPE_CANDIDATES
from backend.privacy import ClinicalDataPrivacyError, validate_llm_payload


MAX_SOURCE_PHRASE_CHARACTERS = 500
CandidateRejectionReason = Literal[
    "invalid_candidate",
    "invalid_hpo_id",
    "not_found",
    "duplicate",
    "label_mismatch",
    "invalid_source_phrase",
    "prohibited_content",
]


class PhenotypeSelectionError(ValueError):
    """Raised when an HPO candidate set cannot be safely accepted."""


class ValidatedHPOCandidate(TypedDict):
    """One suggestion resolved against the installed local ontology."""

    hpo_id: str
    label: str
    source_phrase_fa: str


class RejectedHPOCandidate(TypedDict):
    """One candidate excluded before human review."""

    hpo_id: str
    reason: CandidateRejectionReason


class HPOCandidateValidationResult(TypedDict):
    """Complete local validation outcome for one candidate collection."""

    validated_candidates: list[ValidatedHPOCandidate]
    rejected_candidates: list[RejectedHPOCandidate]


def _candidate_id(value: object) -> str:
    """Return a bounded display-only candidate identifier."""

    if not isinstance(value, str):
        return ""
    return value.strip()[:32]


def _normalized_ontology_label(value: str) -> str:
    """Normalize one English ontology label for exact local comparison."""

    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _source_phrase(value: object) -> tuple[str | None, CandidateRejectionReason | None]:
    """Validate editable source text without trusting browser state."""

    if not isinstance(value, str):
        return None, "invalid_source_phrase"
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_SOURCE_PHRASE_CHARACTERS
        or any(
            ord(character) < 32
            and character not in {"\n", "\r", "\t"}
            for character in normalized
        )
    ):
        return None, "invalid_source_phrase"
    try:
        validate_llm_payload({"source_phrase_fa": normalized})
    except ClinicalDataPrivacyError:
        return None, "prohibited_content"
    return normalized, None


def validate_hpo_candidates(
    candidates: object,
    *,
    ontology_path: str | Path | None = None,
) -> HPOCandidateValidationResult:
    """Resolve every suggested identifier against the local HPO ontology."""

    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes, bytearray))
    ):
        raise PhenotypeSelectionError(
            "HPO candidates must be provided as a sequence."
        )
    if len(candidates) > MAX_PHENOTYPE_CANDIDATES:
        raise PhenotypeSelectionError(
            "The candidate set exceeds the extraction limit."
        )

    validated: list[ValidatedHPOCandidate] = []
    rejected: list[RejectedHPOCandidate] = []
    seen_canonical_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            rejected.append(
                {"hpo_id": "", "reason": "invalid_candidate"}
            )
            continue
        raw_id = _candidate_id(candidate.get("hpo_id"))
        source_phrase, source_error = _source_phrase(
            candidate.get("source_phrase_fa")
        )
        if source_error is not None:
            rejected.append({"hpo_id": raw_id, "reason": source_error})
            continue
        try:
            normalized_id = validate_hpo_id(candidate.get("hpo_id"))
        except PhenotypeError:
            rejected.append(
                {"hpo_id": raw_id, "reason": "invalid_hpo_id"}
            )
            continue
        try:
            term = lookup_hpo_term(
                normalized_id,
                ontology_path=ontology_path,
            )
        except PhenotypeError:
            rejected.append(
                {"hpo_id": normalized_id, "reason": "not_found"}
            )
            continue
        raw_label = candidate.get("label")
        if (
            not isinstance(raw_label, str)
            or not raw_label.strip()
            or len(raw_label.strip()) > 200
        ):
            rejected.append(
                {"hpo_id": normalized_id, "reason": "label_mismatch"}
            )
            continue
        try:
            label_matches = search_hpo_terms(
                raw_label.strip(),
                limit=50,
                ontology_path=ontology_path,
            )
        except PhenotypeError:
            label_matches = []
        normalized_label = _normalized_ontology_label(raw_label)
        exact_label_ids = {
            match["id"]
            for match in label_matches
            if _normalized_ontology_label(match["matched_label"])
            == normalized_label
        }
        if term["id"] not in exact_label_ids:
            if len(exact_label_ids) != 1:
                rejected.append(
                    {
                        "hpo_id": normalized_id,
                        "reason": "label_mismatch",
                    }
                )
                continue
            corrected_id = next(iter(exact_label_ids))
            term = lookup_hpo_term(
                corrected_id,
                ontology_path=ontology_path,
            )
        if term["id"] in seen_canonical_ids:
            rejected.append(
                {"hpo_id": normalized_id, "reason": "duplicate"}
            )
            continue
        seen_canonical_ids.add(term["id"])
        validated.append(
            {
                "hpo_id": term["id"],
                "label": term["name"],
                "source_phrase_fa": source_phrase,
            }
        )

    return {
        "validated_candidates": validated,
        "rejected_candidates": rejected,
    }


def accept_hpo_candidates(
    candidates: object,
    *,
    existing_hpo_ids: Sequence[str] = (),
    ontology_path: str | Path | None = None,
) -> list[HPOTerm]:
    """Revalidate edited candidates and merge them after explicit acceptance."""

    validation = validate_hpo_candidates(
        candidates,
        ontology_path=ontology_path,
    )
    if validation["rejected_candidates"]:
        raise PhenotypeSelectionError(
            "Every accepted HPO candidate must pass local validation."
        )
    candidate_ids = [
        candidate["hpo_id"]
        for candidate in validation["validated_candidates"]
    ]
    if not candidate_ids:
        raise PhenotypeSelectionError(
            "Select at least one locally validated HPO candidate."
        )
    if (
        not isinstance(existing_hpo_ids, Sequence)
        or isinstance(existing_hpo_ids, (str, bytes, bytearray))
    ):
        raise PhenotypeSelectionError(
            "Existing HPO terms must be provided as a sequence."
        )
    combined_ids = list(
        dict.fromkeys([*existing_hpo_ids, *candidate_ids])
    )
    if len(combined_ids) > MAX_PATIENT_HPO_TERMS:
        raise PhenotypeSelectionError(
            "The accepted phenotype set exceeds the 50-term limit."
        )
    try:
        return normalize_phenotypes(
            combined_ids,
            ontology_path=ontology_path,
        )
    except PhenotypeError as exc:
        raise PhenotypeSelectionError(
            "The accepted phenotype set failed local validation."
        ) from exc


__all__ = [
    "CandidateRejectionReason",
    "HPOCandidateValidationResult",
    "PhenotypeSelectionError",
    "RejectedHPOCandidate",
    "ValidatedHPOCandidate",
    "accept_hpo_candidates",
    "validate_hpo_candidates",
]
