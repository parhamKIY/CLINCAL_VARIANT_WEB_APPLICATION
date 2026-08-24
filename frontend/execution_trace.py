"""User-safe Streamlit projection of bounded analysis execution traces."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TypedDict

import streamlit as st

from backend.execution_trace import MAX_EXECUTION_TRACE_EVENTS


_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,79}")
_PROVIDER_LABELS = {
    "clinvar": "ClinVar",
    "ensembl_vep": "Ensembl VEP",
    "genebe": "GeneBe",
    "mydisease": "MyDisease.info",
    "myvariant": "MyVariant.info",
    "phen2gene": "Phen2Gene",
    "variant_validator": "VariantValidator",
    "variantvalidator": "VariantValidator",
}
_TIMELINE_LABELS = {
    "analysis_started": "Analysis started",
    "input_validated": "Input validation completed",
    "input_validation_failed": "Input validation could not be completed",
    "preprocessing_completed": "Variant normalization completed",
    "preprocessing_failed": "Variant normalization could not be completed",
    "annotation_completed": "Variant annotation completed",
    "annotation_failed": "Variant annotation could not be completed",
    "evidence_collection_completed": "Evidence collection completed",
    "evidence_collection_failed": "Evidence collection could not be completed",
    "interpretation_completed": "AI synthesis completed",
    "interpretation_failed": "AI synthesis could not be completed",
    "report_generated": "Report generated",
    "report_generation_failed": "Report generation could not be completed",
    "analysis_completed": "Analysis completed",
    "analysis_aborted": "Analysis stopped before completion",
}
_FAILURE_EVENTS = frozenset(
    event for event in _TIMELINE_LABELS if event.endswith(("failed", "aborted"))
)
_PROVIDER_EVENTS = frozenset(
    {
        "fallback_activated",
        "provider_attempt_completed",
        "provider_call_skipped",
        "provider_circuit_opened",
        "provider_route_completed",
        "provider_source_selected",
        "repository_lookup_failed",
        "repository_lookup_hit",
        "repository_lookup_miss",
        "repository_lookup_stale",
        "repository_record_rejected",
    }
)
_LLM_LABELS = {
    "llm_call_started": (":material/sync:", "AI interpretation request started"),
    "llm_call_completed": (
        ":material/check_circle:",
        "AI interpretation request completed",
    ),
    "llm_retry_scheduled": (":material/refresh:", "Interpretation retry scheduled"),
    "llm_recovery_started": (
        ":material/refresh:",
        "Alternate interpretation model activated",
    ),
    "llm_output_classification_generated": (
        ":material/check_circle:",
        "AI classification generated",
    ),
    "llm_output_interpretation_generated": (
        ":material/check_circle:",
        "Interpretation generated",
    ),
    "variant_interpretation_completed": (
        ":material/check_circle:",
        "Variant interpretation completed",
    ),
}
_LLM_FAILURE_EVENTS = frozenset(
    {"llm_call_failed", "variant_interpretation_failed"}
)


class TraceDisplayItem(TypedDict):
    icon: str
    message: str


class VariantTraceSummary(TypedDict):
    processed: int
    completed: int
    failed: int
    interpreted: int


class ExecutionTraceView(TypedDict):
    available: bool
    timeline: list[TraceDisplayItem]
    providers: list[TraceDisplayItem]
    llm: list[TraceDisplayItem]
    variants: VariantTraceSummary
    events_dropped: int


def _events(snapshot: object) -> list[Mapping[str, object]]:
    if not isinstance(snapshot, Mapping):
        return []
    raw_events = snapshot.get("events")
    if not isinstance(raw_events, Sequence) or isinstance(
        raw_events, (str, bytes)
    ):
        return []
    accepted = [
        event
        for event in raw_events[:MAX_EXECUTION_TRACE_EVENTS]
        if isinstance(event, Mapping)
        and isinstance(event.get("event_type"), str)
        and isinstance(event.get("sequence"), int)
        and not isinstance(event.get("sequence"), bool)
        and int(event["sequence"]) > 0
    ]
    return sorted(accepted, key=lambda event: int(event["sequence"]))


def _event_type(event: Mapping[str, object]) -> str:
    value = event.get("event_type")
    return value if isinstance(value, str) else ""


def _provider_label(value: object) -> str:
    if not isinstance(value, str):
        return "Provider"
    normalized = value.strip()
    known = _PROVIDER_LABELS.get(normalized.casefold())
    if known is not None:
        return known
    return (
        normalized.replace("_", " ")
        if _SAFE_LABEL.fullmatch(normalized) is not None
        else "Provider"
    )


def _variant_label(event: Mapping[str, object]) -> str | None:
    value = event.get("variant_index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return f"Variant {value + 1}"


def _failure_message(value: object, *, llm: bool = False) -> str:
    reason = value.casefold() if isinstance(value, str) else ""
    service = "AI service" if llm else "Provider"
    if reason in {"timeout", "request_timeout"}:
        return f"{service} timed out"
    if reason in {"http_429", "rate_limit", "rate_limited"}:
        return f"{service} is temporarily rate-limited"
    if reason in {
        "connection_error",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "provider_error",
        "request_error",
        "unavailable",
    }:
        return f"{service} is temporarily unavailable"
    if reason in {
        "invalid_response",
        "malformed_response",
        "response_validation",
        "schema_validation",
    }:
        return f"{service} response could not be validated"
    if reason in {"authentication", "authentication_error"}:
        return f"{service} authentication failed"
    return (
        "AI interpretation could not be completed"
        if llm
        else "Provider request could not be completed"
    )


def _provider_status(event: Mapping[str, object]) -> str:
    value = event.get("status")
    status = value.casefold() if isinstance(value, str) else ""
    if status in {"available", "completed", "partial", "success"}:
        return "Evidence retrieved"
    if status in {"no_match", "not_found"}:
        return "No matching evidence record found"
    if status in {"error", "failed", "timeout", "unavailable"}:
        return _failure_message(event.get("reason_category") or status)
    if status in {"invalid", "invalid_response"}:
        return "Provider response could not be validated"
    if status == "stale":
        return "Stored evidence was stale; live refresh required"
    if status == "missing":
        return "No reusable repository record found"
    return "Provider step completed"


def _provider_item(event: Mapping[str, object]) -> TraceDisplayItem | None:
    event_type = _event_type(event)
    if event_type not in _PROVIDER_EVENTS:
        return None
    provider = _provider_label(event.get("provider"))
    status_value = event.get("status")
    status = status_value.casefold() if isinstance(status_value, str) else ""
    details = [label for label in (_variant_label(event),) if label]
    source_mode = event.get("source_mode")
    if source_mode == "live_provider":
        details.append("Source: Live provider")
    elif source_mode == "repository_cache":
        details.append("Source: Repository cache")

    icon = ":material/warning:"
    if event_type == "fallback_activated":
        primary = _provider_label(event.get("fallback_for"))
        details[:0] = [f"Primary source: {primary}"]
        details.append(
            f"Reason: {_failure_message(event.get('reason_category'))}"
        )
        message = f"**{primary} → {provider}** — Fallback activated: {provider}"
    elif event_type == "provider_attempt_completed":
        attempt = event.get("attempt")
        attempt_label = (
            f"Attempt {attempt}"
            if isinstance(attempt, int) and not isinstance(attempt, bool)
            else "Provider attempt"
        )
        message = f"**{provider}** — {attempt_label}: {_provider_status(event)}"
        if status in {"no_match", "success"}:
            icon = ":material/check_circle:"
    elif event_type == "provider_call_skipped":
        message = f"**{provider}** — Request skipped; provider unavailable"
    elif event_type == "provider_circuit_opened":
        message = f"**{provider}** — Further attempts paused after failure"
    elif event_type == "provider_source_selected":
        message = f"**{provider}** — Final evidence source selected"
        icon = ":material/check_circle:"
    elif event_type == "provider_route_completed":
        message = f"**{provider}** — {_provider_status(event)}"
        if status in {"available", "partial", "success"}:
            icon = ":material/check_circle:"
    elif event_type == "repository_lookup_hit":
        message = f"**{provider}** — Reusable evidence record found"
        details.append("Source: Repository cache")
        icon = ":material/check_circle:"
    elif event_type in {"repository_lookup_miss", "repository_lookup_stale"}:
        message = f"**{provider}** — {_provider_status(event)}"
        icon = ":material/info:"
    elif event_type == "repository_lookup_failed":
        message = f"**{provider}** — Repository unavailable; live access required"
    else:
        message = f"**{provider}** — Stored evidence rejected safely"

    if details:
        message = f"{message}  \n{' · '.join(dict.fromkeys(details))}"
    return {"icon": icon, "message": message}


def _llm_item(event: Mapping[str, object]) -> TraceDisplayItem | None:
    event_type = _event_type(event)
    if event_type in _LLM_LABELS:
        icon, message = _LLM_LABELS[event_type]
    elif event_type in _LLM_FAILURE_EVENTS:
        icon = ":material/warning:"
        message = _failure_message(event.get("reason_category"), llm=True)
    else:
        return None
    variant = _variant_label(event)
    return {
        "icon": icon,
        "message": f"{message} — {variant}" if variant else message,
    }


def _variant_indexes(
    events: Sequence[Mapping[str, object]],
    event_types: frozenset[str],
) -> set[int]:
    return {
        value
        for event in events
        if _event_type(event) in event_types
        for value in [event.get("variant_index")]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }


def _variant_summary(
    events: Sequence[Mapping[str, object]],
) -> VariantTraceSummary:
    final = next(
        (
            event
            for event in reversed(events)
            if _event_type(event) == "analysis_completed"
        ),
        {},
    )

    def count(field: str, fallback: int) -> int:
        value = final.get(field)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else fallback
        )

    processed = _variant_indexes(
        events,
        frozenset(
            {
                "evidence_created",
                "variant_interpretation_started",
                "variant_processing_completed",
                "variant_processing_started",
            }
        ),
    )
    completed = _variant_indexes(
        events, frozenset({"variant_interpretation_completed"})
    )
    interpreted = _variant_indexes(
        events,
        frozenset(
            {
                "llm_output_interpretation_generated",
                "variant_interpretation_completed",
            }
        ),
    )
    failed = _variant_indexes(
        events,
        frozenset(
            {"evidence_creation_failed", "variant_interpretation_failed"}
        ),
    )
    return {
        "processed": count("variant_count", len(processed)),
        "completed": count("succeeded_count", len(completed)),
        "failed": count("failed_count", len(failed)),
        "interpreted": len(interpreted),
    }


def build_execution_trace_view(snapshot: object) -> ExecutionTraceView:
    """Project only allowlisted trace metadata into user-facing semantics."""

    events = _events(snapshot)
    timeline = [
        {
            "icon": (
                ":material/warning:"
                if event_type in _FAILURE_EVENTS
                else ":material/check_circle:"
            ),
            "message": _TIMELINE_LABELS[event_type],
        }
        for event in events
        for event_type in [_event_type(event)]
        if event_type in _TIMELINE_LABELS
    ]
    providers = [
        item for event in events if (item := _provider_item(event)) is not None
    ]
    llm = [item for event in events if (item := _llm_item(event)) is not None]
    raw_dropped = snapshot.get("events_dropped") if isinstance(snapshot, Mapping) else 0
    dropped = (
        raw_dropped
        if isinstance(raw_dropped, int)
        and not isinstance(raw_dropped, bool)
        and raw_dropped >= 0
        else 0
    )
    return {
        "available": bool(events),
        "timeline": timeline,
        "providers": providers,
        "llm": llm,
        "variants": _variant_summary(events),
        "events_dropped": dropped,
    }


def _render_items(items: Sequence[TraceDisplayItem]) -> None:
    for item in items:
        st.markdown(f"{item['icon']} {item['message']}")


def render_execution_trace(snapshot: object) -> None:
    """Render the additive trace panel using responsive native containers."""

    view = build_execution_trace_view(snapshot)
    st.subheader("Analysis execution trace")
    st.caption(
        "This view summarizes the analysis route. Existing progress and "
        "technical provider details remain available above."
    )
    if not view["available"]:
        st.info(
            "Execution trace details are unavailable for this analysis. "
            "Older restored analyses may not include a runtime trace.",
            icon=":material/info:",
        )
        return

    with st.container(border=True):
        st.markdown("**Analysis timeline**")
        _render_items(view["timeline"])

    with st.container(horizontal=True):
        st.metric("Processed", view["variants"]["processed"])
        st.metric("Completed", view["variants"]["completed"])
        st.metric("Failed", view["variants"]["failed"])
        st.metric("Interpreted", view["variants"]["interpreted"])

    if view["providers"]:
        with st.container(border=True):
            st.markdown("**Provider journey**")
            _render_items(view["providers"])
    if view["llm"]:
        with st.container(border=True):
            st.markdown("**AI interpretation journey**")
            _render_items(view["llm"])
    if view["events_dropped"]:
        st.caption(
            "Some later events were omitted because the bounded runtime "
            "trace reached its safety limit."
        )


__all__ = [
    "ExecutionTraceView",
    "TraceDisplayItem",
    "VariantTraceSummary",
    "build_execution_trace_view",
    "render_execution_trace",
]
