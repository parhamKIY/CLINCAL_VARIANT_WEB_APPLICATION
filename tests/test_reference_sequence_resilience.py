"""Stage 2C GRCh38 reference-route resilience contracts."""

from __future__ import annotations

import requests

from backend.input_preprocessing import adapt_annovar_like_record
from backend.annotation import _to_vep_input
from backend.pipeline import run_annovar_like_input_processing
from backend.reference_sequence import (
    compare_reference_sequence_results,
    fetch_grch38_reference_sequence,
    ucsc_interval_from_internal,
)


class _Response:
    def __init__(self, status_code: int, *, text: str = "", payload: object = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, dict(kwargs)))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, _Response)
        return value


def test_red_ensembl_operational_failure_uses_ucsc_fallback() -> None:
    primary = _Session([requests.Timeout("primary unavailable")] * 2)
    fallback = _Session([_Response(200, payload={"dna": "GTGC"})])

    result = fetch_grch38_reference_sequence(
        assembly="GRCh38",
        chrom="13",
        start=100,
        end=103,
        session=primary,
        ucsc_session=fallback,
    )

    assert result["status"] == "success"
    assert result["sequence"] == "GTGC"
    assert result["source"] == "ucsc_hg38_sequence"
    assert result["fallback_used"] is True
    assert len(primary.calls) == 2
    assert fallback.calls[0][1]["params"] == {
        "genome": "hg38",
        "chrom": "chr13",
        "start": 99,
        "end": 103,
    }


def test_red_primary_success_short_circuits_ucsc() -> None:
    primary = _Session([_Response(200, text="GTGC")])
    fallback = _Session([_Response(200, payload={"dna": "AAAA"})])

    result = fetch_grch38_reference_sequence(
        assembly="GRCh38",
        chrom="13",
        start=100,
        end=103,
        session=primary,
        ucsc_session=fallback,
    )

    assert result["status"] == "success"
    assert result["source"] == "ensembl_grch38_sequence"
    assert result["fallback_used"] is False
    assert fallback.calls == []


def test_red_ucsc_coordinate_translation_is_zero_based_half_open() -> None:
    assert ucsc_interval_from_internal(chrom="1", start=1, end=1) == (
        "chr1",
        0,
        1,
    )
    assert ucsc_interval_from_internal(chrom="X", start=200, end=203) == (
        "chrX",
        199,
        203,
    )
    assert ucsc_interval_from_internal(chrom="X", start=199, end=199) == (
        "chrX",
        198,
        199,
    )


def test_red_valid_primary_mismatch_never_queries_ucsc() -> None:
    primary = _Session([_Response(200, text="AAAA")])
    fallback = _Session([_Response(200, payload={"dna": "GTGC"})])

    reference = fetch_grch38_reference_sequence(
        assembly="GRCh38",
        chrom="13",
        start=100,
        end=103,
        session=primary,
        ucsc_session=fallback,
    )
    adapted = adapt_annovar_like_record(
        {
            "chrom": "13",
            "start": 100,
            "end": 103,
            "ref": "GTGC",
            "alt": 0,
        },
        reference_fetcher=lambda **_: reference,
    )

    assert adapted["failure_reason"] == "REFERENCE_MISMATCH"
    assert fallback.calls == []


def test_red_conflicting_verified_reference_context_fails_closed() -> None:
    ensembl = {
        "status": "success",
        "assembly": "GRCh38",
        "chrom": "13",
        "start": 100,
        "end": 103,
        "sequence": "GTGC",
        "source": "ensembl_grch38_sequence",
        "fallback_used": False,
        "provider_attempts": [],
    }
    ucsc = {**ensembl, "sequence": "AAAA", "source": "ucsc_hg38_sequence"}

    conflict = compare_reference_sequence_results(ensembl, ucsc)

    assert conflict["status"] == "unavailable"
    assert conflict["failure_reason"] == "REFERENCE_SOURCE_CONFLICT"


def test_malformed_ensembl_response_uses_ucsc_and_retains_attempt_provenance() -> None:
    primary = _Session([_Response(200, text="NNNN")])
    fallback = _Session([_Response(200, payload={"dna": "GTGC"})])

    result = fetch_grch38_reference_sequence(
        assembly="GRCh38",
        chrom="chr13",
        start=100,
        end=103,
        session=primary,
        ucsc_session=fallback,
    )

    assert result["status"] == "success"
    assert result["fallback_used"] is True
    assert [item["status"] for item in result["provider_attempts"]] == [
        "response_invalid",
        "success",
    ]
    assert result["provider_attempts"][1]["request_provenance"] == (
        "ucsc_hg38_start_0based_end_1based"
    )


def test_both_reference_routes_unavailable_remain_explicitly_operational() -> None:
    primary = _Session([requests.Timeout("primary unavailable")] * 2)
    fallback = _Session([requests.Timeout("fallback unavailable")] * 2)

    result = fetch_grch38_reference_sequence(
        assembly="GRCh38",
        chrom="13",
        start=100,
        end=103,
        session=primary,
        ucsc_session=fallback,
    )

    assert result["status"] == "unavailable"
    assert result["failure_reason"] == "REFERENCE_LOOKUP_UNAVAILABLE"
    assert result["fallback_used"] is True
    assert len(result["provider_attempts"]) == 2


