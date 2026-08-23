"""RED reproduction for the Stage 6.1 clinical-entity contract gap."""

from __future__ import annotations

import json

import pytest

from backend.llm import LLMClient, LLMRequest, LLMResponse
from backend.phenotype_llm import extract_hpo_candidates
from backend.pipeline import validate_analysis_context


class _MixedClinicalEntityAdapter:
    """Return the current schema's best representation of a mixed narrative."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(
            content=json.dumps(
                {
                    "phenotype_candidates": [
                        {
                            "hpo_id": "HP:0000618",
                            "label": "Blindness",
                            "source_phrase_fa": "نابینایی دارد",
                        }
                    ],
                    "disease_mentions": [
                        {"source_phrase_fa": "سندروم والنبرگ دارد"}
                    ],
                    "negated_phenotype_mentions": [],
                    "uncertain_phenotype_mentions": [],
                    "unmapped_clinical_phrases": [],
                },
                ensure_ascii=False,
            ),
            model="stage6-1-audit-model",
            finish_reason="stop",
        )


@pytest.mark.testing_v3_phenotype
def test_explicit_persian_disease_survives_into_analysis_context() -> None:
    """A recognized disease must not disappear at the pipeline handoff."""

    extraction = extract_hpo_candidates(
        "بیمار نابینایی دارد و سندروم والنبرگ دارد.",
        client=LLMClient(_MixedClinicalEntityAdapter()),
    )

    assert extraction["disease_mentions"] == [
        {"source_phrase_fa": "سندروم والنبرگ دارد"}
    ]
    clinical_entities = [
        {
            "original_text": "سندروم والنبرگ دارد",
            "normalized_text": "سندروم والنبرگ",
            "entity_type": "DISEASE",
            "assertion": "PRESENT",
        }
    ]

    context = validate_analysis_context(
        {
            "input_type": "manual",
            "accepted_hpo_terms": ["HP:0000618"],
            "phenotype_extraction_model": extraction["model"],
            "variant_interpretation_model": "interpretation-model",
            "phenotype_extraction_provenance": {
                "schema_version": extraction["schema_version"],
                "task": extraction["task"],
                "prompt_version": extraction["prompt_version"],
                "model": extraction["model"],
                "candidate_hpo_ids": ["HP:0000618"],
            },
            "clinical_entities": clinical_entities,
        }
    )

    assert context["clinical_entities"] == clinical_entities
