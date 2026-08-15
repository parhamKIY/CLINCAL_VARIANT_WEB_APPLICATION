"""Provider-readiness catalog shared by pre-analysis health checks.

The catalog describes operational connectivity only.  It deliberately does
not change evidence collection, fallback activation, or clinical provenance.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Literal, Protocol, Sequence
from urllib.parse import urlsplit

import requests

from config import settings


ProbeKind = Literal["transport", "authenticated"]
CredentialMode = Literal["none", "optional", "required"]
ReadinessState = Literal["reachable", "unreachable"]
DEFAULT_READINESS_TIMEOUT_SECONDS = 5.0
MAX_READINESS_WORKERS = 5


@dataclass(frozen=True)
class ProviderReadinessTarget:
    """One configured source shown by the future pre-analysis readiness UI."""

    provider: str
    label: str
    url: str
    capabilities: tuple[str, ...]
    fallback_for: tuple[str, ...] = ()
    credential_mode: CredentialMode = "none"
    probe_kind: ProbeKind = "transport"

    def __post_init__(self) -> None:
        if not self.provider or not self.provider.isidentifier():
            raise ValueError("Provider identifier must be a Python identifier.")
        if not self.label.strip() or not self.capabilities:
            raise ValueError("Provider label and capabilities are required.")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Provider readiness targets require HTTPS URLs.")
        if self.credential_mode not in {"none", "optional", "required"}:
            raise ValueError("Unsupported provider credential mode.")
        if self.probe_kind not in {"transport", "authenticated"}:
            raise ValueError("Unsupported provider probe kind.")


@dataclass(frozen=True)
class ProviderReadinessResult:
    """Secret-free outcome of one bounded, pre-analysis provider probe."""

    provider: str
    state: ReadinessState
    dns_status: str
    http_status: int | None
    latency_ms: float | None
    failure_category: str
    probe_kind: ProbeKind


class ProviderReadinessSession(Protocol):
    """Small HTTP surface needed for deterministic readiness tests."""

    def head(self, url: str, **kwargs: object) -> requests.Response: ...

    def get(self, url: str, **kwargs: object) -> requests.Response: ...


FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    "variant_annotation": ("vep", "variantvalidator"),
    "variant_context": ("myvariant", "ensembl_variation"),
    "clinvar_evidence": ("clinvar", "myvariant"),
    "cspec_context": ("cspec", "local_cspec_cache"),
    "phenotype_gene_context": ("phen2gene", "local_hpo_gene"),
    "disease_hpo_context": ("mydisease", "local_hpo_disease"),
    "population_frequency": ("gnomad", "ensembl_variation"),
    "variant_literature": ("litvar2", "europe_pmc", "pubmed"),
    "gene_disease_literature": ("europe_pmc", "pubmed"),
    "variant_interpretation": ("llm", "llm_fallback_model"),
}


def configured_provider_readiness_targets() -> tuple[ProviderReadinessTarget, ...]:
    """Return all network providers used by the active analysis workflow.

    Local CSpec and HPO fallbacks intentionally do not appear: they have no
    network endpoint to probe.  LLM health is marked authenticated because a
    future capability probe must validate the configured key without logging it.
    """

    return (
        ProviderReadinessTarget(
            "vep",
            "Ensembl VEP",
            settings.VEP_BASE_URL,
            ("variant_annotation",),
        ),
        ProviderReadinessTarget(
            "variantvalidator",
            "VariantValidator",
            settings.VARIANTVALIDATOR_BASE_URL,
            ("variant_annotation",),
            ("variant_annotation",),
        ),
        ProviderReadinessTarget(
            "genebe",
            "GeneBe",
            settings.GENEBE_BASE_URL,
            ("classification_context",),
            credential_mode="optional",
        ),
        ProviderReadinessTarget(
            "myvariant",
            "MyVariant.info",
            settings.MYVARIANT_BASE_URL,
            ("variant_context", "clinvar_evidence"),
            ("clinvar_evidence",),
        ),
        ProviderReadinessTarget(
            "clinvar",
            "NCBI ClinVar",
            settings.CLINVAR_BASE_URL,
            ("clinvar_evidence",),
        ),
        ProviderReadinessTarget(
            "clingen",
            "UCSC GenCC track",
            settings.CLINGEN_BASE_URL,
            ("gene_disease_validity",),
        ),
        ProviderReadinessTarget(
            "cspec",
            "ClinGen CSpec Registry",
            settings.CSPEC_BASE_URL,
            ("cspec_context",),
        ),
        ProviderReadinessTarget(
            "phen2gene",
            "Phen2Gene",
            settings.PHEN2GENE_BASE_URL,
            ("phenotype_gene_context",),
        ),
        ProviderReadinessTarget(
            "mydisease",
            "MyDisease.info",
            settings.MYDISEASE_BASE_URL,
            ("disease_hpo_context",),
        ),
        ProviderReadinessTarget(
            "gnomad",
            "gnomAD",
            settings.GNOMAD_BASE_URL,
            ("population_frequency",),
        ),
        ProviderReadinessTarget(
            "ensembl_variation",
            "Ensembl REST Variation",
            settings.ENSEMBL_VARIATION_BASE_URL,
            ("variant_context", "population_frequency"),
            ("variant_context", "population_frequency"),
        ),
        ProviderReadinessTarget(
            "litvar2",
            "LitVar2",
            settings.LITVAR_BASE_URL,
            ("variant_literature",),
        ),
        ProviderReadinessTarget(
            "europe_pmc",
            "Europe PMC",
            settings.EUROPE_PMC_BASE_URL,
            ("variant_literature", "gene_disease_literature"),
            ("variant_literature",),
        ),
        ProviderReadinessTarget(
            "pubmed",
            "PubMed",
            settings.PUBMED_BASE_URL,
            ("variant_literature", "gene_disease_literature"),
            ("variant_literature", "gene_disease_literature"),
        ),
        ProviderReadinessTarget(
            "llm",
            "Configured interpretation endpoint",
            settings.LLM_BASE_URL,
            ("phenotype_extraction", "variant_interpretation"),
            credential_mode="required",
            probe_kind="authenticated",
        ),
    )


def provider_readiness_target_map() -> dict[str, ProviderReadinessTarget]:
    """Return the catalog keyed by stable provider identifier."""

    targets = configured_provider_readiness_targets()
    result = {target.provider: target for target in targets}
    if len(result) != len(targets):
        raise ValueError("Provider readiness identifiers must be unique.")
    return result


def _http_failure_category(
    status_code: int,
    *,
    probe_kind: ProbeKind,
) -> str:
    if 200 <= status_code < 400:
        return "none"
    if probe_kind == "transport" and status_code in {404, 405}:
        return "none"
    if status_code == 401:
        return "configuration_error"
    if status_code == 403:
        return "forbidden"
    if status_code == 408:
        return "timeout"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "client_error"


def _request_failure_category(error: requests.RequestException) -> str:
    if isinstance(error, requests.Timeout):
        return "timeout"
    if isinstance(error, requests.exceptions.SSLError):
        return "tls_failure"
    if isinstance(error, requests.ConnectionError):
        return "connection_error"
    return "request_error"


def _authenticated_probe_url(target: ProviderReadinessTarget) -> str:
    """Return the OpenAI-compatible, key-validating discovery endpoint."""

    if target.provider != "llm":
        raise ValueError("No authenticated readiness probe is defined.")
    return f"{target.url}/models"


def probe_provider_readiness(
    target: ProviderReadinessTarget,
    *,
    session: ProviderReadinessSession,
    timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    resolver: Callable[..., object] = socket.getaddrinfo,
    clock: Callable[[], float] = perf_counter,
) -> ProviderReadinessResult:
    """Probe one endpoint without submitting variants or clinical text.

    Transport probes use one HTTP HEAD request. The configured LLM endpoint
    uses its OpenAI-compatible model-discovery path with the configured API key;
    neither credentials nor response content are retained in the result.
    """

    if not 0 < timeout <= 20:
        raise ValueError("Readiness timeout must be greater than 0 and at most 20.")
    hostname = urlsplit(target.url).hostname
    if not hostname:
        return ProviderReadinessResult(
            provider=target.provider,
            state="unreachable",
            dns_status="invalid_host",
            http_status=None,
            latency_ms=None,
            failure_category="configuration_error",
            probe_kind=target.probe_kind,
        )

    try:
        resolver(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        return ProviderReadinessResult(
            provider=target.provider,
            state="unreachable",
            dns_status="failed",
            http_status=None,
            latency_ms=None,
            failure_category="dns_failure",
            probe_kind=target.probe_kind,
        )

    started_at = clock()
    try:
        if target.probe_kind == "authenticated":
            response = session.get(
                _authenticated_probe_url(target),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "User-Agent": "clinical-variant-provider-readiness/1.0",
                },
                timeout=(min(timeout, 3.0), timeout),
            )
        else:
            response = session.head(
                target.url,
                headers={
                    "User-Agent": "clinical-variant-provider-readiness/1.0",
                },
                allow_redirects=True,
                timeout=(min(timeout, 3.0), timeout),
            )
    except requests.RequestException as exc:
        elapsed = round(max(0.0, clock() - started_at) * 1_000, 1)
        return ProviderReadinessResult(
            provider=target.provider,
            state="unreachable",
            dns_status="resolved",
            http_status=None,
            latency_ms=elapsed,
            failure_category=_request_failure_category(exc),
            probe_kind=target.probe_kind,
        )

    elapsed = round(max(0.0, clock() - started_at) * 1_000, 1)
    try:
        status_code = response.status_code
    finally:
        response.close()
    failure_category = _http_failure_category(
        status_code,
        probe_kind=target.probe_kind,
    )
    return ProviderReadinessResult(
        provider=target.provider,
        state="reachable" if failure_category == "none" else "unreachable",
        dns_status="resolved",
        http_status=status_code,
        latency_ms=elapsed,
        failure_category=failure_category,
        probe_kind=target.probe_kind,
    )


def _probe_with_new_session(
    target: ProviderReadinessTarget,
    *,
    timeout: float,
) -> ProviderReadinessResult:
    with requests.Session() as session:
        return probe_provider_readiness(
            target,
            session=session,
            timeout=timeout,
        )


def run_provider_readiness_checks(
    targets: Sequence[ProviderReadinessTarget] | None = None,
    *,
    timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    max_workers: int = MAX_READINESS_WORKERS,
) -> tuple[ProviderReadinessResult, ...]:
    """Run bounded connectivity checks concurrently in stable catalog order."""

    if not 0 < timeout <= 20:
        raise ValueError("Readiness timeout must be greater than 0 and at most 20.")
    if not 1 <= max_workers <= MAX_READINESS_WORKERS:
        raise ValueError(
            f"Readiness workers must be from 1 to {MAX_READINESS_WORKERS}."
        )
    selected_targets = (
        tuple(targets)
        if targets is not None
        else configured_provider_readiness_targets()
    )
    if not selected_targets:
        return ()

    worker_count = min(max_workers, len(selected_targets))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(_probe_with_new_session, target, timeout=timeout)
            for target in selected_targets
        ]
        return tuple(future.result() for future in futures)


__all__ = [
    "CredentialMode",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "FALLBACK_CHAINS",
    "MAX_READINESS_WORKERS",
    "ProbeKind",
    "ProviderReadinessResult",
    "ProviderReadinessSession",
    "ProviderReadinessTarget",
    "ReadinessState",
    "configured_provider_readiness_targets",
    "probe_provider_readiness",
    "provider_readiness_target_map",
    "run_provider_readiness_checks",
]
