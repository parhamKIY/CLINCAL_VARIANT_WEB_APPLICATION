"""Stage 2A selected-input result and recovery contract tests."""

from __future__ import annotations

from backend.database import (
    connect_database,
    load_pipeline_state,
    save_analysis,
    save_pipeline_state,
)
from backend.input_preprocessing import (
    build_accepted_input_result,
    build_unresolved_input_result,
)
from backend.pipeline import (
    PIPELINE_SCHEMA_VERSION,
    attach_input_preprocessing_results,
    migrate_pipeline_schema32_to33,
    run_variant_processing,
)


def _variant(chrom: str, pos: int, ref: str, alt: str) -> dict[str, object]:
    return {
        "chrom": chrom,
        "pos": pos,
        "ref": ref,
        "alt": alt,
        "qual": 1.0,
        "filter": "QDfilter;SORfilter",
    }


def _base_result(*, include_third: bool = False) -> dict[str, object]:
    variants = [
        _variant("1", 100, "A", "G"),
        _variant("X", 200, "C", "T"),
    ]
    if include_third:
        variants.append(_variant("2", 300, "G", "A"))
    return run_variant_processing(
        vcf_path=None,
        manual_variants=variants,
        phenotypes=[],
    )


def _three_selected_results(
    result: dict[str, object],
) -> list[dict[str, object]]:
    integrity = result["variant_integrity_records"]
    return [
        build_accepted_input_result(
            source_index=0,
            source_type="excel",
            source_provenance={
                "source_worksheet": "Noticable",
                "source_row": 2,
                "source_chrom": "1",
                "source_start": 100,
                "source_end": 100,
                "source_ref": "A",
                "source_alt": "G",
                "source_filter": "QDfilter",
                "source_qual": 1.0,
                "source_depth": 2,
                "source_ad": 1,
                "source_gq": 1,
                "source_representation": "annovar_like",
            },
            canonical_variant=result["variants"][0],
            canonical_variant_index=0,
            canonical_variant_identity=integrity[0]["parser_allele_identity"],
        ),
        build_unresolved_input_result(
            source_index=1,
            source_type="excel",
            source_provenance={
                "source_worksheet": "Noticable",
                "source_row": 3,
                "source_chrom": "13",
                "source_start": 300,
                "source_end": 303,
                "source_ref": "GTGC",
                "source_alt": "0",
                "source_filter": "QDfilter",
                "source_qual": 1.0,
                "source_depth": 2,
                "source_ad": 1,
                "source_gq": 1,
                "source_representation": "annovar_zero_alt_deletion",
            },
            failure_reason="reference_identity_unresolved",
        ),
        build_accepted_input_result(
            source_index=2,
            source_type="excel",
            source_provenance={
                "source_worksheet": "Noticable",
                "source_row": 4,
                "source_chrom": "X",
                "source_start": 200,
                "source_end": 200,
                "source_ref": "C",
                "source_alt": "T",
                "source_filter": "QDfilter;SORfilter",
                "source_qual": 1.0,
                "source_depth": 2,
                "source_ad": 1,
                "source_gq": 1,
                "source_representation": "annovar_like",
            },
            canonical_variant=result["variants"][1],
            canonical_variant_index=1,
            canonical_variant_identity=integrity[1]["parser_allele_identity"],
        ),
    ]


def test_selected_inputs_are_accounted_for_separately_from_variants() -> None:
    result = _base_result()
    attached = attach_input_preprocessing_results(
        result,
        _three_selected_results(result),
    )

    assert attached["schema_version"] == PIPELINE_SCHEMA_VERSION
    assert len(attached["input_preprocessing_results"]) == 3
    assert attached["variant_count"] == 2
    assert len(attached["variants"]) == 2
    unresolved = attached["input_preprocessing_results"][1]
    assert unresolved["status"] == "IDENTITY_UNRESOLVED"
    assert unresolved["canonical_variant"] is None
    assert unresolved["canonical_variant_index"] is None
    assert attached["annotations"] == []
    assert attached["evidence_objects"] == []
    assert attached["input_preprocessing_results"][2][
        "canonical_variant_identity"
    ] == attached["variant_integrity_records"][1]["parser_allele_identity"]


def test_all_accepted_selected_inputs_preserve_canonical_order() -> None:
    result = _base_result(include_third=True)

    assert len(result["input_preprocessing_results"]) == 3
    assert result["variant_count"] == 3
    assert [
        item["canonical_variant_index"]
        for item in result["input_preprocessing_results"]
    ] == [0, 1, 2]
    assert all(
        item["status"] == "ACCEPTED_DIRECT"
        for item in result["input_preprocessing_results"]
    )


def test_quality_metadata_is_retained_without_eligibility_semantics() -> None:
    base = _base_result()
    result = attach_input_preprocessing_results(
        base,
        _three_selected_results(base),
    )
    source = result["input_preprocessing_results"][0]["source_provenance"]

    assert source["source_filter"] == "QDfilter"
    assert source["source_qual"] == 1.0
    assert source["source_depth"] == 2
    assert source["source_ad"] == 1
    assert source["source_gq"] == 1
    assert result["input_preprocessing_results"][0]["status"] == "ACCEPTED_DIRECT"


def test_schema32_snapshot_migrates_without_losing_selected_inputs() -> None:
    legacy = _base_result()
    legacy.pop("input_preprocessing_results")
    legacy["schema_version"] = "3.2"
    migrated = migrate_pipeline_schema32_to33(legacy)

    assert migrated is not None
    assert migrated["schema_version"] == PIPELINE_SCHEMA_VERSION
    assert len(migrated["input_preprocessing_results"]) == 2
    assert all(
        item["status"] == "ACCEPTED_DIRECT"
        for item in migrated["input_preprocessing_results"]
    )


def test_persistence_and_recovery_preserve_unresolved_input(
    tmp_path,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    result = _base_result()
    attached = attach_input_preprocessing_results(
        result,
        _three_selected_results(result),
    )
    record = save_analysis(status="partial", database_path=database_path)
    attached["analysis_id"] = record["analysis_id"]
    save_pipeline_state(attached, database_path=database_path)

    restored = load_pipeline_state(
        record["analysis_id"], database_path=database_path
    )
    assert restored["input_preprocessing_results"] == attached[
        "input_preprocessing_results"
    ]
    assert len(restored["evidence_objects"]) == 0

    connection = connect_database(database_path)
    try:
        row = connection.execute(
            "SELECT pipeline_schema_version FROM pipeline_states WHERE analysis_id = ?",
            (record["analysis_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert row["pipeline_schema_version"] == PIPELINE_SCHEMA_VERSION
