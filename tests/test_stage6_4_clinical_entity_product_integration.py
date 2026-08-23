"""Stage 6.4 product integration and persistence acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import frontend.ui as frontend_ui
from backend.database import load_pipeline_state, save_analysis, save_pipeline_state
from backend.clinical_entities import ClinicalEntityError
from backend.error_handling import safe_ui_error_message
from backend.llm import LLMClient, LLMRequest, LLMResponse
from backend.pipeline import create_pipeline_result, validate_analysis_context
from backend.phenotype_llm import extract_hpo_candidates
from backend.report import build_clinical_interpretation_prompt, build_evidence_object
from config import settings
from frontend.evidence_review import build_clinical_context_view


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Adapter:
    def __init__(self, entities: list[dict[str, object]]) -> None:
        self.entities = entities

    def generate(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(
            content=json.dumps(
                {
                    "clinical_entities": self.entities,
                    "unmapped_clinical_phrases": [],
                },
                ensure_ascii=False,
            ),
            model="stage6-4-model",
            finish_reason="stop",
        )


def _entity_payload(
    original_text: str,
    entity_type: str,
    assertion: str,
    *,
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


@pytest.mark.testing_v3_phenotype
@pytest.mark.parametrize(
    ("clinical_text", "entities", "expected_types", "expected_assertions"),
    [
        (
            "بیمار نابینایی دارد",
            [
                _entity_payload(
                    "نابینایی",
                    "PHENOTYPE",
                    "PRESENT",
                    hpo_id="HP:0000618",
                    label="Blindness",
                )
            ],
            ["PHENOTYPE"],
            ["PRESENT"],
        ),
        (
            "بیمار سابقه سندروم والنبرگ دارد",
            [_entity_payload("سندروم والنبرگ", "DISEASE", "HISTORICAL")],
            ["DISEASE"],
            ["HISTORICAL"],
        ),
        (
            "سرگیجه و اختلال بلع دارد",
            [
                _entity_payload("سرگیجه", "PHENOTYPE", "PRESENT"),
                _entity_payload("اختلال بلع", "PHENOTYPE", "PRESENT"),
            ],
            ["PHENOTYPE", "PHENOTYPE"],
            ["PRESENT", "PRESENT"],
        ),
        (
            "بیمار نابینایی دارد و سابقه بیماری X دارد",
            [
                _entity_payload(
                    "نابینایی",
                    "PHENOTYPE",
                    "PRESENT",
                    hpo_id="HP:0000618",
                    label="Blindness",
                ),
                _entity_payload("بیماری X", "DISEASE", "HISTORICAL"),
            ],
            ["PHENOTYPE", "DISEASE"],
            ["PRESENT", "HISTORICAL"],
        ),
    ],
)
def test_acceptance_scenarios_preserve_explicit_entity_semantics(
    clinical_text: str,
    entities: list[dict[str, object]],
    expected_types: list[str],
    expected_assertions: list[str],
) -> None:
    result = extract_hpo_candidates(
        clinical_text,
        client=LLMClient(_Adapter(entities)),
    )

    assert [item["entity_type"] for item in result["clinical_entities"]] == (
        expected_types
    )
    assert [item["assertion"] for item in result["clinical_entities"]] == (
        expected_assertions
    )
    assert not any(
        item["entity_type"] == "DISEASE"
        for item in result["clinical_entities"]
        if "سندروم" not in clinical_text and "بیماری" not in clinical_text
    )


def test_review_rows_require_confirmation_and_support_edit_and_reject() -> None:
    extracted = [
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

    rows = frontend_ui.prepare_clinical_entity_review_rows(extracted)
    rows[0]["include"] = False
    rows[1]["original_text"] = "سندروم\u200cوالنبرگ"
    rows[1]["assertion"] = "SUSPECTED"

    assert frontend_ui.accept_clinical_entity_review_rows(rows) == [
        {
            "original_text": "سندروم\u200cوالنبرگ",
            "normalized_text": "سندروم والنبرگ",
            "entity_type": "DISEASE",
            "assertion": "SUSPECTED",
        }
    ]


def test_invalid_entity_edit_maps_to_entity_specific_safe_message() -> None:
    message = safe_ui_error_message(
        ClinicalEntityError("private invalid entity detail"),
        context="clinical_entity_acceptance",
    )

    assert message == (
        "The edited clinical entities could not be accepted. Correct or "
        "remove invalid entries and try again."
    )
    assert "private" not in message


def test_streamlit_separates_entities_and_does_not_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        frontend_ui,
        "extract_hpo_candidates",
        lambda *_args, **_kwargs: {
            "schema_version": "3.0",
            "task": "extract_hpo_candidates",
            "prompt_version": "phenotype-extraction-v3.0",
            "model": settings.PHENOTYPE_EXTRACTION_MODEL,
            "clinical_entities": [
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
            ],
            "phenotype_candidates": [
                {
                    "hpo_id": "HP:0000618",
                    "label": "Blindness",
                    "source_phrase_fa": "نابینایی",
                }
            ],
            "disease_mentions": [{"source_phrase_fa": "سندروم والنبرگ"}],
            "negated_phenotype_mentions": [],
            "uncertain_phenotype_mentions": [],
            "unmapped_clinical_phrases": [],
        },
    )
    monkeypatch.setattr(
        frontend_ui,
        "validate_hpo_candidates",
        lambda candidates: {
            "validated_candidates": candidates,
            "rejected_candidates": [],
        },
    )
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    next(
        area
        for area in app.text_area
        if area.label == "Persian clinical description"
    ).set_value(
        "بیمار نابینایی دارد و سابقه سندروم والنبرگ دارد"
    ).run(timeout=10)
    next(
        button
        for button in app.button
        if button.label == "Extract clinical entities"
    ).click().run(timeout=10)

    assert not app.exception
    assert app.session_state["clinical_entities"] == []
    assert app.session_state["clinical_entity_review_complete"] is False
    assert any("Observed findings" in item.value for item in app.markdown)
    assert any("Disease/context mentions" in item.value for item in app.markdown)
    accept = next(
        button
        for button in app.button
        if button.label == "Accept clinical entities"
    )
    accept.click().run(timeout=10)

    assert not app.exception
    assert app.session_state["clinical_entity_review_complete"] is True
    assert [
        item["entity_type"] for item in app.session_state["clinical_entities"]
    ] == ["PHENOTYPE", "DISEASE"]


def test_analysis_cannot_start_while_entity_review_is_pending() -> None:
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["clinical_entity_review_drafts"] = [
        {
            "source_index": 0,
            "include": True,
            "original_text": "سندروم والنبرگ",
            "normalized_text": "سندروم والنبرگ",
            "entity_type": "DISEASE",
            "assertion": "HISTORICAL",
        }
    ]
    app.session_state["clinical_entity_review_complete"] = False
    app.run(timeout=10)

    next(
        button for button in app.button if button.label == "Analyze variants"
    ).click().run(timeout=10)

    assert not app.exception
    assert any(
        error.value
        == (
            "Review and accept or reject the extracted clinical entities "
            "before starting analysis."
        )
        for error in app.error
    )
    assert app.session_state["pipeline_result"] is None


def test_reviewer_view_keeps_findings_and_disease_context_separate() -> None:
    result = create_pipeline_result()
    result["analysis_context"] = validate_analysis_context(
        {
            **result["analysis_context"],
            "clinical_entities": [
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
            ],
            "disease_resolutions": [
                {
                    "original_text": "سندروم والنبرگ",
                    "normalized_name": "سندروم والنبرگ",
                    "identifier": None,
                    "preferred_label": None,
                    "resolution_status": "UNRESOLVED",
                    "resolver_source": "HPO disease annotations",
                    "resolver_version": "2026-06-23",
                    "match_type": None,
                    "assertion": "HISTORICAL",
                    "limitations": ["no_exact_disease_match"],
                }
            ],
        }
    )

    view = build_clinical_context_view(result)

    assert view["capture_state"] == "CAPTURED"
    assert view["observed_findings"] == [
        {
            "text": "نابینایی",
            "type": "PHENOTYPE",
            "assertion": "PRESENT",
        }
    ]
    assert view["disease_context"] == [
        {
            "text": "سندروم والنبرگ",
            "type": "DISEASE",
            "assertion": "HISTORICAL",
            "resolution_status": "UNRESOLVED",
            "preferred_label": None,
            "identifier": None,
        }
    ]


def test_entities_and_disease_context_survive_pipeline_snapshot_recovery(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage6-4.sqlite3"
    record = save_analysis(status="pending", database_path=database_path)
    result = create_pipeline_result()
    result["analysis_id"] = record["analysis_id"]
    result["analysis_context"] = validate_analysis_context(
        {
            **result["analysis_context"],
            "clinical_entities": [
                {
                    "original_text": "سندروم والنبرگ",
                    "normalized_text": "سندروم والنبرگ",
                    "entity_type": "DISEASE",
                    "assertion": "HISTORICAL",
                }
            ],
            "disease_resolutions": [
                {
                    "original_text": "سندروم والنبرگ",
                    "normalized_name": "سندروم والنبرگ",
                    "identifier": None,
                    "preferred_label": None,
                    "resolution_status": "UNRESOLVED",
                    "resolver_source": "HPO disease annotations",
                    "resolver_version": "2026-06-23",
                    "match_type": None,
                    "assertion": "HISTORICAL",
                    "limitations": ["no_exact_disease_match"],
                }
            ],
        }
    )

    save_pipeline_state(result, database_path=database_path)
    restored = load_pipeline_state(
        record["analysis_id"],
        database_path=database_path,
    )

    assert restored["analysis_context"]["clinical_entities"] == result[
        "analysis_context"
    ]["clinical_entities"]
    assert restored["analysis_context"]["disease_resolutions"] == result[
        "analysis_context"
    ]["disease_resolutions"]


def test_clinical_context_is_not_projected_into_evidence() -> None:
    candidate = {
        "variant": {
            "chrom": "2",
            "pos": 166848215,
            "ref": "C",
            "alt": "T",
            "qual": 99.0,
            "filter": "PASS",
        },
        "assembly": "GRCh38",
        "gene": "SCN1A",
        "gene_id": None,
        "transcript": None,
        "consequence": "missense_variant",
        "impact": "MODERATE",
        "protein_change": None,
        "population_frequency": None,
        "sources": {
            "vep": {"status": "success"},
            "myvariant": {"status": "not_found"},
            "clinvar": {
                "status": "not_found",
                "accession": None,
                "accession_version": None,
                "clinical_significance": None,
                "review_status": None,
                "conditions": [],
            },
            "clingen": {"status": "not_found", "curations": []},
        },
        "references": [],
        "warnings": [],
        "clinical_entities": [{"original_text": "سندروم والنبرگ"}],
        "disease_resolutions": [{"preferred_label": "Wallenberg syndrome"}],
    }

    evidence = build_evidence_object(candidate)
    serialized = json.dumps(evidence, ensure_ascii=False)
    prompt = build_clinical_interpretation_prompt(evidence)["user_prompt"]

    assert "clinical_entities" not in evidence
    assert "disease_resolutions" not in evidence
    assert "سندروم والنبرگ" not in serialized
    assert "Wallenberg syndrome" not in serialized
    assert "سندروم والنبرگ" not in prompt
    assert "Wallenberg syndrome" not in prompt
