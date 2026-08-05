"""Run one bounded, non-automated MyDisease.info diagnostic."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.mydisease import (  # noqa: E402
    clear_mydisease_cache,
    enrich_with_mydisease,
)
from config import settings  # noqa: E402


def main() -> int:
    clear_mydisease_cache()
    variant = {
        "variant": {
            "chrom": "2",
            "pos": 166848215,
            "ref": "C",
            "alt": "T",
        },
        "gene": "SCN1A",
        "phen2gene": {
            "availability": "available",
            "score": 0.95,
        },
        "sources": {
            "genebe": {
                "status": "success",
                "gene": "SCN1A",
                "gene_hgnc_id": 10585,
            }
        },
    }
    with requests.Session() as session:
        result = enrich_with_mydisease(
            [variant],
            ["HP:0001250"],
            session=session,
        )
    evidence = result["variants"][0]["mydisease"]
    diseases = evidence["diseases"]
    disease_ids = [
        disease["disease_id"]
        for disease in diseases
        if isinstance(disease, dict)
    ]
    retained_hpo_count = sum(
        len(disease.get("supporting_hpo_terms", []))
        for disease in diseases
        if isinstance(disease, dict)
    )
    matched = sorted(
        {
            term
            for disease in diseases
            if isinstance(disease, dict)
            for term in disease.get("matched_patient_hpo_terms", [])
            if isinstance(term, str)
        }
    )
    diagnostic = {
        "base_url": settings.MYDISEASE_BASE_URL,
        "endpoint": "/query",
        "query": evidence["query"],
        "http_status": evidence["http_status"],
        "provider_total": evidence["provider_total"],
        "provider_returned_count": evidence[
            "provider_returned_count"
        ],
        "normalized_disease_count": evidence["disease_count"],
        "direct_disease_ids": disease_ids,
        "retained_hpo_count": retained_hpo_count,
        "matched_patient_hpo": matched,
        "status": evidence["status"],
        "provider_version": evidence["provider_version"],
        "raw_response_persisted": (
            "raw_api_payload" in json.dumps(result).casefold()
        ),
    }
    print(json.dumps(diagnostic, indent=2, sort_keys=True))
    return 0 if evidence["status"] in {
        "available",
        "no_association",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
