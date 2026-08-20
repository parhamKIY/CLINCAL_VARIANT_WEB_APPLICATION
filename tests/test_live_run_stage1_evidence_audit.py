"""Stage 1 deterministic audit for the live EvidenceObject failure shape."""

from __future__ import annotations

import json
import logging
from copy import deepcopy

import pytest

from backend.report import (
    MAX_EVIDENCE_SERIALIZED_BYTES,
    EvidenceObjectError,
    build_evidence_object,
    build_evidence_objects,
    build_evidence_objects_isolated,
)
from tests.test_pipeline import TestEvidenceObject as EvidenceFactory


def _population_fallback() -> dict[str, object]:
    return {
        "triggered": True,
        "triggers": ["readiness_population_deficit"],
        "population_frequency": {
            "status": "available",
            "response_status": "available",
            "provider": "UCSC gnomAD",
            "provider_role": "fallback",
            "fallback_used": True,
            "fallback_for": "gnomad",
            "primary_provider": "gnomAD",
            "primary_failure": "forbidden",
            "fallback_provider": "UCSC gnomAD",
            "fallback_status": "success",
            "source": "ucsc_gnomad",
            "underlying_dataset": "gnomAD",
            "upstream_sources": ["gnomAD"],
            "retrieved_at": "2026-08-17T13:52:30Z",
            "assembly": "GRCh38",
            "query_identifier": "synthetic-exact-allele",
            "variant_id": "synthetic-exact-allele",
            "global_af": 0.0001,
            "populations": [
                {
                    "population": f"synthetic-{index}",
                    "allele": "T",
                    "frequency": 0.0001,
                    "allele_count": 1,
                    "allele_number": 10_000,
                    "homozygote_count": 0,
                    "hemizygote_count": 0,
                }
                for index in range(20)
            ],
            "warnings": [],
            "failure_reason": None,
        },
        "literature": {
            "status": "not_triggered",
            "providers": {
                "litvar": {"status": "not_triggered"},
                "europe_pmc": {"status": "not_triggered"},
                "pubmed": {"status": "not_triggered"},
            },
            "articles": [],
            "warnings": [],
        },
        "myvariant_fallback": {
            "used": False,
            "status": "not_needed",
            "provider": "MyVariant.info",
            "upstream_sources": ["gnomAD"],
            "independent_evidence": False,
        },
        "warnings": [],
    }


def _live_shape_candidate(index: int) -> dict[str, object]:
    candidate = EvidenceFactory._complete_candidate()
    variant = candidate["variant"]
    sources = candidate["sources"]
    assert isinstance(variant, dict)
    assert isinstance(sources, dict)
    variant["pos"] = 166_848_215 + index

    sources["vep"] = {
        "status": "partial",
        "provider": "VariantValidator",
        "provider_role": "fallback",
        "fallback_used": True,
        "fallback_for": "ensembl_vep",
        "primary_provider": "Ensembl VEP",
        "primary_failure": "timeout",
        "fallback_provider": "VariantValidator",
        "fallback_status": "success",
        "source_type": "validation_mapping_fallback",
        "upstream_sources": ["VariantValidator"],
        "normalized_variant": {
            "assembly": "GRCh38",
            "chrom": "2",
            "pos": variant["pos"],
            "ref": "C",
            "alt": "T",
        },
        "validated_gene": "SCN1A",
        "validated_transcript": "NM_001165963.4",
        "validated_transcript_hgvs": "NM_001165963.4:c.3877G>A",
        "validated_protein_hgvs": "NP_001159435.1:p.(Val1293Ile)",
        "selected_record": "NM_001165963.4:c.3877G>A",
        "consequence_available": False,
        "validation_warnings": [],
    }
    sources["genebe"] = {
        "status": "unavailable",
        "provider": "GeneBe",
    }
    sources["cspec"] = {
        "status": "not_found",
        "provider": "ClinGen CSpec Registry",
        "specifications": [],
    }
    sources["erepo"] = {
        "status": "no_match",
        "provider": "ClinGen ERepo",
    }
    if index == 2:
        sources["myvariant"] = {
            "status": "not_found",
            "provider": "MyVariant.info",
        }
        sources["clinvar"] = {
            "status": "not_found",
            "provider": "NCBI ClinVar",
            "accession": None,
            "accession_version": None,
            "clinical_significance": None,
            "review_status": None,
            "conditions": [],
        }

    candidate["phen2gene"] = {
        "availability": "available",
        "status": "SeedGene",
        "provider": "Phen2Gene",
        "gene": "SCN1A",
        "gene_id": "6323",
        "rank": 1,
        "score": 0.95,
        "hpo_terms": ["HP:0001250"],
        "warnings": [],
    }
    candidate["medgen_disease_hpo_context"] = {
        "schema_version": "1.0",
        "provider": "NCBI MedGen",
        "provider_id": "ncbi_medgen",
        "provider_role": "supporting",
        "fallback_used": False,
        "primary_provider": "mydisease",
        "primary_retrieval_state": "usable",
        "status": "success",
        "retrieval_state": "usable",
        "retrieved_at": "2026-08-17T13:52:24Z",
        "query_gene": "SCN1A",
        "query_key": "synthetic-scn1a",
        "attempts": 1,
        "http_status": 200,
        "upstream_sources": ["NCBI MedGen"],
        "records": [],
        "enrichment_decision": {
            "triggered": True,
            "reason_codes": ["disease_context_support"],
            "target_semantic_node": "disease_hpo_context",
            "primary_retrieval_state": "usable",
            "required_fields_missing": [],
            "query_key": "synthetic-scn1a",
        },
    }
    candidate["medgen_phenotype_gene_context"] = []
    candidate["mydisease"] = {
        "status": "available",
        "provider": "MyDisease.info",
        "provider_version": "synthetic-2026-08",
        "retrieved_at": "2026-08-17T13:52:22Z",
        "query_gene": "SCN1A",
        "query_gene_id": "HGNC:10585",
        "disease_count": 5,
        "diseases": [
            {
                "disease_id": f"MONDO:{index + 1:07d}",
                "disease_name": f"Synthetic disease context {index + 1}",
                "synonyms": [],
                "primary_source": "MONDO",
                "cross_references": {"omim": [f"{index + 1:06d}"]},
                "gene_disease_relation": {
                    "association_type": "direct_gene_disease",
                    "requested_gene_id": "HGNC:10585",
                    "matched_gene_id": "HGNC:10585",
                    "upstream_source": "MONDO",
                    "is_direct": True,
                },
                "supporting_hpo_terms": [],
                "matched_patient_hpo_terms": ["HP:0001250"],
                "unmatched_patient_hpo_terms": ["HP:0001263"],
                "phenotype_match_count": 1,
                "phenotype_match_status": "partial_match",
                "upstream_sources": ["HPO", "MONDO"],
                "warnings": [],
            }
            for index in range(5)
        ],
        "inferred_pathway_context": [],
        "upstream_sources": ["HPO", "MONDO"],
        "warnings": [],
        "failure_reason": None,
        "cache_state": "miss",
    }
    candidate.pop("conditional_enrichment", None)
    return candidate


