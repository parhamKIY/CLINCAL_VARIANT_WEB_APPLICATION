"""Validate the Stage 80 professor-report specification artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPECIFICATION = ROOT / "docs" / "professor_report_template_spec.md"
STYLE_SPECIFICATION = ROOT / "docs" / "report_style_spec.yaml"
PROGRESS = ROOT / "docs" / "stage_80_progress.md"
REFERENCE_SHA256 = (
    "41DFE4F49925CE86E42E881897EC8E7A8662DA71EE693101DAB681333390CACC"
)


@pytest.mark.stage80_specification
def test_specification_is_pdf_authoritative_and_per_variant() -> None:
    specification = SPECIFICATION.read_text(encoding="utf-8")

    assert "docs/TS-Final Report.pdf" in specification
    assert "authoritative visual and structural reference" in specification
    assert "all four rendered pages" in specification
    assert REFERENCE_SHA256 in specification
    assert "One accepted allele produces one independent report" in specification
    assert "Two variants in the same gene remain separate reports" in specification


@pytest.mark.stage80_specification
def test_specification_covers_required_anatomy_and_content_rules() -> None:
    specification = SPECIFICATION.read_text(encoding="utf-8")
    required_phrases = {
        "Clinical Features",
        "Conclusive Result(s)",
        "Brief Interpretation(s)",
        "Main Finding(s) in Detail",
        "Variant interpretation",
        "Variant(s) classification",
        "Method",
        "Comments / scope",
        "References",
        "Data Sources",
        "No supported association",
        "No exact record",
        "raw API JSON endpoint",
    }

    assert all(phrase in specification for phrase in required_phrases)
    assert "Patient identifiers" in specification
    assert "prenatal/PGD" in specification
    assert "No Stage 81 schema" in specification


@pytest.mark.stage80_specification
def test_style_tokens_preserve_observed_geometry_and_truth_labels() -> None:
    style = STYLE_SPECIFICATION.read_text(encoding="utf-8")
    required_tokens = {
        'authoritative_reference: "docs/TS-Final Report.pdf"',
        f'authoritative_reference_sha256: "{REFERENCE_SHA256}"',
        "width_pt: 612",
        "height_pt: 792",
        'value: "Times New Roman"',
        'value: "#C00000"',
        'value: "#2F5496"',
        'value: "#FFC000"',
        "width_pt: 477.6",
        "first_band_column_percent:",
        "second_band_column_percent:",
        'basis: "observed"',
        "target_approximation",
    }

    assert all(token in style for token in required_tokens)


@pytest.mark.stage80_specification
def test_progress_file_records_resumable_stage80_state() -> None:
    progress = PROGRESS.read_text(encoding="utf-8")

    assert all(f"80.{number}" in progress for number in range(1, 10))
    assert "**Exact next step:**" in progress
    assert "ReportData V4" in progress
