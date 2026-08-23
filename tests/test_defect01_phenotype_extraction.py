"""Regression tests for Defect 01 Persian phenotype extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.llm import (
    LLMClient,
    LLMJSONObject,
    LLMRequest,
    LLMResponse,
    OpenAICompatibleAdapter,
)
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
    assert schema["required"] == [
        "clinical_entities",
        "unmapped_clinical_phrases",
    ]
    assert set(schema["properties"]) == set(schema["required"])
    entity_schema = schema["properties"]["clinical_entities"]["items"]
    assert entity_schema["required"] == [
        "original_text",
        "entity_type",
        "assertion",
        "hpo_id",
        "label",
    ]


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


class _MultiResponseFakeSession:
    """Fake HTTP session providing sequential responses for Adapter testing."""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = responses
        self.post_calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> _FakeHTTPResponse:
        self.post_calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("No more responses configured in fake session.")
        status_code, body = self.responses.pop(0)
        return _FakeHTTPResponse(status_code, body)


class _FakeHTTPResponse:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body

    def raise_for_status(self) -> None:
        pass


@pytest.mark.testing_v3_phenotype
def test_gemini_compatible_json_object_generation_path_works() -> None:
    """Direct LLMJSONObject generation path parses and validates correctly."""
    payload = {
        "phenotype_candidates": [
            {
                "hpo_id": "HP:0001250",
                "label": "Seizure",
                "source_phrase_fa": "تشنج دارد",
            }
        ],
        "disease_mentions": [],
        "negated_phenotype_mentions": [],
        "uncertain_phenotype_mentions": [],
        "unmapped_clinical_phrases": [],
    }
    adapter = _Adapter(payload)
    result = extract_hpo_candidates(
        "بیمار تشنج دارد.",
        client=LLMClient(adapter),
        response_format=LLMJSONObject(),
    )
    assert len(result["phenotype_candidates"]) == 1
    assert result["phenotype_candidates"][0]["hpo_id"] == "HP:0001250"
    assert adapter.requests[0].response_format == LLMJSONObject()


@pytest.mark.testing_v3_phenotype
def test_adapter_falls_back_to_json_object_on_gemini_schema_rejection() -> None:
    """When a provider rejects strict json_schema with HTTP 400, fallback to json_object succeeds."""
    extraction_payload = {
        "phenotype_candidates": [
            {
                "hpo_id": "HP:0001250",
                "label": "Seizure",
                "source_phrase_fa": "تشنج دارد",
            }
        ],
        "disease_mentions": [],
        "negated_phenotype_mentions": [],
        "uncertain_phenotype_mentions": [],
        "unmapped_clinical_phrases": [],
    }
    fake_session = _MultiResponseFakeSession(
        [
            (400, {"error": {"message": "Invalid JSON schema keyword: pattern"}}),
            (
                200,
                {
                    "model": "gemini-3.1-flash-lite",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(extraction_payload, ensure_ascii=False),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            ),
        ]
    )
    adapter = OpenAICompatibleAdapter(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test-key",
        model="gemini-3.1-flash-lite",
        timeout=10,
        session=fake_session,
    )
    result = extract_hpo_candidates(
        "بیمار تشنج دارد.",
        client=LLMClient(adapter),
    )
    assert len(fake_session.post_calls) == 2
    # First call attempted strict json_schema
    assert fake_session.post_calls[0]["json"]["response_format"]["type"] == "json_schema"
    # Second fallback call used json_object
    assert fake_session.post_calls[1]["json"]["response_format"] == {"type": "json_object"}
    # Result safely parsed and validated through Python pipeline
    assert len(result["phenotype_candidates"]) == 1
    assert result["phenotype_candidates"][0]["hpo_id"] == "HP:0001250"


@pytest.mark.testing_v3_phenotype
def test_adapter_preserves_strict_json_schema_when_supported_200() -> None:
    """When a provider natively accepts strict json_schema with HTTP 200, no fallback is triggered."""
    extraction_payload = {
        "phenotype_candidates": [
            {
                "hpo_id": "HP:0001250",
                "label": "Seizure",
                "source_phrase_fa": "تشنج دارد",
            }
        ],
        "disease_mentions": [],
        "negated_phenotype_mentions": [],
        "uncertain_phenotype_mentions": [],
        "unmapped_clinical_phrases": [],
    }
    fake_session = _MultiResponseFakeSession(
        [
            (
                200,
                {
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(extraction_payload, ensure_ascii=False),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        ]
    )
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-4o",
        timeout=10,
        session=fake_session,
    )
    result = extract_hpo_candidates(
        "بیمار تشنج دارد.",
        client=LLMClient(adapter),
    )
    assert len(fake_session.post_calls) == 1
    assert fake_session.post_calls[0]["json"]["response_format"]["type"] == "json_schema"
    assert len(result["phenotype_candidates"]) == 1


@pytest.mark.testing_v3_phenotype
def test_invalid_json_rejected_by_local_validation() -> None:
    """Malformed non-JSON output is safely rejected by local _parse_extraction."""
    class _MalformedAdapter:
        def generate(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="This is not valid JSON.",
                model="test-model",
                finish_reason="stop",
            )

    with pytest.raises(PhenotypeExtractionError, match="not valid JSON"):
        extract_hpo_candidates(
            "بیمار تشنج دارد.",
            client=LLMClient(_MalformedAdapter()),
        )


@pytest.mark.testing_v3_phenotype
def test_invalid_hpo_id_rejected_by_local_validation() -> None:
    """Invalid HPO ID format in model output is rejected by local validation."""
    payload = {
        "phenotype_candidates": [
            {
                "hpo_id": "INVALID:123",
                "label": "Seizure",
                "source_phrase_fa": "تشنج دارد",
            }
        ],
        "disease_mentions": [],
        "negated_phenotype_mentions": [],
        "uncertain_phenotype_mentions": [],
        "unmapped_clinical_phrases": [],
    }
    with pytest.raises(PhenotypeExtractionError, match="invalid HPO ID"):
        extract_hpo_candidates(
            "بیمار تشنج دارد.",
            client=LLMClient(_Adapter(payload)),
        )