def _calibrated_failing_candidate(index: int = 3) -> dict[str, object]:
    """Create the historical safe live shape that exceeded the final cap."""

    candidate = _live_shape_candidate(index)
    mydisease = candidate["mydisease"]
    assert isinstance(mydisease, dict)
    diseases = mydisease["diseases"]
    assert isinstance(diseases, list)
    for synonym_index in range(120):
        disease = diseases[synonym_index % len(diseases)]
        assert isinstance(disease, dict)
        synonyms = disease["synonyms"]
        assert isinstance(synonyms, list)
        synonyms.append(
            f"Synthetic bounded disease synonym {synonym_index:03d} "
            + ("context " * 40)
        )
    candidate["conditional_enrichment"] = _population_fallback()
    return candidate


def test_live_shape_is_compacted_without_a_failure_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidates = [_live_shape_candidate(index) for index in range(3)]
    candidates.append(_calibrated_failing_candidate())
    with caplog.at_level(logging.WARNING):
        evidence_objects = build_evidence_objects(candidates)

    assert len(evidence_objects) == 4
    assert evidence_objects[3]["phenotype_relationship"]["mydisease"][
        "compaction"
    ]["applied"] is True
    assert not any(
        "event=evidence_construction_failed" in record.message
        for record in caplog.records
    )


def test_one_failing_variant_is_isolated_without_discarding_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _live_shape_candidate(0)
    failing = _live_shape_candidate(1)
    third = _live_shape_candidate(2)
    entered: list[int] = []
    real_builder = build_evidence_object

    def traced_builder(candidate: object) -> dict[str, object]:
        assert isinstance(candidate, dict)
        variant = candidate["variant"]
        assert isinstance(variant, dict)
        entered.append(int(variant["pos"]))
        if variant["pos"] == 166_848_216:
            raise EvidenceObjectError("Synthetic per-variant contract failure.")
        return real_builder(candidate)

    monkeypatch.setattr("backend.report.build_evidence_object", traced_builder)
    batch = build_evidence_objects_isolated([first, failing, third])

    assert entered == [166_848_215, 166_848_216, 166_848_217]
    assert [item["variant"]["pos"] for item in batch["evidence_objects"]] == [
        166_848_215,
        166_848_217,
    ]
    assert [item["status"] for item in batch["outcomes"]] == [
        "success",
        "failed",
        "success",
    ]


def test_live_shape_preliminary_and_final_evidence_remain_bounded() -> None:
    final_candidate = _calibrated_failing_candidate()
    preliminary_candidate = deepcopy(final_candidate)
    preliminary_candidate.pop("conditional_enrichment")

    preliminary = build_evidence_object(preliminary_candidate)
    preliminary_size = len(
        json.dumps(preliminary, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    )

    assert preliminary_size <= MAX_EVIDENCE_SERIALIZED_BYTES
    final = build_evidence_object(final_candidate)
    final_size = len(
        json.dumps(final, ensure_ascii=False, allow_nan=False).encode("utf-8")
    )

    assert final_size <= MAX_EVIDENCE_SERIALIZED_BYTES
    assert final["phenotype_relationship"]["mydisease"]["compaction"][
        "applied"
    ] is True
