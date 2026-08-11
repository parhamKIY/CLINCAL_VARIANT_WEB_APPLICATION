"""Stage 98 safe per-provider technical diagnostic projection."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TypedDict


class ProviderDiagnostic(TypedDict):
    """One bounded developer diagnostic row for a specific variant."""

    provider: str
    variant_identity: str
    status: str
    attempt_count: int | None
    latency_ms: float | None
    fallback_used: bool
    failure_category: str
    provider_note: str


_ATTEMPT_FIELDS = (
    "request_attempts",
    "attempt_count",
    "attempts",
    "primary_request_attempts",
)
_LATENCY_FIELDS = ("latency_ms", "duration_ms", "elapsed_ms")
_OPERATIONAL_FAILURES = frozenset(
    {
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
        "configuration_error",
    }
)
_MAX_DEPTH = 6
_MAX_ITEMS = 100


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalized(value: object) -> str:
    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def _status(value: object) -> str:
    return str(value or "not_recorded").strip().casefold().replace(" ", "_")


def _iter_mappings(value: object, *, depth: int = 0) -> Iterator[Mapping[str, object]]:
    if depth > _MAX_DEPTH:
        return
    current = _mapping(value)
    if current is not None:
        yield current
        for child in list(current.values())[:_MAX_ITEMS]:
            yield from _iter_mappings(child, depth=depth + 1)
        return
    for child in list(_sequence(value))[:_MAX_ITEMS]:
        yield from _iter_mappings(child, depth=depth + 1)


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _non_negative_number(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or float(value) < 0
    ):
        return None
    return round(float(value), 2)


def _telemetry(
    evidence: object,
    *,
    provider: str,
    capability: str,
) -> tuple[int | None, float | None]:
    provider_key = _normalized(provider)
    capability_key = _normalized(capability)
    best: tuple[int, Mapping[str, object]] | None = None
    for candidate in _iter_mappings(evidence):
        has_telemetry = any(
            field in candidate for field in (*_ATTEMPT_FIELDS, *_LATENCY_FIELDS)
        )
        if not has_telemetry:
            continue
        candidate_capability = _normalized(candidate.get("capability"))
        provider_values = (
            candidate.get("provider"),
            candidate.get("source"),
            candidate.get("operational_provider"),
            candidate.get("primary_provider"),
            candidate.get("fallback_provider"),
        )
        provider_match = any(
            value_key
            and (
                value_key in provider_key
                or provider_key in value_key
            )
            for value in provider_values
            if (value_key := _normalized(value))
        )
        capability_match = bool(
            candidate_capability
            and candidate_capability == capability_key
        )
        score = 2 * int(capability_match) + int(provider_match)
        if score == 0:
            continue
        if best is None or score > best[0]:
            best = (score, candidate)
    if best is None:
        return None, None
    candidate = best[1]
    attempts = next(
        (
            parsed
            for field in _ATTEMPT_FIELDS
            if (parsed := _non_negative_int(candidate.get(field))) is not None
        ),
        None,
    )
    latency = next(
        (
            parsed
            for field in _LATENCY_FIELDS
            if (parsed := _non_negative_number(candidate.get(field))) is not None
        ),
        None,
    )
    return attempts, latency


def _variant_identity(
    summary: Mapping[str, object] | None,
    variant_index: int,
) -> str:
    if summary is None:
        return f"Variant {variant_index + 1}: identity unavailable"
    assembly = _text(summary.get("assembly"))
    chrom = _text(summary.get("chrom"))
    pos = summary.get("pos")
    ref = _text(summary.get("ref"))
    alt = _text(summary.get("alt"))
    if (
        assembly
        and chrom
        and isinstance(pos, int)
        and not isinstance(pos, bool)
        and ref
        and alt
    ):
        return f"{assembly} {chrom}:{pos} {ref}>{alt}"
    return _text(summary.get("display_label")) or (
        f"Variant {variant_index + 1}: identity unavailable"
    )


def _failure_category(source: Mapping[str, object], status: str) -> str:
    primary_failure = _text(source.get("primary_failure"))
    if primary_failure:
        return primary_failure
    return status if status in _OPERATIONAL_FAILURES else "none"


def _provider_note(
    provider: str,
    status: str,
    *,
    fallback_used: bool,
    failure_category: str,
) -> str:
    if fallback_used:
        return (
            f"{provider} supplied fallback evidence after the primary source "
            f"reported {failure_category.replace('_', ' ')}."
        )
    if status == "no_match":
        return f"{provider} returned a valid no-match result."
    if status in _OPERATIONAL_FAILURES or status == "unsupported":
        return (
            f"{provider} could not supply this capability; remaining evidence "
            "was retained."
        )
    if status in {"not_assessed", "not_triggered", "not_applicable"}:
        return f"{provider} was not assessed for this capability."
    return f"{provider} evidence was retained for this capability."


def build_provider_diagnostics(
    result: Mapping[str, object],
) -> dict[int, list[ProviderDiagnostic]]:
    """Build safe ordered diagnostics without exposing raw provider payloads."""

    evidence_items = _sequence(result.get("evidence_objects"))
    diagnostics: dict[int, list[ProviderDiagnostic]] = {}
    for raw_report in _sequence(result.get("draft_variant_reports")):
        report = _mapping(raw_report)
        if report is None:
            continue
        variant_index = report.get("variant_index")
        if (
            isinstance(variant_index, bool)
            or not isinstance(variant_index, int)
            or variant_index < 0
        ):
            continue
        content = _mapping(report.get("reviewed_report"))
        summary = _mapping(content.get("variant_summary")) if content else None
        identity = _variant_identity(summary, variant_index)
        evidence = (
            evidence_items[variant_index]
            if variant_index < len(evidence_items)
            else None
        )
        rows: list[ProviderDiagnostic] = []
        for raw_source in _sequence(content.get("data_sources")) if content else ():
            source = _mapping(raw_source)
            if source is None:
                continue
            provider = _text(source.get("source"))
            capability = _text(source.get("capability"))
            if provider is None or capability is None:
                continue
            status = _status(
                source.get("operational_status") or source.get("status")
            )
            attempts, latency = _telemetry(
                evidence,
                provider=provider,
                capability=capability,
            )
            fallback_used = source.get("fallback_used") is True
            failure_category = _failure_category(source, status)
            rows.append(
                {
                    "provider": provider,
                    "variant_identity": identity,
                    "status": status,
                    "attempt_count": attempts,
                    "latency_ms": latency,
                    "fallback_used": fallback_used,
                    "failure_category": failure_category,
                    "provider_note": _provider_note(
                        provider,
                        status,
                        fallback_used=fallback_used,
                        failure_category=failure_category,
                    ),
                }
            )
        diagnostics[variant_index] = rows
    return diagnostics


__all__ = ["ProviderDiagnostic", "build_provider_diagnostics"]
