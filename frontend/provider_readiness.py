"""Pre-upload rendering for bounded provider connectivity checks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable, Mapping, Sequence

import pandas as pd
import streamlit as st

from backend.provider_readiness import (
    ProviderReadinessResult,
    ProviderReadinessTarget,
    configured_provider_readiness_targets,
    run_provider_readiness_checks,
)


PROVIDER_READINESS_RESULTS_KEY = "provider_readiness_results"
PROVIDER_READINESS_CHECKED_AT_KEY = "provider_readiness_checked_at"
_CAPABILITY_LABELS = {
    "classification_context": "Classification context",
    "clinvar_evidence": "ClinVar evidence",
    "cspec_context": "CSpec context",
    "disease_hpo_context": "Disease/HPO context",
    "gene_disease_literature": "Gene/disease literature",
    "gene_disease_validity": "Gene–disease validity",
    "phenotype_extraction": "Phenotype extraction",
    "phenotype_gene_context": "Phenotype–gene context",
    "population_frequency": "Population frequency",
    "variant_annotation": "Variant annotation",
    "variant_context": "Variant context",
    "variant_interpretation": "Variant interpretation",
    "variant_literature": "Variant literature",
}
_FAILURE_DETAILS = {
    "client_error": "The endpoint rejected the readiness request.",
    "configuration_error": "The configured API access was rejected.",
    "connection_error": "A network connection could not be established.",
    "dns_failure": "The provider hostname could not be resolved.",
    "none": "The readiness request completed.",
    "rate_limited": "The provider is currently rate-limiting requests.",
    "request_error": "The readiness request could not be completed.",
    "server_error": "The provider returned a server error.",
    "timeout": "The provider did not respond before the deadline.",
    "tls_failure": "A secure TLS connection could not be established.",
}


def initialize_provider_readiness_state() -> None:
    """Initialize the per-session, non-persistent readiness result state."""

    st.session_state.setdefault(PROVIDER_READINESS_RESULTS_KEY, ())
    st.session_state.setdefault(PROVIDER_READINESS_CHECKED_AT_KEY, None)


def _fallback_role(target: ProviderReadinessTarget) -> str:
    if not target.fallback_for:
        return "Primary or independent source"
    labels = [
        _CAPABILITY_LABELS.get(capability, capability.replace("_", " "))
        for capability in target.fallback_for
    ]
    return "Fallback for " + ", ".join(labels)


def _check_label(target: ProviderReadinessTarget) -> str:
    return "API-key access" if target.probe_kind == "authenticated" else "Endpoint"


def _result_detail(result: ProviderReadinessResult) -> str:
    if result.failure_category == "none":
        return (
            "Configured API key accepted."
            if result.probe_kind == "authenticated"
            else "Endpoint responded."
        )
    return _FAILURE_DETAILS.get(
        result.failure_category,
        "The readiness request could not be completed.",
    )


def build_provider_readiness_rows(
    results: Sequence[ProviderReadinessResult],
    targets: Sequence[ProviderReadinessTarget] | None = None,
) -> list[dict[str, str]]:
    """Return safe, reviewer-readable rows in stable provider order."""

    target_rows = (
        tuple(targets)
        if targets is not None
        else configured_provider_readiness_targets()
    )
    result_by_provider: Mapping[str, ProviderReadinessResult] = {
        result.provider: result for result in results
    }
    rows: list[dict[str, str]] = []
    for target in target_rows:
        result = result_by_provider.get(target.provider)
        status = "Not checked"
        latency = "—"
        detail = "Run the readiness check to test this provider."
        if result is not None:
            status = "Reachable" if result.state == "reachable" else "Unreachable"
            latency = (
                f"{result.latency_ms:.0f} ms"
                if result.latency_ms is not None
                else "—"
            )
            detail = _result_detail(result)
        rows.append(
            {
                "Provider": target.label,
                "Use": ", ".join(
                    _CAPABILITY_LABELS.get(
                        capability,
                        capability.replace("_", " "),
                    )
                    for capability in target.capabilities
                ),
                "Fallback role": _fallback_role(target),
                "Status": status,
                "Check": _check_label(target),
                "Latency": latency,
                "Details": detail,
            }
        )
    return rows


def _status_style(value: object) -> str:
    if value == "Reachable":
        return "color: #1b7f3a; font-weight: 600"
    if value == "Unreachable":
        return "color: #b42318; font-weight: 600"
    return "color: #667085"


def render_provider_readiness(
    *,
    job_active: bool,
    checker: Callable[[], tuple[ProviderReadinessResult, ...]] = (
        run_provider_readiness_checks
    ),
) -> None:
    """Render the user-triggered pre-upload connectivity table."""

    with st.container(border=True):
        st.subheader("Provider readiness")
        st.caption(
            "Check current endpoint access before uploading variants. "
            "This does not submit variant data or clinical text."
        )
        if st.button(
            "Check provider readiness",
            key="check_provider_readiness",
            icon=":material/network_check:",
            disabled=job_active,
        ):
            with st.spinner("Checking provider readiness...", show_time=True):
                st.session_state[PROVIDER_READINESS_RESULTS_KEY] = checker()
            st.session_state[PROVIDER_READINESS_CHECKED_AT_KEY] = (
                datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )

        results = tuple(st.session_state[PROVIDER_READINESS_RESULTS_KEY])
        checked_at = st.session_state[PROVIDER_READINESS_CHECKED_AT_KEY]
        if checked_at is None:
            st.info("Provider readiness has not been checked in this session.")
        else:
            st.caption(f"Last checked: {checked_at}")

        rows = build_provider_readiness_rows(results)
        table = pd.DataFrame(rows)
        st.dataframe(
            table.style.map(_status_style, subset=["Status"]),
            hide_index=True,
            column_order=(
                "Provider",
                "Use",
                "Fallback role",
                "Status",
                "Check",
                "Latency",
                "Details",
            ),
            key="provider_readiness_table",
        )