def _fallback_fetcher(
    primary: _Session,
    fallback: _Session,
):
    def fetch(**kwargs: object) -> dict[str, object]:
        return fetch_grch38_reference_sequence(
            assembly=str(kwargs["assembly"]),
            chrom=str(kwargs["chrom"]),
            start=int(kwargs["start"]),
            end=int(kwargs["end"]),
            session=primary,
            ucsc_session=fallback,
        )

    return fetch


def test_ucsc_fallback_normalizes_single_and_multi_base_deletions_like_primary() -> None:
    primary = _Session([requests.Timeout("unavailable")] * 8)
    fallback = _Session(
        [
            _Response(200, payload={"dna": "GTGC"}),
            _Response(200, payload={"dna": "A"}),
            _Response(200, payload={"dna": "C"}),
            _Response(200, payload={"dna": "A"}),
        ]
    )
    fetcher = _fallback_fetcher(primary, fallback)

    multi = adapt_annovar_like_record(
        {"chrom": "13", "start": 100, "end": 103, "ref": "GTGC", "alt": 0},
        reference_fetcher=fetcher,
    )
    single = adapt_annovar_like_record(
        {"chrom": "13", "start": 200, "end": 200, "ref": "C", "alt": 0},
        reference_fetcher=fetcher,
    )

    assert multi["status"] == single["status"] == "NORMALIZED_AND_ACCEPTED"
    assert multi["canonical_variant"] == {
        "chrom": "13", "pos": 99, "ref": "AGTGC", "alt": "A", "qual": None, "filter": None
    }
    assert single["canonical_variant"] == {
        "chrom": "13", "pos": 199, "ref": "AC", "alt": "A", "qual": None, "filter": None
    }
    assert "ucsc_fallback" in multi["source_provenance"]["reference_verification"]


def test_equivalent_primary_and_fallback_sequences_emit_identical_cpra() -> None:
    source = {"chrom": "13", "start": 100, "end": 103, "ref": "GTGC", "alt": 0}

    primary = adapt_annovar_like_record(
        source,
        reference_fetcher=lambda **kwargs: {
            "status": "success",
            "assembly": kwargs["assembly"],
            "chrom": kwargs["chrom"],
            "start": kwargs["start"],
            "end": kwargs["end"],
            "sequence": "GTGC" if kwargs["start"] == 100 else "A",
            "source": "ensembl_grch38_sequence",
        },
    )
    fallback = adapt_annovar_like_record(
        source,
        reference_fetcher=lambda **kwargs: {
            "status": "success",
            "assembly": kwargs["assembly"],
            "chrom": kwargs["chrom"],
            "start": kwargs["start"],
            "end": kwargs["end"],
            "sequence": "GTGC" if kwargs["start"] == 100 else "A",
            "source": "ucsc_hg38_sequence",
            "fallback_used": True,
        },
    )

    assert primary["canonical_variant"] == fallback["canonical_variant"]


def test_ucsc_fallback_normalizes_insertion_and_preserves_canonical_handoff() -> None:
    primary = _Session([requests.Timeout("unavailable")] * 2)
    fallback = _Session([_Response(200, payload={"dna": "G"})])
    adapted = adapt_annovar_like_record(
        {"chrom": "X", "start": 200, "end": 200, "ref": 0, "alt": "ACT"},
        reference_fetcher=_fallback_fetcher(primary, fallback),
    )

    assert adapted["status"] == "NORMALIZED_AND_ACCEPTED"
    assert adapted["canonical_variant"] == {
        "chrom": "X", "pos": 200, "ref": "G", "alt": "GACT", "qual": None, "filter": None
    }
    assert _to_vep_input("input-000", adapted["canonical_variant"]) == (
        "X 200 input-000 G GACT . . ."
    )


def test_fallback_reference_mismatch_does_not_establish_identity() -> None:
    primary = _Session([requests.Timeout("unavailable")] * 2)
    fallback = _Session([_Response(200, payload={"dna": "AAAA"})])
    adapted = adapt_annovar_like_record(
        {"chrom": "13", "start": 100, "end": 103, "ref": "GTGC", "alt": 0},
        reference_fetcher=_fallback_fetcher(primary, fallback),
    )

    assert adapted["status"] == "IDENTITY_UNRESOLVED"
    assert adapted["failure_reason"] == "REFERENCE_MISMATCH"


def test_analysis_scoped_reference_cache_deduplicates_identical_intervals() -> None:
    calls: list[tuple[str, int, int]] = []

    def fetch(*, assembly: str, chrom: str, start: int, end: int) -> dict[str, object]:
        calls.append((chrom, start, end))
        sequence = {("13", 99, 99): "A", ("13", 100, 103): "GTGC"}.get(
            (chrom, start, end)
        )
        return {
            "status": "success" if sequence else "unavailable",
            "assembly": assembly,
            "chrom": chrom,
            "start": start,
            "end": end,
            "sequence": sequence,
            "source": "ensembl_grch38_sequence",
        }

    result = run_annovar_like_input_processing(
        [
            {"chrom": "13", "start": 100, "end": 103, "ref": "GTGC", "alt": 0},
            {"chrom": "13", "start": 100, "end": 103, "ref": "GTGC", "alt": 0},
        ],
        phenotypes=[],
        reference_fetcher=fetch,
    )

    assert result["variant_count"] == 2
    assert calls == [("13", 100, 103), ("13", 99, 99)]
