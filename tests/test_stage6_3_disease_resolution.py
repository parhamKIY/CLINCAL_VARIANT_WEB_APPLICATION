"""Stage 6.3 deterministic disease resolution and routing safety tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

import backend.pipeline as pipeline
from backend.database import load_pipeline_state, save_analysis, save_pipeline_state
from backend.error_handling import PipelineResultError
from backend.pipeline import validate_analysis_context
from backend.report import build_evidence_object
from backend.vcf_processing import VCFProcessingError
from test_pipeline import TestEvidenceObject as EvidenceFactory


_HPOA_HEADER = (
    "database_id\tdisease_name\tqualifier\thpo_id\treference\tevidence\t"
    "onset\tfrequency\tsex\tmodifier\taspect\tbiocuration"
)


def _write_hpoa(path: Path, rows: list[tuple[str, str]]) -> Path:
    lines = [
        '#description: "Stage 6.3 synthetic disease annotations"',
        "#version: 2026-06-23",
        _HPOA_HEADER,
    ]
    lines.extend(
        (
            f"{identifier}\t{name}\t\tHP:0001250\tPMID:1\tPCS\t\t\t\t\t"
            "P\tHPO:test"
        )
        for identifier, name in rows
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _disease_entity(
    text: str,
    *,
    assertion: str = "PRESENT",
) -> dict[str, object]:
    from backend.clinical_entities import normalize_clinical_entity_text

    return {
        "original_text": text,
        "normalized_text": normalize_clinical_entity_text(text),
        "entity_type": "DISEASE",
        "assertion": assertion,
    }


def _phenotype_entity(text: str = "آتاکسی") -> dict[str, object]:
    from backend.clinical_entities import normalize_clinical_entity_text

    return {
        "original_text": text,
        "normalized_text": normalize_clinical_entity_text(text),
        "entity_type": "PHENOTYPE",
        "assertion": "PRESENT",
    }


def test_explicit_disease_resolves_only_by_unique_normalized_exact_match(
    tmp_path: Path,
) -> None:
    from backend.disease_resolution import HpoDiseaseAnnotationResolver

    annotations = _write_hpoa(
        tmp_path / "phenotype.hpoa",
        [("ORPHA:999001", "سندروم والنبرگ")],
    )
    resolver = HpoDiseaseAnnotationResolver(annotations)

    result = resolver.resolve(_disease_entity("  سندروم\u200cوالنبرگ،  "))

    assert result == {
        "original_text": "سندروم\u200cوالنبرگ،",
        "normalized_name": "سندروم والنبرگ",
        "identifier": "ORPHA:999001",
        "preferred_label": "سندروم والنبرگ",
        "resolution_status": "RESOLVED",
        "resolver_source": "HPO disease annotations",
        "resolver_version": "2026-06-23",
        "match_type": "EXACT",
        "assertion": "PRESENT",
        "limitations": [],
    }
    assert resolver.resolve(_disease_entity("سندروم والنبرگ"))[
        "identifier"
    ] == "ORPHA:999001"


def test_unknown_and_ambiguous_disease_terms_never_receive_an_identifier(
    tmp_path: Path,
) -> None:
    from backend.disease_resolution import HpoDiseaseAnnotationResolver

    annotations = _write_hpoa(
        tmp_path / "phenotype.hpoa",
        [
            ("OMIM:100001", "Shared syndrome"),
            ("ORPHA:100001", "Shared-syndrome"),
        ],
    )
    resolver = HpoDiseaseAnnotationResolver(annotations)

    unknown = resolver.resolve(_disease_entity("rare syndrome xyz"))
    ambiguous = resolver.resolve(_disease_entity("Shared syndrome"))

    assert unknown["resolution_status"] == "UNRESOLVED"
    assert unknown["identifier"] is None
    assert unknown["match_type"] is None
    assert unknown["limitations"] == ["no_exact_disease_match"]
    assert ambiguous["resolution_status"] == "AMBIGUOUS"
    assert ambiguous["identifier"] is None
    assert ambiguous["limitations"] == ["multiple_exact_disease_matches"]


def test_unavailable_resolver_is_explicit_and_bounded(tmp_path: Path) -> None:
    from backend.disease_resolution import HpoDiseaseAnnotationResolver

    result = HpoDiseaseAnnotationResolver(
        tmp_path / "missing.hpoa"
    ).resolve(_disease_entity("سندروم والنبرگ"))

    assert result["resolution_status"] == "UNAVAILABLE"
    assert result["identifier"] is None
    assert result["resolver_source"] == "HPO disease annotations"
    assert result["limitations"] == ["resolver_data_unavailable"]


def test_entity_routes_remain_separate_and_do_not_invent_disease() -> None:
    from backend.disease_resolution import route_clinical_entities

    class ResolverThatMustNotRun:
        def resolve(self, _entity: object) -> object:
            raise AssertionError("Phenotype routing invoked the disease resolver.")

    phenotypes = [_phenotype_entity("آتاکسی"), _phenotype_entity("اختلال بلع")]
    routed = route_clinical_entities(
        phenotypes,
        disease_resolver=ResolverThatMustNotRun(),
    )

    assert routed["phenotype_entities"] == phenotypes
    assert routed["disease_resolutions"] == []
    assert "Wallenberg" not in json.dumps(routed, ensure_ascii=False)


def test_disease_assertion_and_provenance_are_preserved_as_case_context(
    tmp_path: Path,
) -> None:
    from backend.disease_resolution import (
        HpoDiseaseAnnotationResolver,
        route_clinical_entities,
    )

    resolver = HpoDiseaseAnnotationResolver(
        _write_hpoa(
            tmp_path / "phenotype.hpoa",
            [("OMIM:154700", "Marfan syndrome")],
        )
    )
    routed = route_clinical_entities(
        [_disease_entity("Marfan syndrome", assertion="NEGATED")],
        disease_resolver=resolver,
    )

    assert routed["phenotype_entities"] == []
    assert routed["disease_resolutions"][0]["assertion"] == "NEGATED"
    assert routed["disease_resolutions"][0]["identifier"] == "OMIM:154700"
    assert routed["disease_resolutions"][0]["resolver_source"] == (
        "HPO disease annotations"
    )


def test_pipeline_context_keeps_resolution_out_of_hpo_and_variant_evidence(
    tmp_path: Path,
) -> None:
    from backend.disease_resolution import (
        HpoDiseaseAnnotationResolver,
        route_clinical_entities,
    )

    entity = _disease_entity("Marfan syndrome", assertion="SUSPECTED")
    routed = route_clinical_entities(
        [entity],
        disease_resolver=HpoDiseaseAnnotationResolver(
            _write_hpoa(
                tmp_path / "phenotype.hpoa",
                [("OMIM:154700", "Marfan syndrome")],
            )
        ),
    )
    context = validate_analysis_context(
        {
            "input_type": "manual",
            "accepted_hpo_terms": ["HP:0000618"],
            "clinical_entities": [entity],
            "disease_resolutions": routed["disease_resolutions"],
            "phenotype_extraction_model": "entity-model",
            "variant_interpretation_model": "interpretation-model",
            "phenotype_extraction_provenance": None,
        }
    )
    evidence = build_evidence_object(
        deepcopy(EvidenceFactory._pipeline_candidate())
    )

    assert context["accepted_hpo_terms"] == ["HP:0000618"]
    assert context["disease_resolutions"][0]["identifier"] == "OMIM:154700"
    assert "disease_resolutions" not in json.dumps(evidence)
    assert evidence["pathogenicity"]["automated_acmg_classification"] is None


def test_pipeline_context_rejects_unlinked_disease_resolution(
    tmp_path: Path,
) -> None:
    from backend.disease_resolution import (
        HpoDiseaseAnnotationResolver,
        route_clinical_entities,
    )

    routed = route_clinical_entities(
        [_disease_entity("Marfan syndrome")],
        disease_resolver=HpoDiseaseAnnotationResolver(
            _write_hpoa(
                tmp_path / "phenotype.hpoa",
                [("OMIM:154700", "Marfan syndrome")],
            )
        ),
    )

    with pytest.raises(PipelineResultError, match="disease_resolutions"):
        validate_analysis_context(
            {
                "input_type": "manual",
                "accepted_hpo_terms": [],
                "clinical_entities": [_disease_entity("Unknown syndrome")],
                "disease_resolutions": routed["disease_resolutions"],
                "phenotype_extraction_model": None,
                "variant_interpretation_model": None,
                "phenotype_extraction_provenance": None,
            }
        )


def test_run_analysis_routes_disease_before_variant_enrichment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.disease_resolution import HpoDiseaseAnnotationResolver

    resolver = HpoDiseaseAnnotationResolver(
        _write_hpoa(
            tmp_path / "phenotype.hpoa",
            [("OMIM:154700", "Marfan syndrome")],
        )
    )

    def stop_after_context(*_args: object, **_kwargs: object) -> None:
        raise VCFProcessingError("bounded test stop")

    monkeypatch.setattr(pipeline, "_process_filtered_variants", stop_after_context)
    result = pipeline.run_analysis(
        vcf_path=None,
        manual_variants=[
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "G",
                "qual": None,
                "filter": None,
            }
        ],
        phenotypes=["HP:0000618"],
        clinical_entities=[_disease_entity("Marfan syndrome")],
        disease_resolver=resolver,
        persist_analysis=False,
    )

    assert result["status"] == "error"
    assert result["analysis_context"]["accepted_hpo_terms"] == ["HP:0000618"]
    assert result["analysis_context"]["disease_resolutions"][0][
        "identifier"
    ] == "OMIM:154700"


def test_invalid_resolver_output_fails_closed_before_variant_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidResolver:
        def resolve(self, _entity: object) -> object:
            return {}

    monkeypatch.setattr(
        pipeline,
        "_process_filtered_variants",
        lambda *_args, **_kwargs: pytest.fail(
            "Variant processing ran after an invalid resolver result."
        ),
    )
    result = pipeline.run_analysis(
        vcf_path=None,
        manual_variants=[
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "G",
                "qual": None,
                "filter": None,
            }
        ],
        phenotypes=[],
        clinical_entities=[_disease_entity("Marfan syndrome")],
        disease_resolver=InvalidResolver(),
        persist_analysis=False,
    )

    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "invalid_disease_resolution"


def test_pipeline_35_snapshot_migrates_without_fabricated_resolution() -> None:
    legacy = deepcopy(pipeline.create_pipeline_result())
    legacy["schema_version"] = "3.5"
    legacy["analysis_context"].pop("disease_resolutions")

    migrated = pipeline.migrate_pipeline_schema35_to36(legacy)

    assert migrated is not None
    assert migrated["schema_version"] == "3.6"
    assert migrated["analysis_context"]["disease_resolutions"] is None


def test_persisted_pipeline_35_snapshot_recovers_without_sqlite_migration(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage6_3.sqlite3"
    record = save_analysis(status="pending", database_path=database_path)
    current = pipeline.create_pipeline_result()
    current["analysis_id"] = record["analysis_id"]
    save_pipeline_state(current, database_path=database_path)

    legacy = deepcopy(current)
    legacy["schema_version"] = "3.5"
    legacy["analysis_context"].pop("disease_resolutions")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE pipeline_states SET pipeline_schema_version = ?, "
            "pipeline_json = ? WHERE analysis_id = ?",
            (
                "3.5",
                json.dumps(legacy, separators=(",", ":"), sort_keys=True),
                record["analysis_id"],
            ),
        )

    restored = load_pipeline_state(
        record["analysis_id"],
        database_path=database_path,
    )

    assert restored["schema_version"] == "3.6"
    assert restored["analysis_context"]["disease_resolutions"] is None
