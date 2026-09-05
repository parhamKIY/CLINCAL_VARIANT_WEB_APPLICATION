from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from config import settings
from frontend import ui as frontend_ui


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _mixed_extraction() -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "task": "extract_hpo_candidates",
        "prompt_version": "phenotype-extraction-v3.0",
        "model": settings.PHENOTYPE_EXTRACTION_MODEL,
        "clinical_entities": [
            {
                "original_text": "تشنج",
                "normalized_text": "تشنج",
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
                "hpo_id": "HP:0001250",
                "label": "Seizure",
                "source_phrase_fa": "تشنج",
            }
        ],
        "disease_mentions": [{"source_phrase_fa": "سندروم والنبرگ"}],
        "negated_phenotype_mentions": [],
        "uncertain_phenotype_mentions": [],
        "unmapped_clinical_phrases": [],
    }


def test_review_explains_entity_roles_and_collapses_manual_hpo() -> None:
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)

    assert not app.exception
    assert any(
        caption.value
        == (
            "Phenotypes are used for phenotype-based analysis. Disease "
            "mentions are retained as clinical context and are not used as "
            "direct variant evidence."
        )
        for caption in app.caption
    )
    assert any(
        item.label == "Add phenotype manually" for item in app.status
    )
    assert not any(
        item.value == "**Selected phenotypes**" for item in app.markdown
    )


def test_extracted_findings_and_disease_context_have_distinct_review_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        frontend_ui,
        "extract_hpo_candidates",
        lambda *_args, **_kwargs: _mixed_extraction(),
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
    ).set_value("کودک تشنج دارد و سابقه سندروم والنبرگ دارد").run(
        timeout=10
    )
    next(
        button
        for button in app.button
        if button.label == "Extract clinical entities"
    ).click().run(timeout=10)

    assert not app.exception
    assert any("Observed findings" in item.value for item in app.markdown)
    assert any(
        "Disease/context mentions" in item.value for item in app.markdown
    )
    assert any(
        caption.value
        == "Context only — disease mentions do not directly become variant evidence."
        for caption in app.caption
    )
    assert any(
        warning.value == "Clinical entities are awaiting reviewer confirmation."
        for warning in app.warning
    )

    next(
        button
        for button in app.button
        if button.label == "Accept clinical entities"
    ).click().run(timeout=10)

    assert not app.exception
    assert any(
        success.value == "Clinical entity review accepted."
        for success in app.success
    )


def test_manual_hpo_addition_remains_available_and_supplemental(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        frontend_ui,
        "search_hpo_terms",
        lambda *_args, **_kwargs: [
            {"id": "HP:0001250", "name": "Seizure"}
        ],
    )
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    next(
        field for field in app.text_input if field.label == "Search HPO terms"
    ).set_value("seizure").run(timeout=10)
    next(
        button for button in app.button if button.label == "Search"
    ).click().run(timeout=10)
    next(
        choice
        for choice in app.selectbox
        if choice.label == "Search results"
    ).select_index(0).run(timeout=10)
    next(
        button for button in app.button if button.label == "Add phenotype"
    ).click().run(timeout=10)

    assert not app.exception
    assert app.session_state["selected_hpo_terms"] == [
        {"id": "HP:0001250", "name": "Seizure"}
    ]
    assert any(
        item.value == "**Accepted HPO terms**" for item in app.markdown
    )
    assert any(
        "HP:0001250" in item.value and "Seizure" in item.value
        for item in app.markdown
    )
