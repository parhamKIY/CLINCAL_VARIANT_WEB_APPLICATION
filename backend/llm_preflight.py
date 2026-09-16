"""Isolated LLM preflight connectivity check.

This module provides a single public function that verifies whether a
selected LLM model is reachable and functional before the analysis pipeline
starts.  It is intentionally separated from the pipeline LLM path so that
a preflight failure can never affect analysis execution.

Guarantees:
- No clinical data.
- No patient information.
- No API response text stored or logged.
- No retry logic (single attempt only).
- All exceptions caught; never raises to the caller.
- Result is ephemeral (caller must store it in session state).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from backend.llm import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMJSONObject,
    LLMMessage,
    LLMQuotaError,
    LLMRateLimitError,
    LLMRequest,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleAdapter,
    strip_markdown_json_fences,
)
from backend.logging_config import get_logger
from config import settings


LOGGER = get_logger("llm_preflight")

# Preflight request constants — minimal, anonymous, deterministic structured JSON check.
_PREFLIGHT_SYSTEM = "Respond only with a JSON object: {\"status\": \"ready\"}"
_PREFLIGHT_USER = "ready"
_PREFLIGHT_MAX_TOKENS = 50
_PREFLIGHT_TEMPERATURE = 0.0
_PREFLIGHT_RESPONSE_FORMAT = LLMJSONObject()

# Maximum time (seconds) the preflight check may block the UI.
# Always smaller than the pipeline LLM_TIMEOUT so as not to stall startup.
_PREFLIGHT_TIMEOUT = 10.0

# Human-readable failure categories shown in the UI.
# These must not contain raw exception text or provider payloads.
_CATEGORY_AUTH = "Invalid API key"
_CATEGORY_QUOTA = "Quota exhausted"
_CATEGORY_UNAVAILABLE = "Model unavailable"
_CATEGORY_TIMEOUT = "Timeout"
_CATEGORY_STRUCTURED_OUTPUT = "Structured output unsupported"
_CATEGORY_PARAM_UNSUPPORTED = "Model parameter unsupported"
_CATEGORY_CONTEXT_EXCEEDED = "Context length exceeded"
_CATEGORY_BAD_REQUEST = "Bad request (HTTP 400)"
_CATEGORY_FAILED = "Request failed"


@dataclass(frozen=True, slots=True)
class LLMPreflightResult:
    """Outcome of one isolated preflight connectivity check."""

    ok: bool
    model: str
    failure_category: str | None
    response_ms: int | None
    checked_at: str  # ISO-8601 UTC


def _map_failure_category(error: LLMError) -> str:
    """Map one LLMError subclass to a bounded, safe UI category string."""

    if isinstance(error, LLMAuthenticationError):
        return _CATEGORY_AUTH
    if isinstance(error, (LLMQuotaError, LLMRateLimitError)):
        return _CATEGORY_QUOTA
    if isinstance(error, LLMTimeoutError):
        return _CATEGORY_TIMEOUT
    if isinstance(error, LLMConfigurationError):
        return _CATEGORY_UNAVAILABLE
    if isinstance(error, LLMResponseError):
        return _CATEGORY_STRUCTURED_OUTPUT
    if isinstance(error, LLMRequestError):
        ft = getattr(error, "failure_type", None)
        if ft == "unsupported_response_format":
            return _CATEGORY_STRUCTURED_OUTPUT
        if ft in {"unsupported_temperature", "unsupported_max_tokens"}:
            return _CATEGORY_PARAM_UNSUPPORTED
        if ft == "context_length_exceeded":
            return _CATEGORY_CONTEXT_EXCEEDED
        if ft == "model_not_found":
            return _CATEGORY_UNAVAILABLE
        if getattr(error, "http_status", None) in {400, 422}:
            return _CATEGORY_BAD_REQUEST
        return _CATEGORY_FAILED
    return _CATEGORY_FAILED


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def check_llm_connectivity(
    model: str,
    *,
    timeout: float = _PREFLIGHT_TIMEOUT,
) -> LLMPreflightResult:
    """Run a minimal anonymous LLM connectivity check for *model*.

    Always returns an :class:`LLMPreflightResult`.  Never raises.

    The request uses a tiny token budget, no clinical data, and a dedicated
    short timeout independent of the pipeline ``LLM_TIMEOUT``.

    Args:
        model: The model identifier to test (must be a non-empty string).
        timeout: HTTP timeout in seconds (capped at ``_PREFLIGHT_TIMEOUT``).

    Returns:
        :class:`LLMPreflightResult` with ``ok=True`` on success or
        ``ok=False`` with a bounded ``failure_category`` string on failure.
    """

    bounded_timeout = min(float(timeout), _PREFLIGHT_TIMEOUT)
    checked_at = _utc_now_iso()

    # Validate model string before touching the network.
    if not isinstance(model, str) or not model.strip():
        LOGGER.warning(
            "event=llm_preflight outcome=skipped reason=invalid_model_arg"
        )
        return LLMPreflightResult(
            ok=False,
            model=str(model) if model else "(empty)",
            failure_category=_CATEGORY_UNAVAILABLE,
            response_ms=None,
            checked_at=checked_at,
        )

    clean_model = model.strip()

    try:
        adapter = OpenAICompatibleAdapter(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=clean_model,
            timeout=bounded_timeout,
        )
    except LLMConfigurationError:
        LOGGER.warning(
            "event=llm_preflight outcome=config_error model=%s",
            clean_model,
        )
        return LLMPreflightResult(
            ok=False,
            model=clean_model,
            failure_category=_CATEGORY_UNAVAILABLE,
            response_ms=None,
            checked_at=checked_at,
        )
    except Exception:  # noqa: BLE001
        LOGGER.warning(
            "event=llm_preflight outcome=unexpected_config_error model=%s",
            clean_model,
        )
        return LLMPreflightResult(
            ok=False,
            model=clean_model,
            failure_category=_CATEGORY_FAILED,
            response_ms=None,
            checked_at=checked_at,
        )

    request = LLMRequest(
        messages=(
            LLMMessage(role="system", content=_PREFLIGHT_SYSTEM),
            LLMMessage(role="user", content=_PREFLIGHT_USER),
        ),
        temperature=_PREFLIGHT_TEMPERATURE,
        max_tokens=_PREFLIGHT_MAX_TOKENS,
        response_format=_PREFLIGHT_RESPONSE_FORMAT,
    )

    started = perf_counter()
    try:
        response = adapter.generate(request)
        try:
            parsed = json.loads(strip_markdown_json_fences(response.content))
            if not isinstance(parsed, Mapping):
                raise LLMResponseError(
                    "The preflight JSON response is not an object.",
                    failure_type="output_schema_failure",
                )
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(
                "The preflight response is not valid JSON.",
                failure_type="output_parse_failure",
            ) from exc

        elapsed_ms = max(0, round((perf_counter() - started) * 1000))
        LOGGER.info(
            "event=llm_preflight outcome=ok model=%s duration_ms=%d",
            clean_model,
            elapsed_ms,
        )
        return LLMPreflightResult(
            ok=True,
            model=clean_model,
            failure_category=None,
            response_ms=elapsed_ms,
            checked_at=checked_at,
        )
    except LLMError as exc:
        elapsed_ms = max(0, round((perf_counter() - started) * 1000))
        category = _map_failure_category(exc)
        LOGGER.warning(
            "event=llm_preflight outcome=failed model=%s "
            "category=%s error_type=%s duration_ms=%d",
            clean_model,
            category,
            type(exc).__name__,
            elapsed_ms,
        )
        return LLMPreflightResult(
            ok=False,
            model=clean_model,
            failure_category=category,
            response_ms=elapsed_ms,
            checked_at=checked_at,
        )
    except Exception:  # noqa: BLE001
        elapsed_ms = max(0, round((perf_counter() - started) * 1000))
        LOGGER.warning(
            "event=llm_preflight outcome=unexpected_error model=%s "
            "duration_ms=%d",
            clean_model,
            elapsed_ms,
        )
        return LLMPreflightResult(
            ok=False,
            model=clean_model,
            failure_category=_CATEGORY_FAILED,
            response_ms=elapsed_ms,
            checked_at=checked_at,
        )


__all__ = [
    "LLMPreflightResult",
    "check_llm_connectivity",
]
