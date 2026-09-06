"""Tests for Evidence Repository integration across VEP, GeneBe, and MyVariant."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote
import pytest
import requests

from backend.annotation import annotate_variants
from backend.evidence_repository import EvidenceRepository
from backend.evidence_repository_freshness import EvidenceFreshnessPolicy
from backend.provider_resilience import ProviderCircuitState
from config import settings


class MockResponse:
    def __init__(self, status_code: int, json_data: Any, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {"content-type": "application/json"}
        self.text = str(json_data)

    def json(self) -> Any:
        return self._json_data


class MockAnnotationSession:
    """Mock session tracking calls across VEP, GeneBe, and MyVariant."""

    def __init__(self) -> None:
        self.vep_batches: list[list[str]] = []
        self.genebe_batches: list[list[dict[str, Any]]] = []
        self.myvariant_calls: list[str] = []
        self.vep_handler: Any = None
        self.genebe_handler: Any = None
        self.myvariant_handler: Any = None

    def post(self, url: str, **kwargs: Any) -> MockResponse:
        if f"{settings.VEP_BASE_URL}/vep/homo_sapiens/region" in url:
            inputs = kwargs.get("json", {}).get("variants", [])
            self.vep_batches.append(inputs)
            if self.vep_handler is not None:
                if isinstance(self.vep_handler, Exception):
                    raise self.vep_handler
                if callable(self.vep_handler):
                    return self.vep_handler(inputs, **kwargs)
                return self.vep_handler
            return MockResponse(
                200,
                [
                    {
                        "input": item,
                        "id": f"vep_{idx}",
                        "assembly_name": settings.GENOME_ASSEMBLY,
                        "most_severe_consequence": "missense_variant",
                        "transcript_consequences": [
                            {
                                "gene_symbol": "TESTGENE",
                                "gene_id": "ENSG00000000001",
                                "transcript_id": "ENST00000000001",
                                "consequence_terms": ["missense_variant"],
                                "canonical": 1,
                            }
                        ],
                    }
                    for idx, item in enumerate(inputs)
                ],
            )

        if f"{settings.GENEBE_BASE_URL}/api-public/v1/variants" in url:
            variants = kwargs.get("json", [])
            self.genebe_batches.append(variants)
            if self.genebe_handler is not None:
                if isinstance(self.genebe_handler, Exception):
                    raise self.genebe_handler
                if callable(self.genebe_handler):
                    return self.genebe_handler(variants, **kwargs)
                return self.genebe_handler
            return MockResponse(
                200,
                {
                    "variants": [
                        {
                            "chr": v.get("chr"),
                            "pos": v.get("pos"),
                            "ref": v.get("ref"),
                            "alt": v.get("alt"),
                            "gene_symbol": "TESTGENE",
                            "transcript": "ENST00000000001",
                            "effect": "missense_variant",
                            "consequences": [],
                            "acmg_classification": "Pathogenic",
                            "acmg_criteria": ["PM2", "PP3"],
                            "acmg_score": 8,
                        }
                        for v in variants
                    ]
                },
            )

        return MockResponse(200, {})

    def get(self, url: str, **kwargs: Any) -> MockResponse:
        if f"{settings.MYVARIANT_BASE_URL}/variant/" in url:
            self.myvariant_calls.append(url)
            if self.myvariant_handler is not None:
                if isinstance(self.myvariant_handler, Exception):
                    raise self.myvariant_handler
                if callable(self.myvariant_handler):
                    return self.myvariant_handler(url, **kwargs)
                return self.myvariant_handler
            variant_id = unquote(url.split("/variant/")[-1].split("?")[0])
            return MockResponse(
                200,
                {
                    "_id": variant_id,
                    "_version": 1,
                    "dbsnp": {"rsid": "rs12345"},
                    "gnomad_genome": {"af": {"af": 0.0001}},
                },
            )

        if "/overlap/region/human/" in url:
            return MockResponse(404, [])
        if f"{settings.UCSC_GNOMAD_BASE_URL}/getData/track" in url:
            track = kwargs.get("params", {}).get("track", "")
            return MockResponse(200, {track: []})
        if url.endswith(("/esearch.fcgi", "/esummary.fcgi")):
            return MockResponse(200, {"esearchresult": {"count": "0", "idlist": []}})
        if f"{settings.CLINGEN_BASE_URL}/getData/track" in url:
            return MockResponse(200, {"gene_disease": []})
        if f"{settings.CSPEC_BASE_URL}/api/rules/" in url:
            return MockResponse(404, {})

        return MockResponse(200, {})


def _variant_a() -> dict[str, Any]:
    return {"chrom": "1", "pos": 100_000, "ref": "A", "alt": "G"}


def _variant_b() -> dict[str, Any]:
    return {"chrom": "2", "pos": 200_000, "ref": "C", "alt": "T"}


def _variant_c() -> dict[str, Any]:
    return {"chrom": "3", "pos": 300_000, "ref": "G", "alt": "A"}


def test_first_call_misses_and_stores_second_call_hits_without_network(tmp_path: Path) -> None:
    repo = EvidenceRepository(tmp_path / "evidence_repo.sqlite3")
    session1 = MockAnnotationSession()

    res1 = annotate_variants(
        [_variant_a()],
        session=session1,
        evidence_repository=repo,
        use_repository=True,
    )

    assert len(session1.vep_batches) == 1
    assert len(session1.genebe_batches) == 1
    assert len(session1.myvariant_calls) == 1
    assert res1[0]["sources"]["vep"]["source_mode"] == "live_provider"
    assert res1[0]["sources"]["genebe"]["source_mode"] == "live_provider"
    assert res1[0]["sources"]["myvariant"]["source_mode"] == "live_provider"

    session2 = MockAnnotationSession()
    res2 = annotate_variants(
        [_variant_a()],
        session=session2,
        evidence_repository=repo,
        use_repository=True,
    )

    assert len(session2.vep_batches) == 0
    assert len(session2.genebe_batches) == 0
    assert len(session2.myvariant_calls) == 0

    assert res2[0]["sources"]["vep"]["source_mode"] == "repository_cache"
    assert res2[0]["sources"]["genebe"]["source_mode"] == "repository_cache"
    assert res2[0]["sources"]["myvariant"]["source_mode"] == "repository_cache"
    assert res2[0]["sources"]["vep"]["original_retrieved_at"] == res1[0]["sources"]["vep"]["retrieved_at"]
    assert res2[0]["gene"] == "TESTGENE"


def test_persistence_across_separate_repository_instances(tmp_path: Path) -> None:
    db_file = tmp_path / "persistent.sqlite3"
    repo1 = EvidenceRepository(db_file)
    session1 = MockAnnotationSession()

    annotate_variants(
        [_variant_a()],
        session=session1,
        evidence_repository=repo1,
        use_repository=True,
    )
    assert len(session1.myvariant_calls) == 1

    repo2 = EvidenceRepository(db_file)
    session2 = MockAnnotationSession()

    res = annotate_variants(
        [_variant_a()],
        session=session2,
        evidence_repository=repo2,
        use_repository=True,
    )
    assert len(session2.vep_batches) == 0
    assert len(session2.genebe_batches) == 0
    assert len(session2.myvariant_calls) == 0
    assert res[0]["sources"]["myvariant"]["source_mode"] == "repository_cache"


def test_partial_batch_hit_only_sends_uncached_variants(tmp_path: Path) -> None:
    repo = EvidenceRepository(tmp_path / "evidence_repo.sqlite3")

    prime_session = MockAnnotationSession()
    annotate_variants(
        [_variant_a()],
        session=prime_session,
        evidence_repository=repo,
        use_repository=True,
    )

    batch_session = MockAnnotationSession()
    results = annotate_variants(
        [_variant_a(), _variant_b(), _variant_c()],
        session=batch_session,
        evidence_repository=repo,
        use_repository=True,
    )

    assert len(results) == 3
    assert results[0]["variant"]["pos"] == 100_000
    assert results[1]["variant"]["pos"] == 200_000
    assert results[2]["variant"]["pos"] == 300_000

    assert len(batch_session.vep_batches) == 1
    vep_inputs = batch_session.vep_batches[0]
    assert len(vep_inputs) == 2
    assert "1 100000" not in vep_inputs[0]
    assert "1 100000" not in vep_inputs[1]

    assert len(batch_session.genebe_batches) == 1
    genebe_vars = batch_session.genebe_batches[0]
    assert len(genebe_vars) == 2
    assert all(v["pos"] != 100_000 for v in genebe_vars)

    assert len(batch_session.myvariant_calls) == 2

    assert results[0]["sources"]["vep"]["source_mode"] == "repository_cache"
    assert results[1]["sources"]["vep"]["source_mode"] == "live_provider"
    assert results[2]["sources"]["vep"]["source_mode"] == "live_provider"


def test_outage_resilience_uses_cached_evidence_when_circuit_is_open(tmp_path: Path) -> None:
    repo = EvidenceRepository(tmp_path / "evidence_repo.sqlite3")

    prime_session = MockAnnotationSession()
    annotate_variants(
        [_variant_a()],
        session=prime_session,
        evidence_repository=repo,
        use_repository=True,
    )

    fail_session = MockAnnotationSession()
    fail_session.myvariant_handler = requests.HTTPError("503 Service Unavailable")
    fail_session.vep_handler = requests.HTTPError("503 Service Unavailable")
    fail_session.genebe_handler = requests.HTTPError("503 Service Unavailable")

    results = annotate_variants(
        [_variant_a()],
        session=fail_session,
        evidence_repository=repo,
        use_repository=True,
    )

    assert results[0]["sources"]["vep"]["status"] == "success"
    assert results[0]["sources"]["genebe"]["status"] == "success"
    assert results[0]["sources"]["myvariant"]["status"] == "success"
    assert results[0]["sources"]["vep"]["source_mode"] == "repository_cache"


def test_operational_failure_is_never_stored_in_repository(tmp_path: Path) -> None:
    repo = EvidenceRepository(tmp_path / "evidence_repo.sqlite3")
    session = MockAnnotationSession()
    session.myvariant_handler = requests.Timeout("Gateway timeout")

    annotate_variants(
        [_variant_a()],
        session=session,
        max_retries=0,
        evidence_repository=repo,
        use_repository=True,
    )

    records = repo.find_by_canonical_identity(
        canonical_variant=_variant_a(),
        assembly=settings.GENOME_ASSEMBLY,
        semantic_node="annotation",
        provider="myvariant",
    )
    assert records == []


def test_valid_no_match_is_cached_and_replayed(tmp_path: Path) -> None:
    repo = EvidenceRepository(tmp_path / "evidence_repo.sqlite3")
    session1 = MockAnnotationSession()
    session1.myvariant_handler = MockResponse(404, {"error": "not found"})

    res1 = annotate_variants(
        [_variant_a()],
        session=session1,
        evidence_repository=repo,
        use_repository=True,
    )
    assert res1[0]["sources"]["myvariant"]["status"] == "not_found"

    records = repo.find_by_canonical_identity(
        canonical_variant=_variant_a(),
        assembly=settings.GENOME_ASSEMBLY,
        semantic_node="annotation",
        provider="myvariant",
    )
    assert len(records) == 1
    assert records[0].observation_status == "no_match"

    session2 = MockAnnotationSession()
    res2 = annotate_variants(
        [_variant_a()],
        session=session2,
        evidence_repository=repo,
        use_repository=True,
    )
    assert len(session2.myvariant_calls) == 0
    assert res2[0]["sources"]["myvariant"]["status"] == "not_found"
    assert res2[0]["sources"]["myvariant"]["source_mode"] == "repository_cache"


def test_expired_entry_triggers_live_call(tmp_path: Path) -> None:
    repo = EvidenceRepository(tmp_path / "evidence_repo.sqlite3")
    session1 = MockAnnotationSession()

    annotate_variants(
        [_variant_a()],
        session=session1,
        evidence_repository=repo,
        use_repository=True,
    )

    short_policy = EvidenceFreshnessPolicy(
        default_ttl_seconds=10,
        ttl_seconds_by_scope={},
    )

    future_clock = lambda: datetime.now(UTC) + timedelta(hours=1)

    session2 = MockAnnotationSession()
    res2 = annotate_variants(
        [_variant_a()],
        session=session2,
        evidence_repository=repo,
        use_repository=True,
        repository_freshness_policy=short_policy,
        repository_clock=future_clock,
    )

    assert len(session2.myvariant_calls) == 1
    assert len(session2.vep_batches) == 1
    assert res2[0]["sources"]["myvariant"]["source_mode"] == "live_provider"


def test_repository_failure_gracefully_degrades_to_live_calls(tmp_path: Path) -> None:
    invalid_file = tmp_path / "not_a_dir" / "repo.sqlite3"
    repo = EvidenceRepository(invalid_file)

    session = MockAnnotationSession()
    res = annotate_variants(
        [_variant_a()],
        session=session,
        evidence_repository=repo,
        use_repository=True,
    )

    assert len(session.vep_batches) == 1
    assert len(session.genebe_batches) == 1
    assert len(session.myvariant_calls) == 1
    assert res[0]["sources"]["vep"]["source_mode"] == "live_provider"


def test_canonical_identity_mismatch_never_hits(tmp_path: Path) -> None:
    """Variants with different alleles or coordinates never hit the cache."""
    repo = EvidenceRepository(tmp_path / "evidence_repo.sqlite3")

    session1 = MockAnnotationSession()
    annotate_variants(
        [_variant_a()],
        session=session1,
        evidence_repository=repo,
        use_repository=True,
    )
    assert len(session1.myvariant_calls) == 1

    # Query variant with different ALT
    diff_alt = {"chrom": "1", "pos": 100_000, "ref": "A", "alt": "C"}
    session2 = MockAnnotationSession()
    res = annotate_variants(
        [diff_alt],
        session=session2,
        evidence_repository=repo,
        use_repository=True,
    )
    # Must query providers because identity does not match!
    assert len(session2.myvariant_calls) == 1
    assert len(session2.vep_batches) == 1
    assert res[0]["sources"]["myvariant"]["source_mode"] == "live_provider"
