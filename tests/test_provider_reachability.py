"""Offline tests for the manual provider reachability utility."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import requests

from tools.provider_reachability import (
    ProviderTarget,
    ReachabilityResult,
    configured_targets,
    probe_provider,
    write_csv,
    write_json,
)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def head(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        assert isinstance(self.response, FakeResponse)
        return self.response


def _resolved(*_args: object, **_kwargs: object) -> object:
    return [(None, None, None, None, ("127.0.0.1", 443))]


def _clock(values: list[float]):
    iterator = iter(values)
    return lambda: next(iterator)


class TestProviderReachability:
    def test_configured_target_matrix_is_complete(self) -> None:
        assert {target.provider for target in configured_targets()} == {
            "vep",
            "variantvalidator",
            "genebe",
            "myvariant",
            "clinvar",
            "clingen",
            "cspec",
            "phen2gene",
            "mydisease",
            "gnomad",
            "ensembl_variation",
            "litvar2",
            "europe_pmc",
            "pubmed",
        }

    def test_success_records_dns_http_and_latency(self) -> None:
        response = FakeResponse(204)
        session = FakeSession(response)

        result = probe_provider(
            ProviderTarget("example", "https://example.org/api"),
            session=session,
            timeout=8,
            resolver=_resolved,
            clock=_clock([10.0, 10.125]),
        )

        assert result == ReachabilityResult(
            provider="example",
            dns_status="resolved",
            http_status=204,
            latency_ms=125.0,
            failure_category="none",
        )
        assert session.calls == [
            (
                "https://example.org/api",
                {"allow_redirects": True, "timeout": (3.0, 8)},
            )
        ]
        assert response.closed is True

    def test_dns_failure_skips_http(self) -> None:
        session = FakeSession(FakeResponse(200))

        def failed_resolver(*_args: object, **_kwargs: object) -> object:
            raise OSError("unresolvable")

        result = probe_provider(
            ProviderTarget("example", "https://unresolvable.invalid"),
            session=session,
            resolver=failed_resolver,
        )

        assert result.dns_status == "failed"
        assert result.http_status is None
        assert result.latency_ms is None
        assert result.failure_category == "dns_failure"
        assert session.calls == []

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (FakeResponse(403), "forbidden"),
            (FakeResponse(429), "rate_limited"),
            (FakeResponse(503), "server_error"),
            (FakeResponse(404), "none"),
            (FakeResponse(405), "none"),
            (requests.Timeout("private detail"), "timeout"),
            (requests.exceptions.SSLError("private detail"), "tls_failure"),
            (requests.ConnectionError("private detail"), "connection_error"),
        ],
    )
    def test_failures_are_safely_classified(
        self,
        response: object,
        expected: str,
    ) -> None:
        result = probe_provider(
            ProviderTarget("example", "https://example.org"),
            session=FakeSession(response),
            resolver=_resolved,
            clock=_clock([5.0, 5.01]),
        )

        assert result.failure_category == expected
        assert "private" not in repr(result)

    def test_json_and_csv_exports_are_machine_readable(
        self,
        tmp_path: Path,
    ) -> None:
        results = [
            ReachabilityResult(
                provider="example",
                dns_status="resolved",
                http_status=200,
                latency_ms=12.5,
                failure_category="none",
            )
        ]
        json_path = tmp_path / "nested" / "reachability.json"
        csv_path = tmp_path / "nested" / "reachability.csv"

        write_json(json_path, results)
        write_csv(csv_path, results)

        json_payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert json_payload["checks"] == [
            {
                "provider": "example",
                "dns_status": "resolved",
                "http_status": 200,
                "latency_ms": 12.5,
                "failure_category": "none",
            }
        ]
        with csv_path.open(encoding="utf-8", newline="") as csv_file:
            assert list(csv.DictReader(csv_file)) == [
                {
                    "provider": "example",
                    "dns_status": "resolved",
                    "http_status": "200",
                    "latency_ms": "12.5",
                    "failure_category": "none",
                }
            ]
