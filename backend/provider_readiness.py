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
RecommendationState = Literal[
    "preferred",
    "awaiting_check",
    "runtime_only",
    "unavailable",
]
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


@dataclass(frozen=True)
class ProviderReadinessRecommendation:
    """One quality-first, non-clinical source recommendation."""

    capability: str
    provider: str | None
    state: RecommendationState
    reason: str
    latency_ms: float | None


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
    "population_frequency": (
        "gnomad",
        "ucsc_gnomad",
        "ensembl_variation",
    ),
    "variant_literature": ("litvar2", "europe_pmc", "pubmed"),
    "gene_disease_literature": ("europe_pmc", "pubmed"),
    "variant_interpretation": ("llm", "llm_fallback_model"),
}
QUALITY_SOURCE_CHAINS: dict[str, tuple[str, ...]] = {
    "classification_context": ("genebe",),
    "variant_annotation": FALLBACK_CHAINS["variant_annotation"],
    "variant_context": FALLBACK_CHAINS["variant_context"],
    "clinvar_evidence": FALLBACK_CHAINS["clinvar_evidence"],
    "cspec_context": FALLBACK_CHAINS["cspec_context"],
    "gene_disease_validity": ("clingen",),
    "normal_population_frequency": ("myvariant", "ucsc_gnomad"),
    "phenotype_gene_context": FALLBACK_CHAINS["phenotype_gene_context"],
    "disease_hpo_context": FALLBACK_CHAINS["disease_hpo_context"],
    "population_frequency": FALLBACK_CHAINS["population_frequency"],
    "variant_literature": FALLBACK_CHAINS["variant_literature"],
    "gene_disease_literature": FALLBACK_CHAINS["gene_disease_literature"],
    "phenotype_extraction": ("llm",),
    "variant_interpretation": FALLBACK_CHAINS["variant_interpretation"],
}
LOCAL_FALLBACK_LABELS = {
    "local_cspec_cache": "Local CSpec last-known-good cache",
    "local_hpo_disease": "Local HPO disease annotations",
    "local_hpo_gene": "Local direct HPO-gene overlap",
    "llm_fallback_model": "Configured interpretation fallback model",
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
            (
                "variant_context",
                "clinvar_evidence",
                "normal_population_frequency",
            ),
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
            "ucsc_gnomad",
            "UCSC gnomAD track",
            settings.UCSC_GNOMAD_BASE_URL,
            ("normal_population_frequency", "population_frequency"),
            ("normal_population_frequency", "population_frequency"),
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


def _quality_tiers(capability: str) -> tuple[tuple[str, ...], ...]:
    """Return quality tiers, preserving the existing fallback order."""

    try:
        return tuple(
            (provider,) for provider in QUALITY_SOURCE_CHAINS[capability]
        )
    except KeyError as exc:
        raise ValueError(f"Unsupported readiness capability: {capability}.") from exc


def _lowest_latency(
    providers: Sequence[str],
    results: dict[str, ProviderReadinessResult],
) -> str:
    """Return the quickest provider inside one equal-quality tier."""

    return min(
        providers,
        key=lambda provider: (
            results[provider].latency_ms is None,
            results[provider].latency_ms or float("inf"),
            provider,
        ),
    )


def recommend_provider_readiness(
    capability: str,
    results: Sequence[ProviderReadinessResult],
    *,
    quality_tiers: Sequence[Sequence[str]] | None = None,
) -> ProviderReadinessRecommendation:
    """Choose a readiness recommendation by quality tier then latency.

    A readiness probe can only recommend a source. It cannot suppress an
    evidence-provider call or claim fallback provenance; those decisions remain
    inside the provider-specific pipeline after an operational failure.
    """

    targets = provider_readiness_target_map()
    result_by_provider = {result.provider: result for result in results}
    tiers = (
        tuple(tuple(tier) for tier in quality_tiers)
        if quality_tiers is not None
        else _quality_tiers(capability)
    )
    if not tiers or any(not tier for tier in tiers):
        raise ValueError("Readiness quality tiers must contain providers.")

    for tier_index, tier in enumerate(tiers):
        network_providers = tuple(
            provider for provider in tier if provider in targets
        )
        reachable = tuple(
            provider
            for provider in network_providers
            if result_by_provider.get(provider) is not None
            and result_by_provider[provider].state == "reachable"
        )
        if reachable:
            selected = _lowest_latency(reachable, result_by_provider)
            result = result_by_provider[selected]
            reason = (
                "Highest evidence-quality reachable source."
                if tier_index == 0
                else "Higher-quality source is unreachable; selected the "
                "lowest-latency reachable alternative."
            )
            return ProviderReadinessRecommendation(
                capability=capability,
                provider=selected,
                state="preferred",
                reason=reason,
                latency_ms=result.latency_ms,
            )

        unchecked = tuple(
            provider
            for provider in network_providers
            if provider not in result_by_provider
        )
        if unchecked:
            return ProviderReadinessRecommendation(
                capability=capability,
                provider=unchecked[0],
                state="awaiting_check",
                reason=(
                    "This higher-quality source has not been checked yet."
                ),
                latency_ms=None,
            )

        local_providers = tuple(
            provider for provider in tier if provider in LOCAL_FALLBACK_LABELS
        )
        if local_providers:
            return ProviderReadinessRecommendation(
                capability=capability,
                provider=local_providers[0],
                state="runtime_only",
                reason=(
                    "Local fallback eligibility is validated during the "
                    "analysis after an operational primary failure."
                ),
                latency_ms=None,
            )

    return ProviderReadinessRecommendation(
        capability=capability,
        provider=None,
        state="unavailable",
        reason="No configured network source is currently reachable.",
        latency_ms=None,
    )


def build_provider_readiness_recommendations(
    results: Sequence[ProviderReadinessResult],
) -> tuple[ProviderReadinessRecommendation, ...]:
    """Build stable, quality-first recommendations for every capability."""

    return tuple(
        recommend_provider_readiness(capability, results)
        for capability in QUALITY_SOURCE_CHAINS
    )


__all__ = [
    "CredentialMode",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "FALLBACK_CHAINS",
    "LOCAL_FALLBACK_LABELS",
    "MAX_READINESS_WORKERS",
    "ProbeKind",
    "ProviderReadinessResult",
    "ProviderReadinessRecommendation",
    "ProviderReadinessSession",
    "ProviderReadinessTarget",
    "QUALITY_SOURCE_CHAINS",
    "ReadinessState",
    "RecommendationState",
    "build_provider_readiness_recommendations",
    "configured_provider_readiness_targets",
    "probe_provider_readiness",
    "provider_readiness_target_map",
    "recommend_provider_readiness",
    "run_provider_readiness_checks",
]
