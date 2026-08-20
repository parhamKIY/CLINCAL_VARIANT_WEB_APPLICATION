"""Stage 87 cardinality and allele-identity integrity acceptance gate."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.database import (
    _bounded_stage56_pipeline_migration,
    load_pipeline_state,
    save_pipeline_state,
)
from backend.llm import LLMClient
from backend.pipeline import (
    confirm_reviewed_evidence,
    finalize_reviewed_analysis,
    run_analysis,
)
from backend.variant_integrity import cardinality_counts
from config import settings
from tests.test_pipeline import (
    FakeLLMAdapter,
    TestEvidenceObject as _TestEvidenceObject,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage87_variant_integrity


def _manual_variants() -> list[dict[str, object]]:
    return [
        {
            "chrom": "2",
            "pos": 166_000_000 + index,
            "ref": "C",
            "alt": "T" if index % 2 == 0 else "G",
            "qual": 99.0,
            "filter": "PASS",
        }
        for index in range(7)
    ]


def test_seven_variant_cardinality_and_identity_are_preserved_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_annotate(
        variants: object,
        **_: object,
    ) -> list[dict[str, object]]:
        annotations: list[dict[str, object]] = []
        for normalized in variants:  # type: ignore[union-attr]
            candidate = _TestEvidenceObject._pipeline_candidate()
            candidate["variant"] = dict(normalized)
            candidate["gene"] = "SCN1A"
            candidate["gene_id"] = "HGNC:10585"
            annotations.append(candidate)
        return annotations

    monkeypatch.setattr("backend.pipeline.annotate_variants", fake_annotate)
    monkeypatch.setattr(
        "backend.pipeline.enrich_with_mydisease",
        lambda variants, *_args, **_kwargs: {
            "variants": [deepcopy(item) for item in variants],
            "status": "unsupported",
            "message": "No phenotype terms supplied.",
        },
    )
    monkeypatch.setattr(
        "backend.pipeline.enrich_conditionally",
        lambda variants, *_args, **_kwargs: {
            "variants": [deepcopy(item) for item in variants],
            "triggered_count": 0,
        },
    )
    monkeypatch.setattr(settings, "ENABLE_GNOMAD_DEEP_LOOKUP", False)
    monkeypatch.setattr(settings, "ENABLE_LITERATURE_ENRICHMENT", False)

    database_path = tmp_path / "stage87.sqlite3"
    adapter = FakeLLMAdapter(_variant_interpretation_response())
    analysis = run_analysis(
        vcf_path=None,
        manual_variants=_manual_variants(),
        phenotypes=[],
        llm_client=LLMClient(adapter),
        database_path=database_path,
    )

    expected_indexes = list(range(7))
    assert analysis["variant_count"] == 7
    assert [item["input_index"] for item in analysis["variants"]] == (
        expected_indexes
    )
    assert [
        item["variant"]["input_index"] for item in analysis["annotations"]
    ] == expected_indexes
    assert [
        item["variant"]["input_index"]
        for item in analysis["phenotype_results"]
    ] == expected_indexes
    assert [
        item["variant_index"]
        for item in analysis["variant_interpretation_results"]
    ] == expected_indexes
    assert [
        item["variant_index"] for item in analysis["draft_variant_reports"]
    ] == expected_indexes
    assert [
        item["report_data"]["input_index"]
        for item in analysis["variant_report_records"]
    ] == expected_indexes
    assert cardinality_counts(analysis["variant_integrity_records"]) == {
        "parser": 7,
        "normalized": 7,
        "pipeline": 7,
        "evidence_package": 7,
        "draft_report": 7,
        "review_record": 7,
    }

    identities = [
        item["pipeline_allele_identity"]
        for item in analysis["variant_integrity_records"]
    ]
    assert len(set(identities)) == 7
    assert {item["gene"] for item in analysis["evidence_objects"]} == {
        "SCN1A"
    }

    review_time = max(
        datetime.fromisoformat(
            report["updated_at"].replace("Z", "+00:00")
        )
        for report in analysis["evidence_review_reports"]
    )
    confirmed_at = (review_time + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    finalized_at = (review_time + timedelta(minutes=2)).isoformat().replace(
        "+00:00", "Z"
    )
    confirmed = confirm_reviewed_evidence(
        analysis,
        analysis["evidence_review_reports"],
        timestamp=confirmed_at,
    )
    completed = finalize_reviewed_analysis(
        confirmed,
        timestamp=finalized_at,
    )
    save_pipeline_state(completed, database_path=database_path)
    restored = load_pipeline_state(
        completed["analysis_id"], database_path=database_path
    )

    assert cardinality_counts(restored["variant_integrity_records"]) == {
        "parser": 7,
        "normalized": 7,
        "pipeline": 7,
        "evidence_package": 7,
        "draft_report": 7,
        "review_record": 7,
    }
    assert [
        record["pipeline_allele_identity"]
        for record in restored["variant_integrity_records"]
    ] == identities

    legacy_stage86 = deepcopy(restored)
    legacy_stage86["schema_version"] = "3.0"
    legacy_stage86.pop("variant_integrity_records")
    legacy_stage86.pop("evidence_construction_outcomes")
    for variant in legacy_stage86["variants"]:
        variant.pop("input_index")
    migrated = _bounded_stage56_pipeline_migration(legacy_stage86)
    assert migrated is not None
    assert cardinality_counts(migrated["variant_integrity_records"]) == {
        "parser": 7,
        "normalized": 7,
        "pipeline": 7,
        "evidence_package": 7,
        "draft_report": 7,
        "review_record": 7,
    }


def test_integrity_gate_rejects_silent_cardinality_loss() -> None:
    from backend.variant_integrity import (
        VariantIntegrityError,
        build_variant_integrity_records,
        index_input_variants,
    )

    parsed = index_input_variants(_manual_variants())

    with pytest.raises(VariantIntegrityError, match="counts must be equal"):
        build_variant_integrity_records(
            parsed,
            parsed[:-1],
            assembly="GRCh38",
        )
