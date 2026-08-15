"""Normal population-frequency fallback contract tests."""

from __future__ import annotations

from typing import Any

from backend.annotation import (
    _apply_ucsc_normal_population_frequency_fallback,
    _base_annotation,
    _standardize_myvariant_response,
)
from backend.conditional_enrichment import UCSC_GNOMAD_TRACKS
from backend.report import build_evidence_object
from config import settings


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeUCSCSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        assert url == f"{settings.UCSC_GNOMAD_BASE_URL}/getData/track"
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _annotation() -> dict[str, Any]:
    return _base_annotation(
        {"chrom": "2", "pos": 166848215, "ref": "C", "alt": "T"},
        status="success",
    )


def _track_response(
    track: str,
    *,
    af: float = 0.00012,
    alt: str = "T",
) -> FakeResponse:
    return FakeResponse(
        {
            track: [
                {
                    "chrom": "chr2",
                    "chromStart": 166848214,
                    "chromEnd": 166848215,
                    "ref": "C",
                    "alt": alt,
                    "AF": af,
                    "AC": 12,
                    "AN": 100_000,
                    "FILTER": "PASS",
                }
            ]
        }
    )


def _ucsc_responses(*, alt: str = "T") -> list[FakeResponse]:
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    exome = tracks["exome"]
    genome = tracks["genome"]
    assert isinstance(exome, str) and isinstance(genome, str)
    return [
        _track_response(exome, af=0.00012, alt=alt),
        _track_response(genome, af=0.00008, alt=alt),
    ]


def test_valid_myvariant_frequency_skips_ucsc_normal_fallback() -> None:
    annotation = _annotation()
    _standardize_myvariant_response(
        annotation,
        {"gnomad_exome": {"af": 0.001}},
        "chr2:g.166848215C>T",
    )
    session = FakeUCSCSession(_ucsc_responses())

    _apply_ucsc_normal_population_frequency_fallback(annotation, session)  # type: ignore[arg-type]

    assert annotation["population_frequency"] == 0.001
    assert annotation["population_frequency_provenance"]["provider"] == "MyVariant.info"
    assert session.calls == []


def test_ucsc_supplies_normal_frequency_after_no_usable_myvariant_frequency() -> None:
    annotation = _annotation()
    annotation["sources"]["myvariant"].update({"status": "not_found"})
    session = FakeUCSCSession(_ucsc_responses())

    _apply_ucsc_normal_population_frequency_fallback(annotation, session)  # type: ignore[arg-type]

    provenance = annotation["population_frequency_provenance"]
    assert annotation["population_frequency"] == 0.00012
    assert provenance["provider"] == "UCSC gnomAD"
    assert provenance["operational_provider"] == "ucsc_gnomad"
    assert provenance["underlying_dataset"] == "gnomAD"
    assert provenance["release"] == "gnomAD v4.1"
    assert provenance["available_global_af"] == {
        "exome": 0.00012,
        "genome": 0.00008,
    }
    assert provenance["fallback_used"] is False
    assert provenance["continuation_reason"] is not None
    assert len(session.calls) == 2
    evidence = build_evidence_object(annotation)
    assert evidence["annotations"]["population"][
        "selected_frequency_source"
    ]["operational_provider"] == "ucsc_gnomad"


def test_ucsc_operational_fallback_is_explicit_after_myvariant_failure() -> None:
    annotation = _annotation()
    annotation["sources"]["myvariant"].update(
        {"status": "error", "primary_failure": "server_error"}
    )
    session = FakeUCSCSession(_ucsc_responses())

    _apply_ucsc_normal_population_frequency_fallback(annotation, session)  # type: ignore[arg-type]

    provenance = annotation["population_frequency_provenance"]
    assert provenance["fallback_used"] is True
    assert provenance["primary_failure"] == "server_error"
    assert provenance["continuation_reason"] is None


def test_ucsc_wrong_alt_cannot_supply_normal_frequency() -> None:
    annotation = _annotation()
    annotation["sources"]["myvariant"].update({"status": "not_found"})
    session = FakeUCSCSession(_ucsc_responses(alt="G"))

    _apply_ucsc_normal_population_frequency_fallback(annotation, session)  # type: ignore[arg-type]

    assert annotation["population_frequency"] is None
    assert annotation["population_frequency_provenance"]["status"] == "no_match"
