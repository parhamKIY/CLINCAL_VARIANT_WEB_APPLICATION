"""Tests for MyVariant provider-wide circuit state and request suppression."""

from __future__ import annotations

import time
from typing import Any
import pytest
import requests

import backend.annotation as ann
from backend.annotation import annotate_variants
from config import settings


class MockResponse:
    def __init__(self, status_code: int, json_data: Any = None) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.headers = {"Content-Type": "application/json"}

    def json(self) -> Any:
        return self._json_data


class CountingSession:
    """Session that counts requests and routes to specific mock handlers."""

    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.myvariant_calls: list[dict[str, Any]] = []
        self.ensembl_variation_calls: list[dict[str, Any]] = []
        self.ucsc_calls: list[dict[str, Any]] = []
        self.clinvar_calls: list[dict[str, Any]] = []
        self.clingen_calls: list[dict[str, Any]] = []
        self.cspec_calls: list[dict[str, Any]] = []
        self.myvariant_handler: Any = None

    def post(self, url: str, **kwargs: Any) -> MockResponse:
        self.post_calls.append({"url": url, **kwargs})
        if f"{settings.VEP_BASE_URL}/vep/homo_sapiens/region" in url:
            variants_input = kwargs.get("json", {}).get("variants", [])
            vep_records = []
            for idx, item in enumerate(variants_input):
                vep_records.append(
                    {
                        "input": item,
                        "id": f"var_{idx}",
                        "most_severe_consequence": "missense_variant",
                        "transcript_consequences": [
                            {
                                "gene_symbol": f"GENE_{idx}",
                                "gene_id": f"ENSG0000000000{idx}",
                                "transcript_id": f"ENST0000000000{idx}",
                                "consequence_terms": ["missense_variant"],
                                "canonical": 1,
                            }
                        ],
                    }
                )
            return MockResponse(200, vep_records)

        if f"{settings.GENEBE_BASE_URL}/api-public/v1/variants" in url:
            req_variants = kwargs.get("json", [])
            return MockResponse(
                200,
                {
                    "variants": [
                        {
                            **v,
                            "effect": None,
                            "transcript": None,
                            "gene_symbol": None,
                            "consequences": [],
                            "acmg_classification": None,
                            "acmg_criteria": None,
                            "acmg_score": None,
                        }
                        for v in req_variants
                    ]
                },
            )
        return MockResponse(200, {})

    def get(self, url: str, **kwargs: Any) -> MockResponse:
        call = {"url": url, **kwargs}
        if f"{settings.MYVARIANT_BASE_URL}/variant/" in url:
            self.myvariant_calls.append(call)
            if isinstance(self.myvariant_handler, Exception):
                raise self.myvariant_handler
            if callable(self.myvariant_handler):
                res = self.myvariant_handler(url, **kwargs)
                if isinstance(res, Exception):
                    raise res
                return res
            return self.myvariant_handler or MockResponse(404, {})

        if "/overlap/region/human/" in url:
            self.ensembl_variation_calls.append(call)
            return MockResponse(404, [])

        if f"{settings.UCSC_GNOMAD_BASE_URL}/getData/track" in url:
            self.ucsc_calls.append(call)
            track = kwargs.get("params", {}).get("track", "")
            return MockResponse(200, {track: []})

        if url.endswith(("/esearch.fcgi", "/esummary.fcgi")):
            self.clinvar_calls.append(call)
            return MockResponse(200, {"esearchresult": {"count": "0", "idlist": []}})

        if f"{settings.CLINGEN_BASE_URL}/getData/track" in url:
            self.clingen_calls.append(call)
            return MockResponse(200, {"genCC": [], "itemsReturned": 0})

        if url.startswith(f"{settings.CSPEC_BASE_URL}/"):
            self.cspec_calls.append(call)
            return MockResponse(404, {"status": {"code": 404}})

        return MockResponse(404, {})


def _make_variant(pos: int, chrom: str = "1", ref: str = "A", alt: str = "G") -> dict[str, Any]:
    return {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt}


