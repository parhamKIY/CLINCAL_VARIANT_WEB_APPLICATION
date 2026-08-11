"""Stage 92 Reference Model V2 acceptance checks."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.llm import LLMClient
from backend.provider_resilience import build_capability_result
from backend.reference_model import (
    LITERATURE_IDENTIFIER_TYPES,
    REFERENCE_MODEL_SCHEMA_VERSION,
    ReferenceModelError,
    build_reference_model_v2,
    validate_reference_model_v2,
)
from backend.references import canonicalize_reference
from backend.variant_interpretation import interpret_variant
from backend.variant_report import build_draft_variant_report
from test_pipeline import (
    FakeLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage92_reference_model


def _evidence() -> dict[str, object]:
    return deepcopy(EvidenceFactory._complete_evidence_object())


def test_reference_model_has_two_disjoint_categories() -> None:
    model = build_reference_model_v2(_evidence())

    assert model["schema_version"] == REFERENCE_MODEL_SCHEMA_VERSION
    assert [
        item["reference_id"] for item in model["literature_references"]
    ] == [
        f"R{index}"
        for index in range(1, len(model["literature_references"]) + 1)
    ]
    assert all(
        item["identifier_type"] in LITERATURE_IDENTIFIER_TYPES
        for item in model["literature_references"]
    )
    assert all("reference_id" not in item for item in model["data_sources"])


def test_required_database_and_tool_families_are_data_sources() -> None:
    sources = {
        item["source"] for item in build_reference_model_v2(_evidence())["data_sources"]
    }

    assert {
        "ClinVar",
        "Ensembl",
        "GeneBe",
        "MyVariant.info",
        "ClinGen / GenCC",
        "CSpec",
        "Phen2Gene/local HPO-Gene",
        "MyDisease",
        "Population source (gnomAD)",
    } <= sources


def test_database_reference_is_rejected_from_literature_bibliography() -> None:
    invalid = {
        "schema_version": REFERENCE_MODEL_SCHEMA_VERSION,
        "literature_references": [
            canonicalize_reference(
                source="NCBI ClinVar",
                identifier="VCV000012345.1",
            )
        ],
        "data_sources": [],
    }

    with pytest.raises(ReferenceModelError, match="Database/tool source"):
        validate_reference_model_v2(invalid)


def test_reference_model_order_is_stable_after_json_persistence() -> None:
    evidence = _evidence()

    before = build_reference_model_v2(evidence)
    after = build_reference_model_v2(
        json.loads(json.dumps(evidence, sort_keys=True))
    )

    assert after == before


def test_fallback_provenance_remains_in_unnumbered_data_sources() -> None:
    evidence = _evidence()
    capabilities = evidence["capability_results"]
    assert isinstance(capabilities, dict)
    capabilities["population_frequency"] = build_capability_result(
        capability="population_frequency",
        status="success",
        provider="ensembl_variation",
        provider_role="fallback",
        fallback_for="gnomad",
        primary_failure="forbidden",
        method="exact_mapping_population_lookup",
    )

    model = build_reference_model_v2(evidence)
    source = next(
        item
        for item in model["data_sources"]
        if item["capability"] == "population_frequency"
    )

    assert source["provider_role"] == "fallback"
    assert source["fallback_used"] is True
    assert source["primary_failure"] == "forbidden"
    assert "reference_id" not in source


def test_interpretation_catalog_contains_literature_only() -> None:
    evidence = _evidence()
    adapter = FakeLLMAdapter(_variant_interpretation_response())

    interpret_variant(evidence, client=LLMClient(adapter))

    prompt = adapter.requests[0].messages[1].content
    catalog = prompt.split("BEGIN_ALLOWED_REFERENCE_CATALOG\n", 1)[1].split(
        "\nEND_ALLOWED_REFERENCE_CATALOG", 1
    )[0]
    parsed = json.loads(catalog)
    assert parsed
    assert all(
        item["identifier_type"] in LITERATURE_IDENTIFIER_TYPES
        for item in parsed
    )
    assert all(item["source"] != "NCBI ClinVar" for item in parsed)


def test_draft_report_exposes_separate_persistable_collections() -> None:
    evidence = _evidence()
    interpretation = interpret_variant(
        evidence,
        client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
    )

    report = build_draft_variant_report(
        evidence,
        interpretation,
        variant_index=0,
    )
    content = report["reviewed_report"]

    assert "references" not in content
    assert content["literature_references"]
    assert content["data_sources"]
