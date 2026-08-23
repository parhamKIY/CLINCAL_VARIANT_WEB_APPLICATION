"""Stage 6.2 clinical-entity extraction and transport contract tests."""

from __future__ import annotations

import json

import pytest

import frontend.execution as frontend_execution
from backend.clinical_entities import (
    ClinicalEntityError,
    normalize_clinical_entity_text,
    validate_clinical_entities,
)
from backend.llm import LLMClient, LLMRequest, LLMResponse
from backend.pipeline import create_pipeline_result, validate_analysis_context
from backend.phenotype_llm import (
    PHENOTYPE_EXTRACTION_SCHEMA_VERSION,
    PhenotypeExtractionError,
    extract_hpo_candidates,
)
from frontend.execution import prepare_analysis_recovery_request


class _Adapter:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="stage6-2-model",
            finish_reason="stop",
        )


def _entity_payload(
    *,
    original_text: str,
    entity_type: str,
    assertion: str,
    hpo_id: str | None = None,
    label: str | None = None,
) -> dict[str, object]:
    return {
        "original_text": original_text,
        "entity_type": entity_type,
        "assertion": assertion,
        "hpo_id": hpo_id,
        "label": label,
    }


def _response_payload(
    entities: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "clinical_entities": entities,
        "unmapped_clinical_phrases": [],
    }


@pytest.mark.testing_v3_phenotype
def test_mixed_persian_entities_preserve_type_and_assertion() -> None:
    result = extract_hpo_candidates(
        "بیمار نابینایی دارد و سابقه سندروم والنبرگ دارد.",
        client=LLMClient(
            _Adapter(
                _response_payload(
                    [
                        _entity_payload(
                            original_text="نابینایی",
                            entity_type="PHENOTYPE",
                            assertion="PRESENT",
                            hpo_id="HP:0000618",
                            label="Blindness",
                        ),
                        _entity_payload(
                            original_text="سندروم والنبرگ",
                            entity_type="DISEASE",
                            assertion="HISTORICAL",
                        ),
                    ]
                )
            )
        ),
    )

    assert result["schema_version"] == PHENOTYPE_EXTRACTION_SCHEMA_VERSION
    assert result["clinical_entities"] == [
        {
            "original_text": "نابینایی",
            "normalized_text": "نابینایی",
            "entity_type": "PHENOTYPE",
            "assertion": "PRESENT",
        },
        {
            "original_text": "سندروم والنبرگ",
            "normalized_text": "سندروم والنبرگ",
            "entity_type": "DISEASE",
            "assertion": "HISTORICAL",
        },
    ]
    assert result["phenotype_candidates"] == [
        {
            "hpo_id": "HP:0000618",
            "label": "Blindness",
            "source_phrase_fa": "نابینایی",
        }
    ]
    assert result["disease_mentions"] == [
        {"source_phrase_fa": "سندروم والنبرگ"}
    ]


@pytest.mark.testing_v3_phenotype
@pytest.mark.parametrize(
    "assertion",
    ["PRESENT", "SUSPECTED", "NEGATED", "HISTORICAL"],
)
def test_disease_assertion_states_are_preserved(assertion: str) -> None:
    result = extract_hpo_candidates(
        "سندروم والنبرگ",
        client=LLMClient(
            _Adapter(
                _response_payload(
                    [
                        _entity_payload(
                            original_text="سندروم والنبرگ",
                            entity_type="DISEASE",
                            assertion=assertion,
                        )
                    ]
                )
            )
        ),
    )

    assert result["clinical_entities"][0]["assertion"] == assertion


@pytest.mark.testing_v3_phenotype
def test_unknown_entity_type_is_rejected() -> None:
    with pytest.raises(PhenotypeExtractionError, match="entity type"):
        extract_hpo_candidates(
            "سندروم والنبرگ",
            client=LLMClient(
                _Adapter(
                    _response_payload(
                        [
                            _entity_payload(
                                original_text="سندروم والنبرگ",
                                entity_type="DIAGNOSIS",
                                assertion="PRESENT",
                            )
                        ]
                    )
                )
            ),
        )


