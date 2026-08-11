"""Validate the frozen Stage 79 acceptance registry and synthetic asset."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.privacy import validate_llm_payload


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "acceptance_failures_v1.md"
FIXTURE = ROOT / "tests" / "fixtures" / "stage79_synthetic_report_fixture.json"

EXPECTED_DEFECT_IDS = {f"AF-{number:02d}" for number in range(1, 8)}
EXPECTED_SECTION_ORDER = [
    "ngs_result_report",
    "clinical_features",
    "conclusive_results",
    "brief_interpretation",
    "main_findings_in_detail",
    "variant_interpretation",
    "variant_classification",
    "method",
    "comments_and_scope",
    "references",
    "data_sources",
]


@pytest.fixture
def synthetic_report_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.stage79_acceptance
def test_acceptance_registry_freezes_all_stage79_defects() -> None:
    registry = REGISTRY.read_text(encoding="utf-8")

    assert set(re.findall(r"AF-\d{2}", registry)) == EXPECTED_DEFECT_IDS
    assert "authoritative visual and structural reference" in registry
    assert "local confidential design reference" in registry
    assert "No current application screenshot is committed" in registry


@pytest.mark.stage79_acceptance
def test_synthetic_fixture_preserves_report_structure_and_identity(
    synthetic_report_fixture: dict[str, object],
) -> None:
    fixture = synthetic_report_fixture

    assert fixture["fixture_role"] == "stage79_design_and_acceptance_asset_only"
    assert fixture["synthetic_non_phi"] is True
    assert fixture["production_schema"] is False
    assert fixture["expected_section_order"] == EXPECTED_SECTION_ORDER

    report_identity = fixture["report_identity"]
    variant_identity = fixture["variant_identity"]
    assert isinstance(report_identity, dict)
    assert report_identity["input_index"] == 1
    assert isinstance(variant_identity, dict)
    assert {
        "assembly",
        "chromosome",
        "position",
        "reference",
        "alternate",
        "gene",
        "transcript",
        "coding_hgvs",
        "protein_hgvs",
        "zygosity",
    }.issubset(variant_identity)


@pytest.mark.stage79_acceptance
def test_synthetic_fixture_is_non_phi_and_separates_source_categories(
    synthetic_report_fixture: dict[str, object],
) -> None:
    fixture = synthetic_report_fixture
    validate_llm_payload(fixture)

    serialized = json.dumps(fixture, sort_keys=True)
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert "raw VCF" not in serialized
    assert "national_id" not in serialized.casefold()
    assert "patient_name" not in serialized.casefold()
    assert fixture["literature_references"]
    assert fixture["data_sources"]
    assert "reference_id" in fixture["literature_references"][0]
    assert "source" not in fixture["literature_references"][0]
    assert "source" in fixture["data_sources"][0]
    assert "reference_id" not in fixture["data_sources"][0]


@pytest.mark.stage79_acceptance
def test_synthetic_fixture_captures_valid_non_concordance_and_missingness(
    synthetic_report_fixture: dict[str, object],
) -> None:
    fixture = synthetic_report_fixture
    clinical_features = fixture["clinical_features"]
    interpretation = fixture["variant_interpretation"]
    warnings = fixture["warnings"]

    assert clinical_features["phenotype_concordance"] == (
        "no_supported_association"
    )
    assert interpretation["status"] == "available"
    assert "No supported phenotype association" in (
        interpretation["phenotype_statement"]
    )
    assert {warning["severity"] for warning in warnings} == {
        "INFO",
        "PARTIAL",
    }
