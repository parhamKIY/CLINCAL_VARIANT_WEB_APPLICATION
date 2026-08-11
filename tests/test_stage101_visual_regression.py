"""Stage 101 visual-regression acceptance gate."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from backend.report_docx import render_report_data_docx
from tools.stage101_visual_regression import (
    BASELINE_PATH,
    EXPECTED_HEADINGS,
    VisualRegressionError,
    analyze_rendered_pages,
    assert_structural_contract,
    baseline_projection,
    build_scenario_report,
    compare_image_hashes,
    inspect_docx,
    load_scenario_registry,
    render_docx_with_local_office,
    scenario_page_range,
)


pytestmark = pytest.mark.stage101_visual_regression


def _names() -> list[str]:
    return [
        scenario["name"]
        for scenario in load_scenario_registry()["scenarios"]
    ]


def test_scenario_registry_covers_all_required_layout_risks() -> None:
    assert _names() == [
        "fully_populated",
        "sparse_evidence",
        "long_interpretation",
        "many_references",
        "no_phenotype_match",
        "complex_indel",
    ]


@pytest.mark.parametrize("name", _names())
def test_scenario_matches_committed_structural_snapshot(name: str) -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    data = render_report_data_docx(build_scenario_report(name))
    snapshot = inspect_docx(data)

    assert_structural_contract(snapshot)
    assert baseline_projection(snapshot) == baseline["scenarios"][name]
    assert snapshot["explicit_page_breaks"] + 1 <= scenario_page_range(name)[1]


def test_scenario_semantics_exercise_requested_edge_cases() -> None:
    sparse = build_scenario_report("sparse_evidence")
    long_report = build_scenario_report("long_interpretation")
    references = build_scenario_report("many_references")
    phenotype = build_scenario_report("no_phenotype_match")
    indel = build_scenario_report("complex_indel")

    assert not any(sparse["main_findings"].values())
    assert len(long_report["interpretation"]["current_reviewer_interpretation"]) > 8_000
    assert len(references["literature_references"]) == 20
    assert phenotype["phenotype_summary"]["concordance"] == (
        "no_supported_association"
    )
    assert len(indel["variant_identity"]["reference"]) > 100


def test_structural_contract_rejects_heading_and_overflow_guard_drift() -> None:
    snapshot = inspect_docx(
        render_report_data_docx(build_scenario_report("fully_populated"))
    )
    damaged = dict(snapshot)
    damaged["headings"] = EXPECTED_HEADINGS[:-1]
    with pytest.raises(VisualRegressionError, match="heading order"):
        assert_structural_contract(damaged)

    damaged = dict(snapshot)
    damaged["exact_height_rows"] = 1
    with pytest.raises(VisualRegressionError, match="clip"):
        assert_structural_contract(damaged)


def _page(path: Path, *, blank: bool = False, touch_edge: bool = False) -> None:
    image = Image.new("RGB", (800, 1000), "white")
    if not blank:
        draw = ImageDraw.Draw(image)
        left = 0 if touch_edge else 100
        draw.rectangle((left, 120, 700, 880), outline="black", width=5)
        for offset in range(180, 820, 60):
            draw.line((130, offset, 670, offset), fill="black", width=3)
    image.save(path)


def test_raster_analyzer_checks_count_blank_pages_edges_and_hash_tolerance(
    tmp_path: Path,
) -> None:
    pages = [tmp_path / f"page-{index}.png" for index in range(1, 4)]
    for page in pages:
        _page(page)
    result = analyze_rendered_pages(pages, expected_page_range=(3, 5))
    hashes = result["average_hashes"]
    compare_image_hashes(hashes, hashes, tolerance=0)

    _page(pages[1], blank=True)
    with pytest.raises(VisualRegressionError, match="blank page"):
        analyze_rendered_pages(pages, expected_page_range=(3, 5))

    _page(pages[1], touch_edge=True)
    with pytest.raises(VisualRegressionError, match="page edge"):
        analyze_rendered_pages(pages, expected_page_range=(3, 5))


def test_image_hash_comparison_uses_bounded_tolerance() -> None:
    compare_image_hashes(["0000000000000000"], ["0000000000000001"], tolerance=1)
    with pytest.raises(VisualRegressionError, match="image tolerance"):
        compare_image_hashes(
            ["0000000000000000"],
            ["00000000000000ff"],
            tolerance=6,
        )


@pytest.mark.skipif(
    shutil.which("soffice") is None or shutil.which("pdftoppm") is None,
    reason="LibreOffice/Poppler raster renderer is unavailable.",
)
def test_optional_real_docx_raster_matrix(tmp_path: Path) -> None:
    for name in _names():
        data = render_report_data_docx(build_scenario_report(name))
        pages = render_docx_with_local_office(data, tmp_path / name)
        analyze_rendered_pages(
            pages,
            expected_page_range=scenario_page_range(name),
        )