@pytest.mark.testing_v3_phenotype
def test_disease_entity_cannot_carry_an_hpo_identifier() -> None:
    with pytest.raises(PhenotypeExtractionError, match="must not carry HPO"):
        extract_hpo_candidates(
            "سندروم والنبرگ",
            client=LLMClient(
                _Adapter(
                    _response_payload(
                        [
                            _entity_payload(
                                original_text="سندروم والنبرگ",
                                entity_type="DISEASE",
                                assertion="PRESENT",
                                hpo_id="HP:0000618",
                                label="Blindness",
                            )
                        ]
                    )
                )
            ),
        )


@pytest.mark.testing_v3_phenotype
def test_symptoms_cannot_introduce_an_ungrounded_disease_name() -> None:
    with pytest.raises(PhenotypeExtractionError, match="not grounded"):
        extract_hpo_candidates(
            "بیمار سرگیجه و اختلال بلع دارد.",
            client=LLMClient(
                _Adapter(
                    _response_payload(
                        [
                            _entity_payload(
                                original_text="سندروم والنبرگ",
                                entity_type="DISEASE",
                                assertion="PRESENT",
                            )
                        ]
                    )
                )
            ),
        )


def test_clinical_entity_validation_is_bounded_and_deterministic() -> None:
    assert normalize_clinical_entity_text("  سندروم\u200cوالنبرگ  ") == (
        "سندروم والنبرگ"
    )
    assert validate_clinical_entities(
        [
            {
                "original_text": "سندروم\u200cوالنبرگ",
                "normalized_text": "سندروم والنبرگ",
                "entity_type": "DISEASE",
                "assertion": "PRESENT",
            }
        ]
    ) == [
        {
            "original_text": "سندروم\u200cوالنبرگ",
            "normalized_text": "سندروم والنبرگ",
            "entity_type": "DISEASE",
            "assertion": "PRESENT",
        }
    ]

    with pytest.raises(ClinicalEntityError, match="assertion"):
        validate_clinical_entities(
            [
                {
                    "original_text": "سندروم والنبرگ",
                    "normalized_text": "سندروم والنبرگ",
                    "entity_type": "DISEASE",
                    "assertion": "CONFIRMED",
                }
            ]
        )


def test_analysis_context_and_recovery_preserve_entities_separately() -> None:
    entities = [
        {
            "original_text": "سندروم والنبرگ",
            "normalized_text": "سندروم والنبرگ",
            "entity_type": "DISEASE",
            "assertion": "SUSPECTED",
        }
    ]
    context = validate_analysis_context(
        {
            "input_type": "manual",
            "accepted_hpo_terms": ["HP:0000618"],
            "clinical_entities": entities,
            "phenotype_extraction_model": "entity-model",
            "variant_interpretation_model": "interpretation-model",
            "phenotype_extraction_provenance": None,
        }
    )
    request = prepare_analysis_recovery_request(
        uploaded_vcf=None,
        manual_variants=[
            {
                "chrom": "1",
                "pos": 941284,
                "ref": "G",
                "alt": "A",
                "qual": None,
                "filter": None,
            }
        ],
        phenotypes=context["accepted_hpo_terms"],
        clinical_entities=context["clinical_entities"],
        llm_model=context["variant_interpretation_model"],
        input_type="manual",
        phenotype_extraction_model=context["phenotype_extraction_model"],
    )

    assert request["clinical_entities"] == entities
    assert request["phenotypes"] == ["HP:0000618"]
    assert create_pipeline_result()["analysis_context"]["clinical_entities"] == []


def test_execution_forwards_entities_without_adding_them_to_hpo_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entities = [
        {
            "original_text": "سندروم والنبرگ",
            "normalized_text": "سندروم والنبرگ",
            "entity_type": "DISEASE",
            "assertion": "PRESENT",
        }
    ]
    observed: dict[str, object] = {}

    def fake_run_analysis(**kwargs: object) -> object:
        observed.update(kwargs)
        return create_pipeline_result()

    monkeypatch.setattr(frontend_execution, "run_analysis", fake_run_analysis)
    frontend_execution.execute_analysis(
        uploaded_vcf=None,
        manual_variants=[],
        phenotypes=["HP:0000618"],
        clinical_entities=entities,
        llm_model="interpretation-model",
    )

    assert observed["clinical_entities"] == entities
    assert observed["phenotypes"] == ["HP:0000618"]
