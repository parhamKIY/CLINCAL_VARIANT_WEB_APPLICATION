"""Reviewer-safe explanations for interpretation failure categories."""

from __future__ import annotations


EXPLAINABLE_INTERPRETATION_FAILURE_TYPES = frozenset(
    {
        "request_timeout",
        "connection_error",
        "http_429",
        "http_5xx",
        "authentication_error",
        "configuration_error",
        "invalid_request",
        "empty_response",
        "output_schema_failure",
        "output_parse_failure",
        "safety_or_finish_failure",
        "internal_conversion_failure",
        "gene_identity_unresolved",
    }
)


def interpretation_failure_message(failure_type: object) -> str:
    """Return safe wording for one persisted model failure category."""

    if failure_type in {
        "request_timeout",
        "connection_error",
        "http_429",
        "http_5xx",
        "empty_response",
    }:
        return (
            "The interpretation service remained temporarily unavailable "
            "after bounded recovery."
        )
    if failure_type in {"output_schema_failure", "output_parse_failure"}:
        return (
            "The model returned a response, but no valid structured response "
            "remained after the repair attempt."
        )
    if failure_type in {"authentication_error", "configuration_error"}:
        return (
            "Interpretation could not run because the model provider "
            "configuration requires attention."
        )
    if failure_type == "safety_or_finish_failure":
        return "The model stopped before a usable interpretation was produced."
    if failure_type == "gene_identity_unresolved":
        return (
            "AI interpretation was not started because this variant has no "
            "exact, provenance-backed gene identity."
        )
    return (
        "Interpretation could not be produced because the model workflow "
        "requires attention."
    )


__all__ = [
    "EXPLAINABLE_INTERPRETATION_FAILURE_TYPES",
    "interpretation_failure_message",
]