@pytest.mark.stage59_testing_v3
@pytest.mark.testing_v3_recovery
def test_myvariant_operational_outage_opens_circuit_and_prevents_repeated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operational failure on variant 1 must open circuit and skip variants 2 and 3."""
    monkeypatch.setattr(settings, "ENABLE_EREPO", False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    session = CountingSession()
    session.myvariant_handler = requests.ConnectionError("Connection refused by host")

    variants = [_make_variant(100), _make_variant(200), _make_variant(300)]
    annotations = annotate_variants(
        variants,
        session=session,  # type: ignore[arg-type]
        max_retries=2,
    )

    # Variant 1 made 1 attempt + 2 retries = 3 calls.
    # Variants 2 and 3 must make 0 calls because circuit opened.
    # Retry loop must also make 0 calls.
    assert len(session.myvariant_calls) == 3

    # All 3 variants must record the operational failure and trigger fallback paths
    for item in annotations:
        source = item["sources"]["myvariant"]
        assert source["status"] == "error"
        assert source["primary_failure"] == "unavailable"
        # UCSC gnomAD fallback was attempted
        pop_prov = item["population_frequency_provenance"]
        assert pop_prov["fallback_used"] is True
        assert pop_prov["primary_failure"] == "unavailable"


@pytest.mark.stage59_testing_v3
@pytest.mark.testing_v3_recovery
def test_myvariant_not_found_does_not_open_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 / no_match on variant 1 must NOT open the circuit for variant 2."""
    monkeypatch.setattr(settings, "ENABLE_EREPO", False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    session = CountingSession()

    def handler(url: str, **kwargs: Any) -> MockResponse:
        if "100" in url:
            return MockResponse(404, {"error": "not found"})
        return MockResponse(
            200,
            {
                "_id": "chr1:g.200C>T",
                "dbsnp": {"rsid": "rs999"},
                "gnomad_exome": {"af": 0.001},
            },
        )

    session.myvariant_handler = handler
    variants = [_make_variant(100, ref="A", alt="G"), _make_variant(200, ref="C", alt="T")]
    annotations = annotate_variants(
        variants,
        session=session,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert len(session.myvariant_calls) == 2
    assert annotations[0]["sources"]["myvariant"]["status"] == "not_found"
    assert annotations[1]["sources"]["myvariant"]["status"] == "success"
    assert annotations[1]["sources"]["myvariant"]["rsid"] == "rs999"


@pytest.mark.stage59_testing_v3
@pytest.mark.testing_v3_recovery
def test_myvariant_unsupported_variant_does_not_open_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported variant must NOT open the circuit for a subsequent valid variant."""
    monkeypatch.setattr(settings, "ENABLE_EREPO", False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    session = CountingSession()
    session.myvariant_handler = MockResponse(
        200,
        {
            "_id": "chr1:g.200C>T",
            "dbsnp": {"rsid": "rs999"},
            "gnomad_exome": {"af": 0.001},
        },
    )

    orig_to_hgvs = ann._to_myvariant_hgvs
    monkeypatch.setattr(
        ann,
        "_to_myvariant_hgvs",
        lambda v: None if v.get("pos") == 100 else orig_to_hgvs(v),
    )

    unsupported_variant = _make_variant(100, ref="A", alt="G")
    valid_variant = _make_variant(200, ref="C", alt="T")
    annotations = annotate_variants(
        [unsupported_variant, valid_variant],
        session=session,  # type: ignore[arg-type]
        max_retries=0,
    )

    # Variant 1 is unsupported (0 MyVariant HTTP calls).
    # Variant 2 makes 1 call and succeeds.
    assert len(session.myvariant_calls) == 1
    assert annotations[0]["sources"]["myvariant"]["status"] == "unsupported"
    assert annotations[1]["sources"]["myvariant"]["status"] == "success"
    assert annotations[1]["sources"]["myvariant"]["rsid"] == "rs999"


@pytest.mark.stage59_testing_v3
@pytest.mark.testing_v3_recovery
def test_myvariant_fresh_circuit_per_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subsequent independent analysis run gets a fresh closed circuit."""
    monkeypatch.setattr(settings, "ENABLE_EREPO", False)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Run 1: Outage
    session1 = CountingSession()
    session1.myvariant_handler = requests.ConnectionError("Network down")
    annotations1 = annotate_variants(
        [_make_variant(100), _make_variant(200)],
        session=session1,  # type: ignore[arg-type]
        max_retries=1,
    )
    assert len(session1.myvariant_calls) == 2  # 1 attempt + 1 retry for var1; 0 for var2
    assert annotations1[0]["sources"]["myvariant"]["status"] == "error"
    assert annotations1[1]["sources"]["myvariant"]["status"] == "error"

    # Run 2: Service restored
    session2 = CountingSession()
    session2.myvariant_handler = MockResponse(
        200,
        {
            "_id": "chr1:g.100A>G",
            "dbsnp": {"rsid": "rs123"},
        },
    )
    annotations2 = annotate_variants(
        [_make_variant(100)],
        session=session2,  # type: ignore[arg-type]
        max_retries=1,
    )
    assert len(session2.myvariant_calls) == 1
    assert annotations2[0]["sources"]["myvariant"]["status"] == "success"
    assert annotations2[0]["sources"]["myvariant"]["rsid"] == "rs123"
@pytest.mark.stage59_testing_v3
@pytest.mark.testing_v3_recovery
def test_myvariant_transient_retry_success_does_not_open_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure that succeeds on retry must NOT open the circuit."""
    monkeypatch.setattr(settings, "ENABLE_EREPO", False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    session = CountingSession()
    attempt_count = 0

    def handler(url: str, **kwargs: Any) -> MockResponse:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            return MockResponse(503, {"error": "transient busy"})
        return MockResponse(
            200,
            {
                "_id": "chr1:g.100A>G" if "100" in url else "chr1:g.200C>T",
                "dbsnp": {"rsid": "rs123"},
            },
        )

    session.myvariant_handler = handler
    variants = [_make_variant(100, ref="A", alt="G"), _make_variant(200, ref="C", alt="T")]
    annotations = annotate_variants(
        variants,
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )

    # Variant 1 made 1 attempt (503) + 1 retry (200) = 2 calls.
    # Variant 2 made 1 call (200).
    # Total calls = 3.
    assert len(session.myvariant_calls) == 3
    assert annotations[0]["sources"]["myvariant"]["status"] == "success"
    assert annotations[1]["sources"]["myvariant"]["status"] == "success"
@pytest.mark.stage59_testing_v3
@pytest.mark.testing_v3_recovery
def test_myvariant_identity_mismatch_does_not_open_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact-identity mismatch on variant 1 must NOT open the circuit for variant 2."""
    monkeypatch.setattr(settings, "ENABLE_EREPO", False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    session = CountingSession()

    def handler(url: str, **kwargs: Any) -> MockResponse:
        if "100" in url:
            # Variant 1 requests chr1:g.100A>G, but MyVariant returns _id=chr1:g.999A>G
            return MockResponse(
                200,
                {
                    "_id": "chr1:g.999A>G",
                    "dbsnp": {"rsid": "rs111"},
                },
            )
        # Variant 2 requests chr1:g.200C>T and has a valid matching response
        return MockResponse(
            200,
            {
                "_id": "chr1:g.200C>T",
                "dbsnp": {"rsid": "rs999"},
                "gnomad_exome": {"af": 0.001},
            },
        )

    session.myvariant_handler = handler
    variants = [_make_variant(100, ref="A", alt="G"), _make_variant(200, ref="C", alt="T")]
    annotations = annotate_variants(
        variants,
        session=session,  # type: ignore[arg-type]
        max_retries=0,
    )

    # Both variants must be queried because variant 1 mismatch must NOT open the circuit
    assert len(session.myvariant_calls) == 2

    # Variant 1 must be strictly rejected with error and invalid_response
    assert annotations[0]["sources"]["myvariant"]["status"] == "error"
    assert annotations[0]["sources"]["myvariant"]["primary_failure"] == "invalid_response"

    # Variant 2 must be queried and accepted with success
    assert annotations[1]["sources"]["myvariant"]["status"] == "success"
    assert annotations[1]["sources"]["myvariant"]["rsid"] == "rs999"
@pytest.mark.stage59_testing_v3
@pytest.mark.testing_v3_recovery
def test_myvariant_malformed_json_still_opens_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine operational invalid_response (e.g. malformed JSON) MUST open the circuit."""
    monkeypatch.setattr(settings, "ENABLE_EREPO", False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    session = CountingSession()

    class MalformedResponse(MockResponse):
        def json(self) -> Any:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    session.myvariant_handler = MalformedResponse(200)
    variants = [_make_variant(100), _make_variant(200)]
    annotations = annotate_variants(
        variants,
        session=session,  # type: ignore[arg-type]
        max_retries=0,
    )

    # Variant 1 fails with invalid JSON -> opens circuit.
    # Variant 2 makes 0 calls because circuit is open.
    assert len(session.myvariant_calls) == 1
    assert annotations[0]["sources"]["myvariant"]["status"] == "error"
    assert annotations[0]["sources"]["myvariant"]["primary_failure"] == "invalid_response"
    assert annotations[1]["sources"]["myvariant"]["status"] == "error"
    assert annotations[1]["sources"]["myvariant"]["primary_failure"] == "invalid_response"
