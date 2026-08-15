"""Strict UCSC gnomAD mirror tests for conditional verification."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import requests

from backend.conditional_enrichment import (
    UCSC_GNOMAD_TRACKS,
    fetch_population_evidence_with_fallback,
    fetch_ucsc_gnomad_evidence,
)
from backend.report import _compact_conditional_enrichment
from config import settings


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {"Content-Type": "application/json"}

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(
        self,
        *,
        post_responses: list[object] | None = None,
        get_responses: list[object] | None = None,
    ) -> None:
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    @staticmethod
    def _next(queue: list[object]) -> FakeResponse:
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, FakeResponse)
        return response

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return self._next(self.post_responses)

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self._next(self.get_responses)

    def close(self) -> None:
        return None


def _candidate() -> dict[str, Any]:
    return {
        "assembly": "GRCh38",
        "variant": {"chrom": "2", "pos": 166848215, "ref": "C", "alt": "T"},
        "population_frequency": 0.42,
        "sources": {"myvariant": {"rsid": "rs121913529"}},
    }


def _direct_gnomad_payload() -> dict[str, object]:
    return {
        "data": {
            "variant": {
                "variant_id": "2-166848215-C-T",
                "ref": "C",
                "alt": "T",
                "rsid": "rs121913529",
                "joint": {"ac": 2, "an": 200_000, "populations": []},
                "exome": {"ac": 1, "an": 150_000, "populations": []},
                "genome": {"ac": 1, "an": 50_000, "populations": []},
            }
        }
    }


def _ensembl_payload() -> dict[str, object]:
    return {
        "name": "rs121913529",
        "source": "dbSNP",
        "release": 156,
        "MAF": "0.001",
        "mappings": [
            {
                "assembly_name": "GRCh38",
                "seq_region_name": "2",
                "start": 166848215,
                "allele_string": "C/T",
            }
        ],
        "populations": [
            {
                "population": "1000GENOMES:phase_3:EUR",
                "allele": "T",
                "frequency": 0.002,
            }
        ],
    }


def _ucsc_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "chrom": "chr2",
        "chromStart": 166848214,
        "chromEnd": 166848215,
        "ref": "C",
        "alt": "T",
        "AF": 0.00012,
        "AC": 12,
        "AN": 100_000,
        "FILTER": "PASS",
        "AF_grpmax": 0.0004,
        "grpmax": "South Asian",
        "nhomalt": 0,
    }
    record.update(overrides)
    return record


def _ucsc_track_response(track: str, records: list[dict[str, object]]) -> FakeResponse:
    return FakeResponse(200, {track: records})


def _ucsc_success_responses() -> list[FakeResponse]:
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    exome = tracks["exome"]
    genome = tracks["genome"]
    assert isinstance(exome, str) and isinstance(genome, str)
    return [
        _ucsc_track_response(exome, [_ucsc_record(AF=0.00012, AC=12)]),
        _ucsc_track_response(genome, [_ucsc_record(AF=0.00008, AC=8)]),
    ]


def _ucsc_no_match_responses() -> list[FakeResponse]:
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    return [
        _ucsc_track_response(str(tracks["exome"]), []),
        _ucsc_track_response(str(tracks["genome"]), []),
    ]


def test_direct_gnomad_success_skips_ucsc_and_ensembl() -> None:
    session = FakeSession(
        post_responses=[FakeResponse(200, _direct_gnomad_payload())]
    )

    result = fetch_population_evidence_with_fallback(_candidate(), session=session)  # type: ignore[arg-type]

    assert result["provider"] == "gnomAD"
    assert result["fallback_used"] is False
    assert session.get_calls == []


def test_gnomad_failure_uses_ucsc_exact_mirror_before_ensembl() -> None:
    session = FakeSession(
        post_responses=[FakeResponse(403, {})],
        get_responses=_ucsc_success_responses(),
    )

    result = fetch_population_evidence_with_fallback(_candidate(), session=session)  # type: ignore[arg-type]

    assert result["provider"] == "UCSC gnomAD"
    assert result["operational_provider"] == "ucsc_gnomad"
    assert result["dataset"] == "gnomAD v4.1"
    assert result["underlying_dataset"] == "gnomAD"
    assert result["fallback_used"] is True
    assert result["global_af"] == {"exome": 0.00012, "genome": 0.00008}
    assert len(session.get_calls) == 2


def test_gnomad_no_match_continues_to_usable_ucsc_mirror() -> None:
    session = FakeSession(
        post_responses=[FakeResponse(200, {"data": {"variant": None}})],
        get_responses=_ucsc_success_responses(),
    )

    result = fetch_population_evidence_with_fallback(_candidate(), session=session)  # type: ignore[arg-type]

    assert result["provider"] == "UCSC gnomAD"
    assert result["preceding_provider"] == "gnomad"
    assert result["preceding_status"] == "no_match"
    assert result["fallback_used"] is False


def test_ucsc_multiallelic_response_selects_only_the_exact_alt() -> None:
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    session = FakeSession(
        post_responses=[FakeResponse(403, {})],
        get_responses=[
            _ucsc_track_response(
                str(tracks["exome"]),
                [_ucsc_record(alt="G", AF=0.8), _ucsc_record(AF=0.00012)],
            ),
            _ucsc_track_response(str(tracks["genome"]), [_ucsc_record(AF=0.00008)]),
        ],
    )

    result = fetch_population_evidence_with_fallback(_candidate(), session=session)  # type: ignore[arg-type]

    assert result["provider"] == "UCSC gnomAD"
    assert result["exome"]["allele_frequency"] == 0.00012


def test_ucsc_wrong_alt_or_build_mismatch_continues_to_ensembl() -> None:
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    session = FakeSession(
        post_responses=[FakeResponse(403, {})],
        get_responses=[
            _ucsc_track_response(str(tracks["exome"]), [_ucsc_record(alt="G")]),
            _ucsc_track_response(str(tracks["genome"]), [_ucsc_record(chrom="chr3")]),
            FakeResponse(200, _ensembl_payload()),
        ],
    )

    result = fetch_population_evidence_with_fallback(_candidate(), session=session)  # type: ignore[arg-type]

    assert result["provider"] == "Ensembl REST Variation"
    assert result["intermediate_provider"] == "ucsc_gnomad"
    assert result["intermediate_status"] == "no_match"


def test_ucsc_empty_or_malformed_records_continue_to_ensembl() -> None:
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    session = FakeSession(
        post_responses=[FakeResponse(403, {})],
        get_responses=[
            FakeResponse(200, {str(tracks["exome"]): {}}),
            _ucsc_track_response(str(tracks["genome"]), []),
            FakeResponse(200, _ensembl_payload()),
        ],
    )

    result = fetch_population_evidence_with_fallback(_candidate(), session=session)  # type: ignore[arg-type]

    assert result["provider"] == "Ensembl REST Variation"
    assert result["intermediate_status"] == "invalid_response"


def test_ucsc_missing_af_and_filtered_record_are_never_zero_or_usable() -> None:
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    missing_af = FakeSession(
        get_responses=[
            _ucsc_track_response(str(tracks["exome"]), [_ucsc_record(AF=None)]),
            _ucsc_track_response(str(tracks["genome"]), [_ucsc_record(AF=None)]),
        ]
    )
    filtered = FakeSession(
        get_responses=[
            _ucsc_track_response(str(tracks["exome"]), [_ucsc_record(FILTER="FAIL")]),
            _ucsc_track_response(str(tracks["genome"]), [_ucsc_record(FILTER="FAIL")]),
        ]
    )

    missing_result = fetch_ucsc_gnomad_evidence(_candidate(), session=missing_af)  # type: ignore[arg-type]
    filtered_result = fetch_ucsc_gnomad_evidence(_candidate(), session=filtered)  # type: ignore[arg-type]

    assert missing_result["status"] == "invalid_response"
    assert missing_result["global_af"] == {}
    assert filtered_result["status"] == "invalid_response"
    assert filtered_result["failure_reason"] == "filtered_record"


def test_all_verification_failures_preserve_normal_myvariant_value() -> None:
    candidate = _candidate()
    expected_normal_frequency = candidate["population_frequency"]
    session = FakeSession(
        post_responses=[FakeResponse(403, {})],
        get_responses=[
            *_ucsc_no_match_responses(),
            requests.Timeout("Ensembl unavailable"),
            requests.Timeout("Ensembl unavailable"),
        ],
    )

    result = fetch_population_evidence_with_fallback(candidate, session=session)  # type: ignore[arg-type]

    assert result["status"] == "unavailable"
    assert result["provider"] == "gnomAD"
    assert candidate["population_frequency"] == expected_normal_frequency


def test_ucsc_grch37_records_preserve_explicit_population_rows() -> None:
    candidate = _candidate()
    candidate["assembly"] = "GRCh37"
    candidate["variant"] = {"chrom": "19", "pos": 45412079, "ref": "C", "alt": "T"}
    tracks = UCSC_GNOMAD_TRACKS["GRCh37"]["tracks"]
    assert isinstance(tracks, dict)
    record = _ucsc_record(
        chrom="chr19",
        chromStart=45412078,
        chromEnd=45412079,
        AF_afr=0.04,
        AC_afr=4,
        AN_afr=100,
    )
    session = FakeSession(
        get_responses=[
            _ucsc_track_response(str(tracks["exome"]), [record]),
            _ucsc_track_response(str(tracks["genome"]), [record]),
        ]
    )

    result = fetch_ucsc_gnomad_evidence(candidate, session=session)  # type: ignore[arg-type]

    assert result["assembly"] == "GRCh37"
    assert result["dataset"] == "gnomAD v2.1.1"
    assert result["populations"][0]["population"] == "exome:AFR"


def test_ucsc_provenance_survives_the_report_compaction_boundary() -> None:
    result = fetch_ucsc_gnomad_evidence(
        _candidate(),
        session=FakeSession(get_responses=_ucsc_success_responses()),  # type: ignore[arg-type]
    )

    compact = _compact_conditional_enrichment(
        {
            "triggered": True,
            "triggers": ["vus"],
            "population_frequency": result,
            "literature": {},
            "myvariant_fallback": {},
            "warnings": [],
        }
    )["population_frequency"]

    assert compact["provider"] == "UCSC gnomAD"
    assert compact["underlying_dataset"] == "gnomAD"
    assert compact["dataset"] == "gnomAD v4.1"
    assert compact["track_results"][0]["record_identity"]["alt"] == "T"
