"""Manually check configured provider DNS and HTTP reachability."""

from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable, Protocol, Sequence
from urllib.parse import urlsplit

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings


DEFAULT_TIMEOUT_SECONDS = 8.0
CSV_FIELDS = (
    "provider",
    "dns_status",
    "http_status",
    "latency_ms",
    "failure_category",
)


@dataclass(frozen=True)
class ProviderTarget:
    """One configured provider endpoint."""

    provider: str
    url: str


@dataclass(frozen=True)
class ReachabilityResult:
    """Bounded, non-clinical connectivity result."""

    provider: str
    dns_status: str
    http_status: int | None
    latency_ms: float | None
    failure_category: str


class HeadSession(Protocol):
    def head(self, url: str, **kwargs: object) -> requests.Response: ...


def configured_targets() -> tuple[ProviderTarget, ...]:
    """Return bioinformatics providers used by the current application."""

    return (
        ProviderTarget("vep", settings.VEP_BASE_URL),
        ProviderTarget(
            "variantvalidator",
            settings.VARIANTVALIDATOR_BASE_URL,
        ),
        ProviderTarget("genebe", settings.GENEBE_BASE_URL),
        ProviderTarget("myvariant", settings.MYVARIANT_BASE_URL),
        ProviderTarget("clinvar", settings.CLINVAR_BASE_URL),
        ProviderTarget("clingen", settings.CLINGEN_BASE_URL),
        ProviderTarget("cspec", settings.CSPEC_BASE_URL),
        ProviderTarget("phen2gene", settings.PHEN2GENE_BASE_URL),
        ProviderTarget("mydisease", settings.MYDISEASE_BASE_URL),
        ProviderTarget("gnomad", settings.GNOMAD_BASE_URL),
        ProviderTarget("ucsc_gnomad", settings.UCSC_GNOMAD_BASE_URL),
        ProviderTarget(
            "ensembl_variation",
            settings.ENSEMBL_VARIATION_BASE_URL,
        ),
        ProviderTarget("litvar2", settings.LITVAR_BASE_URL),
        ProviderTarget("europe_pmc", settings.EUROPE_PMC_BASE_URL),
        ProviderTarget("pubmed", settings.PUBMED_BASE_URL),
    )


def _http_failure_category(status_code: int) -> str:
    if status_code in {404, 405}:
        return "none"
    if status_code == 403:
        return "forbidden"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "none"


def _request_failure_category(error: requests.RequestException) -> str:
    if isinstance(error, requests.Timeout):
        return "timeout"
    if isinstance(error, requests.exceptions.SSLError):
        return "tls_failure"
    if isinstance(error, requests.ConnectionError):
        return "connection_error"
    return "request_error"


def probe_provider(
    target: ProviderTarget,
    *,
    session: HeadSession,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    resolver: Callable[..., object] = socket.getaddrinfo,
    clock: Callable[[], float] = perf_counter,
) -> ReachabilityResult:
    """Resolve and issue one bounded, non-mutating request to a provider."""

    hostname = urlsplit(target.url).hostname
    if not hostname:
        return ReachabilityResult(
            provider=target.provider,
            dns_status="invalid_host",
            http_status=None,
            latency_ms=None,
            failure_category="configuration_error",
        )

    try:
        resolver(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        return ReachabilityResult(
            provider=target.provider,
            dns_status="failed",
            http_status=None,
            latency_ms=None,
            failure_category="dns_failure",
        )

    started_at = clock()
    try:
        response = session.head(
            target.url,
            allow_redirects=True,
            timeout=(min(timeout, 3.0), timeout),
        )
    except requests.RequestException as exc:
        elapsed = round(max(0.0, clock() - started_at) * 1_000, 1)
        return ReachabilityResult(
            provider=target.provider,
            dns_status="resolved",
            http_status=None,
            latency_ms=elapsed,
            failure_category=_request_failure_category(exc),
        )

    elapsed = round(max(0.0, clock() - started_at) * 1_000, 1)
    try:
        status_code = response.status_code
    finally:
        response.close()
    return ReachabilityResult(
        provider=target.provider,
        dns_status="resolved",
        http_status=status_code,
        latency_ms=elapsed,
        failure_category=_http_failure_category(status_code),
    )


def run_checks(
    targets: Iterable[ProviderTarget],
    *,
    session: HeadSession,
    timeout: float,
) -> list[ReachabilityResult]:
    return [
        probe_provider(target, session=session, timeout=timeout)
        for target in targets
    ]


def write_json(
    path: Path,
    results: Sequence[ReachabilityResult],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "checks": [asdict(result) for result in results],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def write_csv(
    path: Path,
    results: Sequence[ReachabilityResult],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(asdict(result) for result in results)


def _print_results(results: Sequence[ReachabilityResult]) -> None:
    print("provider\tdns\thttp\tlatency_ms\tfailure_category")
    for result in results:
        http_status = (
            str(result.http_status)
            if result.http_status is not None
            else "-"
        )
        latency = (
            f"{result.latency_ms:.1f}"
            if result.latency_ms is not None
            else "-"
        )
        print(
            f"{result.provider}\t{result.dns_status}\t{http_status}\t"
            f"{latency}\t{result.failure_category}"
        )


def parse_arguments(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    available = tuple(target.provider for target in configured_targets())
    parser = argparse.ArgumentParser(
        description=(
            "Run manual DNS and HTTP reachability checks for configured "
            "bioinformatics providers. This command is network-dependent."
        )
    )
    parser.add_argument(
        "--provider",
        action="append",
        choices=available,
        help="Check only this provider; repeat to select multiple providers.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-provider HTTP read timeout in seconds (default: 8).",
    )
    parser.add_argument("--json", type=Path, help="Optional JSON output path.")
    parser.add_argument("--csv", type=Path, help="Optional CSV output path.")
    arguments = parser.parse_args(argv)
    if arguments.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    targets = configured_targets()
    if arguments.provider:
        selected = set(arguments.provider)
        targets = tuple(
            target for target in targets if target.provider in selected
        )

    with requests.Session() as session:
        session.headers.update(
            {"User-Agent": "clinical-variant-provider-reachability/1.0"}
        )
        results = run_checks(
            targets,
            session=session,
            timeout=arguments.timeout,
        )

    _print_results(results)
    if arguments.json is not None:
        write_json(arguments.json, results)
    if arguments.csv is not None:
        write_csv(arguments.csv, results)
    return int(any(result.failure_category != "none" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
