"""Deterministic compaction acceptance tests for literature, predictors, and MedGen."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy

import pytest

from backend.report import (
    MAX_EVIDENCE_LITERATURE_SECTION_BYTES,
    MAX_EVIDENCE_MEDGEN_GD_SECTION_BYTES,
    MAX_EVIDENCE_MEDGEN_SECTION_BYTES,
    MAX_EVIDENCE_MYDISEASE_SECTION_BYTES,
    MAX_EVIDENCE_PREDICTORS_SECTION_BYTES,
    MAX_EVIDENCE_SERIALIZED_BYTES,
    build_evidence_object,
)
from tests.test_live_run_stage1_evidence_audit import (
    _live_shape_candidate,
    _population_fallback,
)


def _serialized_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    )


def test_oversized_literature_context_is_compacted_deterministically() -> None:
    candidate = _live_shape_candidate(0)
    candidate["conditional_enrichment"] = _population_fallback()
    candidate["conditional_enrichment"]["literature"] = {
        "status": "available",
        "providers": {
            "litvar": {
                "status": "available",
                "result_count": 50,
                "article_identifiers": [f"PMC{i}" for i in range(50)],
            },
            "europe_pmc": {
                "status": "available",
                "result_count": 40,
                "article_identifiers": [f"PMC{i}" for i in range(40)],
            },
            "pubmed": {
                "status": "available",
                "result_count": 30,
                "article_identifiers": [f"{10000000+i}" for i in range(30)],
            },
        },
        "articles": [
            {
                "pmid": f"{20000000+i}",
                "pmcid": f"PMC{5000000+i}",
                "title": (
                    f"A comprehensive longitudinal multicenter cohort study of "
                    f"SCN1A missense variant c.3877G>A in Dravet syndrome cohort {i} "
                    + ("detailed description " * 15)
                ),
                "journal": "Journal of Clinical Investigation and Medical Genetics",
                "publication_date": "2024-06-15",
                "authors": [
                    f"Author Investigator {j}, M.D., Ph.D." for j in range(20)
                ],
                "doi": f"10.1038/s41436-024-0{i:04d}-x",
                "source_providers": ["litvar", "europe_pmc", "pubmed"],
                "url": f"https://europepmc.org/article/MED/{20000000+i}",
            }
            for i in range(10)
        ],
        "warnings": [],
    }
    original = deepcopy(candidate)

    first = build_evidence_object(candidate)
    second = build_evidence_object(candidate)

    assert first == second
    assert candidate == original

    literature = first["conditional_enrichment"]["literature"]
    assert _serialized_size(literature) <= MAX_EVIDENCE_LITERATURE_SECTION_BYTES
    compaction = literature["compaction"]
    assert set(compaction) == {
        "schema_version",
        "policy",
        "applied",
        "section_budget_bytes",
        "omitted_item_count",
        "omitted_content_sha256",
    }
    assert compaction["schema_version"] == "1.0"
    assert compaction["policy"] == "literature_optional_context_v1"
    assert compaction["applied"] is True
    assert compaction["section_budget_bytes"] == MAX_EVIDENCE_LITERATURE_SECTION_BYTES
    assert compaction["omitted_item_count"] > 0
    assert compaction["omitted_content_sha256"].startswith("sha256:")
    assert any(
        "Literature optional context was compacted" in warning
        for warning in literature["warnings"]
    )

    # Core citation fields preserved
    retained_articles = literature["articles"]
    assert len(retained_articles) > 0
    for article in retained_articles:
        assert article["pmid"].isdigit()
        assert article["doi"].startswith("10.1038/")
        assert article["journal"] == "Journal of Clinical Investigation and Medical Genetics"
        assert article["publication_date"] == "2024-06-15"
        assert len(article["authors"]) <= 3


def test_oversized_predictors_context_is_compacted_deterministically() -> None:
    candidate = _live_shape_candidate(0)
    candidate["predictors"] = {
        f"predictor_{i:02d}": {
            "score": 0.95,
            "prediction": "damaging",
            "rankscore": 0.98,
            "detail": (
                "Pathogenic prediction based on multi-species sequence "
                "conservation and structural homology modeling of sodium channel. "
                * 5
            ),
            "provider": "dbNSFP v4.4a",
        }
        for i in range(40)
    }
    original = deepcopy(candidate)

    first = build_evidence_object(candidate)
    second = build_evidence_object(candidate)

    assert first == second
    assert candidate == original

    predictors = first["annotations"]["predictors"]
    assert _serialized_size(predictors) <= MAX_EVIDENCE_PREDICTORS_SECTION_BYTES
    compaction = predictors["compaction"]
    assert set(compaction) == {
        "schema_version",
        "policy",
        "applied",
        "section_budget_bytes",
        "omitted_item_count",
        "omitted_content_sha256",
    }
    assert compaction["schema_version"] == "1.0"
    assert compaction["policy"] == "predictors_optional_context_v1"
    assert compaction["applied"] is True
    assert compaction["section_budget_bytes"] == MAX_EVIDENCE_PREDICTORS_SECTION_BYTES
    assert compaction["omitted_item_count"] > 0
    assert compaction["omitted_content_sha256"].startswith("sha256:")


def test_oversized_medgen_context_is_compacted_deterministically() -> None:
    candidate = _live_shape_candidate(0)
    candidate["medgen_disease_hpo_context"] = {
        "schema_version": "1.1",
        "provider": "NCBI MedGen",
        "provider_id": "ncbi_medgen",
        "provider_role": "primary",
        "fallback_used": False,
        "primary_provider": "NCBI MedGen",
        "primary_retrieval_state": "available",
        "status": "success",
        "retrieval_state": "available",
        "retrieved_at": "2026-08-20T12:00:00Z",
        "query_gene": "SCN1A",
        "query_key": "SCN1A",
        "attempts": 1,
        "http_status": 200,
        "upstream_sources": ["NCBI MedGen"],
        "enrichment_decision": {
            "triggered": True,
            "reason_codes": ["missing_context"],
            "target_semantic_node": "disease_hpo_context",
            "primary_retrieval_state": "available",
            "required_fields_missing": [],
            "query_key": "SCN1A",
        },
        "candidate_diagnostics": [],
        "queried_hpo_terms": [{"hpo_id": "HP:0001250", "label": "Seizures"}],
        "records": [
            {
                "medgen_uid": f"UID{i:05d}",
                "concept_id": f"C000{i:04d}",
                "title": f"Severe myoclonic epilepsy in infancy phenotype {i}",
                "semantic_type": "Disease or Syndrome",
                "definition": (
                    "Extensive medical genetic narrative description of the disease "
                    "phenotype, clinical presentation, and associated neurological findings. "
                    * 10
                ),
                "gene_association_match_state": "exact_gene_association",
                "gene_association_basis": "omim_morbid_map",
                "matched_hpo_terms": [{"hpo_id": "HP:0001250", "label": "Seizures"}],
                "hpo_match_method": "exact_hpo_id_conceptmeta",
                "source_metadata": [
                    {
                        "database": "OMIM",
                        "code": f"60720{i}",
                        "scui": None,
                        "term_type": None,
                    }
                    for i in range(5)
                ],
                "upstream_sources": ["NCBI MedGen", "OMIM"],
            }
            for i in range(8)
        ],
    }
    original = deepcopy(candidate)

    first = build_evidence_object(candidate)
    second = build_evidence_object(candidate)

    assert first == second
    assert candidate == original

    medgen = first["phenotype_relationship"]["medgen_disease_hpo_context"]
    assert _serialized_size(medgen) <= MAX_EVIDENCE_MEDGEN_SECTION_BYTES
    compaction = medgen["compaction"]
    assert set(compaction) == {
        "schema_version",
        "policy",
        "applied",
        "section_budget_bytes",
        "omitted_item_count",
        "omitted_content_sha256",
    }
    assert compaction["schema_version"] == "1.0"
    assert compaction["policy"] == "medgen_optional_context_v1"
    assert compaction["applied"] is True
    assert compaction["section_budget_bytes"] == MAX_EVIDENCE_MEDGEN_SECTION_BYTES
    assert compaction["omitted_item_count"] > 0
    assert compaction["omitted_content_sha256"].startswith("sha256:")

    retained_records = medgen["records"]
    assert len(retained_records) > 0
    for record in retained_records:
        assert record["concept_id"].startswith("C000")
        assert "epilepsy" in record["title"]
        assert record["gene_association_match_state"] == "exact_gene_association"
        assert record["matched_hpo_terms"] == [{"hpo_id": "HP:0001250", "label": "Seizures"}]


def test_oversized_mydisease_with_large_supporting_hpo_and_clinical_course_is_compacted_strictly_under_16kb() -> None:
    candidate = _live_shape_candidate(0)
    # Add diseases with extensive supporting_hpo_terms and clinical_course
    candidate["mydisease"]["inferred_pathway_context"] = [
        {
            "disease_id": f"MONDO:000{i:04d}",
            "association_type": "inferred_via_pathway",
            "classification_effect": "contributory",
            "inference_gene": "LMNA",
            "pathway_id": f"REACTOME:R-HSA-{100000+i}",
            "pathway_name": f"Nuclear envelope lamin assembly signaling cascade pathway {i}",
            "provider": "Reactome",
            "upstream_source": "Reactome",
        }
        for i in range(10)
    ]
    candidate["mydisease"]["diseases"] = [
        {
            "disease_id": f"MONDO:001467{i}",
            "disease_name": f"Emery-Dreifuss muscular dystrophy type {i}",
            "synonyms": [f"Synonym {j} for EDMD disorder {i}" for j in range(10)],
            "primary_source": "MONDO",
            "cross_references": {
                "omim": [f"60720{i}"],
                "orphanet": [f"ORPHA{200+i}"],
            },
            "gene_disease_relation": "causative_germline_mutation",
            "matched_patient_hpo_terms": ["HP:0001250"],
            "unmatched_patient_hpo_terms": ["HP:0001257"],
            "phenotype_match_count": 1,
            "phenotype_match_status": "partial_match",
            "upstream_sources": ["MyDisease.info", "MONDO", "OMIM"],
            "warnings": [],
            "supporting_hpo_terms": [
                {
                    "hpo_id": f"HP:000{1000+j:04d}",
                    "hpo_name": f"Phenotypic feature finding {j} associated with Emery-Dreifuss syndrome",
                    "biocuration": "HPO:probinson",
                    "upstream_source": "HPO",
                }
                for j in range(20)
            ],
            "inheritance": [
                {
                    "hpo_id": "HP:0000005",
                    "hpo_name": "Autosomal dominant inheritance",
                    "biocuration": "HPO:probinson",
                    "upstream_source": "HPO",
                }
            ],
            "clinical_course": [
                {
                    "hpo_id": f"HP:001{2000+j:04d}",
                    "hpo_name": f"Clinical progression stage {j} during adolescent disease onset",
                    "biocuration": "HPO:probinson",
                    "upstream_source": "HPO",
                }
                for j in range(10)
            ],
            "clinical_modifier": [],
        }
        for i in range(5)
    ]

    original = deepcopy(candidate)
    first = build_evidence_object(candidate)
    second = build_evidence_object(candidate)

    assert first == second
    assert candidate == original

    mydisease = first["phenotype_relationship"]["mydisease"]
    assert _serialized_size(mydisease) <= MAX_EVIDENCE_MYDISEASE_SECTION_BYTES
    compaction = mydisease["compaction"]
    assert compaction["schema_version"] == "1.0"
    assert compaction["policy"] == "mydisease_optional_context_v1"
    assert compaction["applied"] is True
    assert compaction["section_budget_bytes"] == MAX_EVIDENCE_MYDISEASE_SECTION_BYTES
    assert compaction["omitted_item_count"] > 0
    assert compaction["omitted_content_sha256"].startswith("sha256:")

    # Disease identities and gene relations preserved
    retained_diseases = mydisease["diseases"]
    assert len(retained_diseases) > 0
    for d in retained_diseases:
        assert d["disease_id"].startswith("MONDO:")
        assert d["gene_disease_relation"] == "causative_germline_mutation"
        assert d["matched_patient_hpo_terms"] == ["HP:0001250"]


def test_oversized_medgen_gene_disease_context_is_compacted_strictly_under_4kb() -> None:
    candidate = _live_shape_candidate(0)
    candidate["medgen_gene_disease_context"] = {
        "schema_version": "1.0",
        "provider": "NCBI MedGen",
        "provider_id": "ncbi_medgen",
        "provider_role": "primary_fallback",
        "primary_provider": "clingen",
        "primary_retrieval_state": "unavailable",
        "status": "success",
        "retrieval_state": "available",
        "retrieved_at": "2026-08-20T12:00:00Z",
        "query_gene": "LMNA",
        "query_key": "LMNA",
        "attempts": 1,
        "http_status": 200,
        "upstream_sources": ["NCBI MedGen", "OMIM"],
        "enrichment_decision": {
            "triggered": True,
            "reason_codes": ["clingen_unavailable"],
            "target_semantic_node": "gene_disease_context",
            "primary_retrieval_state": "unavailable",
            "required_fields_missing": ["gene_disease_evidence"],
            "query_key": "LMNA",
        },
        "candidate_diagnostics": [
            {
                "medgen_uid": f"DIAG_{i}",
                "concept_id": f"C00{i}",
                "title": f"Diagnostic title {i}",
                "gene_association_match_state": "gene_mismatch",
                "rejection_reason": "Gene symbol mismatch against ClinGen database query",
            }
            for i in range(10)
        ],
        "records": [
            {
                "medgen_uid": f"UID{i:05d}",
                "concept_id": f"C000{i:04d}",
                "title": f"Cardiomyopathy dilated 1A phenotype {i}",
                "semantic_type": "Disease or Syndrome",
                "definition": "Detailed description of LMNA gene-disease association and clinical morbid map " * 8,
                "gene_association_match_state": "exact_gene_association",
                "gene_association_basis": "omim_morbid_map",
                "hpo_terms": [{"hpo_id": "HP:0001250", "label": "Seizures"}],
                "source_metadata": [
                    {"database": "OMIM", "code": f"11520{i}", "scui": None, "term_type": None}
                    for i in range(5)
                ],
                "upstream_sources": ["NCBI MedGen", "OMIM"],
            }
            for i in range(8)
        ],
    }

    original = deepcopy(candidate)
    first = build_evidence_object(candidate)
    second = build_evidence_object(candidate)

    assert first == second
    assert candidate == original

    medgen_gd = first["pathogenicity"]["medgen_gene_disease_context"]
    assert _serialized_size(medgen_gd) <= MAX_EVIDENCE_MEDGEN_GD_SECTION_BYTES
    compaction = medgen_gd["compaction"]
    assert compaction["schema_version"] == "1.0"
    assert compaction["policy"] == "medgen_gene_disease_optional_context_v1"
    assert compaction["applied"] is True
    assert compaction["section_budget_bytes"] == MAX_EVIDENCE_MEDGEN_GD_SECTION_BYTES
    assert compaction["omitted_item_count"] > 0
    assert compaction["omitted_content_sha256"].startswith("sha256:")

    retained_records = medgen_gd["records"]
    assert len(retained_records) > 0
    for r in retained_records:
        assert r["concept_id"].startswith("C000")
        assert r["gene_association_match_state"] == "exact_gene_association"


def test_exact_live_lmna_candidate_stays_strictly_under_global_64kb_limit() -> None:
    conn = sqlite3.connect(r"storage/database/clinical_variant.sqlite3")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT pipeline_json FROM pipeline_states WHERE analysis_id='analysis-50b60fea0b5a49b79cf8741cf4cf5fba'"
    )
    row = cursor.fetchone()
    if not row:
        pytest.skip("Live analysis database entry not found")
    pj = json.loads(row[0])
    candidate = pj["phenotype_results"][1]

    evidence = build_evidence_object(candidate)
    total_size = _serialized_size(evidence)

    assert total_size <= MAX_EVIDENCE_SERIALIZED_BYTES
    assert total_size <= 61_000  # Proves > 4KB headroom below 65,536 limit


def test_fully_enriched_variant_stays_strictly_under_global_64kb_limit() -> None:
    candidate = _live_shape_candidate(1)
    
    # 1. MyDisease rich context
    diseases = candidate["mydisease"]["diseases"]
    for i in range(10):
        diseases.append({
            "disease_id": f"MONDO:000{100+i:04d}",
            "disease_name": f"Epileptic encephalopathy type {i}",
            "gene_disease_relation": "causative_germline_mutation",
            "matched_patient_hpo_terms": ["HP:0001250", "HP:0001257"],
            "synonyms": [f"Synonym {j} for disorder {i}" for j in range(15)],
            "cross_references": {
                "omim": [f"{600000+i}"],
                "orphanet": [f"ORPHA{1000+i}"],
            },
        })

    # 2. MedGen rich context
    candidate["medgen_disease_hpo_context"] = {
        "schema_version": "1.1",
        "provider": "NCBI MedGen",
        "provider_id": "ncbi_medgen",
        "provider_role": "primary",
        "fallback_used": False,
        "primary_provider": "NCBI MedGen",
        "primary_retrieval_state": "available",
        "status": "success",
        "retrieval_state": "available",
        "retrieved_at": "2026-08-20T12:00:00Z",
        "query_gene": "SCN1A",
        "query_key": "SCN1A",
        "attempts": 1,
        "http_status": 200,
        "upstream_sources": ["NCBI MedGen"],
        "enrichment_decision": {
            "triggered": True,
            "reason_codes": ["missing_context"],
            "target_semantic_node": "disease_hpo_context",
            "primary_retrieval_state": "available",
            "required_fields_missing": [],
            "query_key": "SCN1A",
        },
        "candidate_diagnostics": [],
        "queried_hpo_terms": [{"hpo_id": "HP:0001250", "label": "Seizures"}],
        "records": [
            {
                "medgen_uid": f"UID{i:05d}",
                "concept_id": f"C000{i:04d}",
                "title": f"Dravet syndrome cohort {i}",
                "semantic_type": "Disease or Syndrome",
                "definition": "Detailed description of Dravet syndrome phenotype " * 8,
                "gene_association_match_state": "exact_gene_association",
                "gene_association_basis": "omim_morbid_map",
                "matched_hpo_terms": [{"hpo_id": "HP:0001250", "label": "Seizures"}],
                "hpo_match_method": "exact_hpo_id_conceptmeta",
                "source_metadata": [{"database": "OMIM", "code": "607208", "scui": None, "term_type": None}],
                "upstream_sources": ["NCBI MedGen", "OMIM"],
            }
            for i in range(8)
        ],
    }

    # 3. Literature rich context
    candidate["conditional_enrichment"] = _population_fallback()
    candidate["conditional_enrichment"]["literature"] = {
        "status": "available",
        "providers": {
            "litvar": {"status": "available", "result_count": 50, "article_identifiers": [f"PMC{i}" for i in range(50)]},
            "europe_pmc": {"status": "available", "result_count": 40, "article_identifiers": [f"PMC{i}" for i in range(40)]},
            "pubmed": {"status": "available", "result_count": 30, "article_identifiers": [f"{10000000+i}" for i in range(30)]},
        },
        "articles": [
            {
                "pmid": f"{20000000+i}",
                "pmcid": f"PMC{5000000+i}",
                "title": f"Longitudinal study of SCN1A variants in Dravet syndrome {i} " + ("context " * 20),
                "journal": "Journal of Clinical Investigation",
                "publication_date": "2024-06-15",
                "authors": [f"Author {j}, M.D." for j in range(15)],
                "doi": f"10.1038/s41436-024-0{i:04d}-x",
                "source_providers": ["litvar", "europe_pmc", "pubmed"],
                "url": f"https://europepmc.org/article/MED/{20000000+i}",
            }
            for i in range(10)
        ],
        "warnings": [],
    }

    # 4. Predictors rich context
    candidate["predictors"] = {
        f"predictor_{i:02d}": {
            "score": 0.95,
            "prediction": "damaging",
            "rankscore": 0.98,
            "detail": "Pathogenic prediction based on multi-species sequence conservation " * 5,
            "provider": "dbNSFP v4.4a",
        }
        for i in range(35)
    }

    # 5. Population frequencies & cohorts
    candidate["sources"]["myvariant"]["population_frequencies"] = {
        "gnomad_exome": {f"pop_{i}": {"af": 0.0001, "ac": 1, "an": 10000, "hom": 0} for i in range(15)},
        "gnomad_genome": {f"pop_{i}": {"af": 0.0002, "ac": 2, "an": 10000, "hom": 0} for i in range(15)},
    }
    candidate["sources"]["myvariant"]["population_frequency_details"] = {
        f"cohort_{i}": {"dataset": f"gnomAD_v4.1_{i}", "frequency": 0.0001}
        for i in range(20)
    }

    evidence = build_evidence_object(candidate)
    total_size = _serialized_size(evidence)

    # Must be safely bounded below global 64KB limit
    assert total_size <= MAX_EVIDENCE_SERIALIZED_BYTES
    assert total_size <= 55_000  # Proves comfortable headroom (> 10KB)

    # Invariants unchanged
    assert evidence["variant"]["chrom"] == candidate["variant"]["chrom"]
    assert evidence["variant"]["pos"] == candidate["variant"]["pos"]
    assert evidence["variant"]["ref"] == candidate["variant"]["ref"]
    assert evidence["variant"]["alt"] == candidate["variant"]["alt"]
    assert evidence["assembly"] == candidate["assembly"]
    assert evidence["gene"] == candidate["gene"]
