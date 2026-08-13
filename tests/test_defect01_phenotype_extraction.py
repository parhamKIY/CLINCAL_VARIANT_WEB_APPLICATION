"""Regression tests for Defect 01 Persian phenotype extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.llm import LLMClient, LLMRequest, LLMResponse
from backend.phenotype_llm import (
    PHENOTYPE_EXTRACTION_PROMPT_VERSION,
    PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA,
    PHENOTYPE_EXTRACTION_SCHEMA_VERSION,
    PHENOTYPE_EXTRACTION_SYSTEM_PROMPT,
    PhenotypeExtractionError,
    extract_hpo_candidates,
)
from backend.phenotype_selection import validate_hpo_candidates


class _Adapter:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="phenotype-model",
            finish_reason="stop",
        )


def _empty_payload() -> dict[str, object]:
    return {
        "phenotype_candidates": [],
        "disease_mentions": [],
        "negated_phenotype_mentions": [],
        "uncertain_phenotype_mentions": [],
        "unmapped_clinical_phrases": [],
    }


@pytest.mark.testing_v3_phenotype
def test_single_persian_phenotype_remains_supported() -> None:
    payload = _empty_payload()
    payload["phenotype_candidates"] = [
        {
            "hpo_id": "HP:0001250",
            "label": "Seizure",
            "source_phrase_fa": "حملات تشنج",
        }
    ]

    result = extract_hpo_candidates(
        "کودک دچار حملات تشنج است.",
        client=LLMClient(_Adapter(payload)),
    )

    assert result["phenotype_candidates"] == payload[
        "phenotype_candidates"
    ]


@pytest.mark.testing_v3_phenotype
def test_multi_concept_result_keeps_non_hpo_mentions_separate() -> None:
    payload = {
        "phenotype_candidates": [
            {
                "hpo_id": "HP:0000365",
                "label": "Hearing impairment",
                "source_phrase_fa": "ناشنواست",
            },
            {
                "hpo_id": "HP:0001250",
                "label": "Seizure",
                "source_phrase_fa": "تشنج دارد",
            },
        ],
        "disease_mentions": [
            {"source_phrase_fa": "سندرم واردنبرگ دارد"}
        ],
        "negated_phenotype_mentions": [
            {"source_phrase_fa": "ضعف عضلانی ندارد"}
        ],
        "uncertain_phenotype_mentions": [
            {"source_phrase_fa": "احتمال آتاکسی مطرح است"}
        ],
        "unmapped_clinical_phrases": [
            {"source_phrase_fa": "برای پیگیری مراجعه کرده است"}
        ],
    }
    result = extract_hpo_candidates(
        "بیمار ناشنواست و تشنج دارد. سندرم واردنبرگ دارد. "
        "ضعف عضلانی ندارد. احتمال آتاکسی مطرح است. "
        "برای پیگیری مراجعه کرده است.",
        client=LLMClient(_Adapter(payload)),
    )

    assert result["schema_version"] == PHENOTYPE_EXTRACTION_SCHEMA_VERSION
    assert result["prompt_version"] == PHENOTYPE_EXTRACTION_PROMPT_VERSION
    assert result["phenotype_candidates"] == payload["phenotype_candidates"]
    assert result["disease_mentions"] == payload["disease_mentions"]
    assert result["negated_phenotype_mentions"] == (
        payload["negated_phenotype_mentions"]
    )
    assert result["uncertain_phenotype_mentions"] == (
        payload["uncertain_phenotype_mentions"]
    )
    assert result["unmapped_clinical_phrases"] == (
        payload["unmapped_clinical_phrases"]
    )


@pytest.mark.testing_v3_phenotype
def test_prompt_and_schema_require_exhaustive_mention_classification() -> None:
    prompt = PHENOTYPE_EXTRACTION_SYSTEM_PROMPT.casefold()
    for requirement in (
        "all independent clinical mentions",
        "disease or syndrome",
        "negated",
        "uncertain",
        "exactly one output category",
        "verbatim persian source phrase",
    ):
        assert requirement in prompt

    schema = PHENOTYPE_EXTRACTION_RESPONSE_SCHEMA.schema
    assert schema["required"] == list(_empty_payload())
    assert set(schema["properties"]) == set(_empty_payload())


@pytest.mark.testing_v3_phenotype
def test_colloquial_source_phrase_is_grounded_after_persian_normalization() -> None:
    payload = _empty_payload()
    payload["phenotype_candidates"] = [
        {
            "hpo_id": "HP:0000365",
            "label": "Hearing impairment",
            "source_phrase_fa": "گوش‌هاش خوب نمی‌شنوه",
        }
    ]

    result = extract_hpo_candidates(
        "كودك می‌گوید گوش‌هاش خوب نمی‌شنوه.",
        client=LLMClient(_Adapter(payload)),
    )

    assert len(result["phenotype_candidates"]) == 1


@pytest.mark.testing_v3_phenotype
def test_ungrounded_source_phrase_is_rejected() -> None:
    payload = _empty_payload()
    payload["phenotype_candidates"] = [
        {
            "hpo_id": "HP:0001250",
            "label": "Seizure",
            "source_phrase_fa": "تشنج مکرر",
        }
    ]

    with pytest.raises(PhenotypeExtractionError, match="not grounded"):
        extract_hpo_candidates(
            "بیمار دچار ضعف عضلانی است.",
            client=LLMClient(_Adapter(payload)),
        )


@pytest.mark.testing_v3_phenotype
def test_duplicate_non_hpo_mention_across_categories_is_rejected() -> None:
    payload = _empty_payload()
    payload["disease_mentions"] = [
        {"source_phrase_fa": "سندرم واردنبرگ دارد"}
    ]
    payload["unmapped_clinical_phrases"] = [
        {"source_phrase_fa": "سندرم واردنبرگ دارد"}
    ]

    with pytest.raises(PhenotypeExtractionError, match="multiple categories"):
        extract_hpo_candidates(
            "بیمار سندرم واردنبرگ دارد.",
            client=LLMClient(_Adapter(payload)),
        )


@pytest.mark.testing_v3_phenotype
def test_syndrome_phrase_cannot_be_returned_as_hpo_candidate() -> None:
    payload = _empty_payload()
    payload["phenotype_candidates"] = [
        {
            "hpo_id": "HP:0000365",
            "label": "Hearing impairment",
            "source_phrase_fa": "سندرم واردنبرگ",
        }
    ]

    with pytest.raises(PhenotypeExtractionError, match="disease or syndrome"):
        extract_hpo_candidates(
            "بیمار سندرم واردنبرگ دارد.",
            client=LLMClient(_Adapter(payload)),
        )


@pytest.mark.testing_v3_phenotype
def test_local_validation_corrects_id_from_unique_ontology_label(
    tmp_path: Path,
) -> None:
    ontology_path = tmp_path / "hp.obo"
    ontology_path.write_text(
        "format-version: 1.2\n\n"
        "[Term]\n"
        "id: HP:0000365\n"
        "name: Hearing impairment\n"
        'synonym: "Deafness" EXACT []\n\n'
        "[Term]\n"
        "id: HP:0000429\n"
        "name: Abnormal morphology of the nasal alae\n",
        encoding="utf-8",
    )

    result = validate_hpo_candidates(
        [
            {
                "hpo_id": "HP:0000429",
                "label": "Deafness",
                "source_phrase_fa": "ناشنواست",
            },
        ],
        ontology_path=ontology_path,
    )

    assert result["validated_candidates"] == [
        {
            "hpo_id": "HP:0000365",
            "label": "Hearing impairment",
            "source_phrase_fa": "ناشنواست",
        }
    ]
    assert result["rejected_candidates"] == []
