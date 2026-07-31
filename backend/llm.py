"""Provider-neutral LLM contract for clinical interpretation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from time import perf_counter
from typing import Literal, Protocol
from urllib.parse import urlsplit

import requests

from backend.logging_config import get_logger
from config import settings


LLMRole = Literal["system", "user", "assistant"]
LOGGER = get_logger("llm")


class LLMError(RuntimeError):
    """Base error for the LLM boundary."""


class LLMConfigurationError(LLMError):
    """Raised when no usable LLM provider configuration exists."""


class LLMValidationError(LLMError):
    """Raised when a provider-neutral request violates the contract."""


class LLMRequestError(LLMError):
    """Raised when an LLM provider request cannot be completed."""


class LLMAuthenticationError(LLMRequestError):
    """Raised when the provider rejects the configured credentials."""


class LLMRateLimitError(LLMRequestError):
    """Raised when the provider rate-limits a request."""


class LLMTimeoutError(LLMRequestError):
    """Raised when the provider request exceeds its timeout."""


class LLMResponseError(LLMError):
    """Raised when a provider returns an invalid standardized response."""


def _llm_error_outcome(error: LLMError) -> str:
    """Map one provider-neutral error to a bounded log outcome."""

    if isinstance(error, LLMAuthenticationError):
        return "authentication_error"
    if isinstance(error, LLMRateLimitError):
        return "rate_limited"
    if isinstance(error, LLMTimeoutError):
        return "timeout"
    if isinstance(error, LLMRequestError):
        return "request_error"
    if isinstance(error, LLMResponseError):
        return "response_error"
    if isinstance(error, LLMConfigurationError):
        return "configuration_error"
    if isinstance(error, LLMValidationError):
        return "validation_error"
    return "llm_error"


def _log_llm_call(
    request: LLMRequest,
    *,
    started_at: float,
    outcome: str,
    response: LLMResponse | None = None,
    error_type: str | None = None,
) -> None:
    """Log bounded LLM telemetry without prompts or response content."""

    usage = response.usage if response is not None else None
    log_method = LOGGER.info if outcome == "success" else LOGGER.warning
    log_method(
        "event=llm_call provider_protocol=%s outcome=%s "
        "duration_ms=%d message_count=%d max_tokens=%d "
        "input_tokens=%s output_tokens=%s error_type=%s",
        settings.LLM_PROVIDER,
        outcome,
        max(0, round((perf_counter() - started_at) * 1000)),
        len(request.messages),
        request.max_tokens,
        usage.input_tokens if usage is not None else None,
        usage.output_tokens if usage is not None else None,
        error_type,
    )


def _require_text(
    value: object,
    field_name: str,
    *,
    response_field: bool = False,
) -> str:
    error_type = (
        LLMResponseError
        if response_field
        else LLMValidationError
    )

    if not isinstance(value, str) or not value.strip():
        raise error_type(
            f"{field_name} must be a non-empty string."
        )

    return value


def _validate_token_count(
    value: object,
    field_name: str,
) -> None:
    if value is None:
        return

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise LLMResponseError(
            f"{field_name} must be a non-negative integer or None."
        )


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One provider-neutral chat message."""

    role: LLMRole
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise LLMValidationError(
                "LLM message role must be system, user, or assistant."
            )
        _require_text(self.content, "LLM message content")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Validated input passed from application code to an LLM adapter."""

    messages: tuple[LLMMessage, ...]
    temperature: float = 0.0
    max_tokens: int = 1000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.messages, tuple)
            or not self.messages
            or any(
                not isinstance(message, LLMMessage)
                for message in self.messages
            )
        ):
            raise LLMValidationError(
                "messages must be a non-empty tuple of LLMMessage objects."
            )

        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not isfinite(self.temperature)
            or not 0.0 <= self.temperature <= 2.0
        ):
            raise LLMValidationError(
                "temperature must be a finite number from 0.0 to 2.0."
            )

        if (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens <= 0
        ):
            raise LLMValidationError(
                "max_tokens must be a positive integer."
            )


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Optional provider-neutral token accounting."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        _validate_token_count(
            self.input_tokens,
            "input_tokens",
        )
        _validate_token_count(
            self.output_tokens,
            "output_tokens",
        )
        _validate_token_count(
            self.total_tokens,
            "total_tokens",
        )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Standard response returned to every application caller."""

    content: str
    model: str
    finish_reason: str | None = None
    usage: LLMUsage | None = None

    def __post_init__(self) -> None:
        _require_text(
            self.content,
            "LLM response content",
            response_field=True,
        )
        _require_text(
            self.model,
            "LLM response model",
            response_field=True,
        )

        if (
            self.finish_reason is not None
            and (
                not isinstance(self.finish_reason, str)
                or not self.finish_reason.strip()
            )
        ):
            raise LLMResponseError(
                "finish_reason must be a non-empty string or None."
            )

        if (
            self.usage is not None
            and not isinstance(self.usage, LLMUsage)
        ):
            raise LLMResponseError(
                "usage must be an LLMUsage object or None."
            )


class LLMAdapter(Protocol):
    """Internal adapter interface implemented once per provider protocol."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate one standardized response."""


class LLMHTTPSession(Protocol):
    """Minimal HTTP dependency used by provider adapters."""

    def post(
        self,
        url: str,
        **kwargs: object,
    ) -> requests.Response:
        """Send one HTTP POST request."""


class OpenAICompatibleAdapter:
    """Translate the neutral contract to OpenAI-compatible chat HTTP."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int | float,
        session: LLMHTTPSession | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise LLMConfigurationError(
                "LLM base URL must be a non-empty string."
            )
        parsed_url = urlsplit(base_url.strip())
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or bool(parsed_url.query)
            or bool(parsed_url.fragment)
        ):
            raise LLMConfigurationError(
                "LLM base URL must be a secure HTTPS URL without "
                "credentials, query parameters, or fragments."
            )
        if not isinstance(api_key, str) or not api_key.strip():
            raise LLMConfigurationError(
                "LLM API key must be a non-empty string."
            )
        if not isinstance(model, str) or not model.strip():
            raise LLMConfigurationError(
                "LLM model must be a non-empty string."
            )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise LLMConfigurationError(
                "LLM timeout must be a positive finite number."
            )

        self._endpoint = (
            f"{base_url.strip().rstrip('/')}/chat/completions"
        )
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout = timeout
        self._session = session or requests

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute and standardize one non-streaming chat completion."""

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }

        try:
            response = self._session.post(
                self._endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
                verify=True,
            )
        except requests.Timeout as exc:
            raise LLMTimeoutError(
                "The LLM provider request timed out."
            ) from exc
        except requests.RequestException as exc:
            raise LLMRequestError(
                "Could not connect to the LLM provider."
            ) from exc

        self._raise_for_status(response)

        try:
            response_data = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(
                "The LLM provider returned invalid JSON."
            ) from exc

        return self._parse_response(response_data)

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        status_code = response.status_code

        if 200 <= status_code < 300:
            return
        if status_code in {401, 403}:
            raise LLMAuthenticationError(
                "The LLM provider rejected the configured credentials "
                f"or permissions (HTTP {status_code})."
            )
        if status_code == 429:
            raise LLMRateLimitError(
                "The LLM provider rate limit was exceeded (HTTP 429)."
            )

        raise LLMRequestError(
            f"The LLM provider returned HTTP {status_code}."
        )

    def _parse_response(
        self,
        response_data: object,
    ) -> LLMResponse:
        if not isinstance(response_data, Mapping):
            raise LLMResponseError(
                "The LLM provider response must be a JSON object."
            )

        choices = response_data.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], Mapping)
        ):
            raise LLMResponseError(
                "The LLM provider response has no valid choices."
            )

        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise LLMResponseError(
                "The LLM provider response has no valid message."
            )

        content = message.get("content")
        model = response_data.get("model", self._model)
        finish_reason = choices[0].get("finish_reason")
        usage = self._parse_usage(response_data.get("usage"))

        return LLMResponse(
            content=content,  # type: ignore[arg-type]
            model=model,  # type: ignore[arg-type]
            finish_reason=finish_reason,  # type: ignore[arg-type]
            usage=usage,
        )

    @staticmethod
    def _parse_usage(usage_data: object) -> LLMUsage | None:
        if usage_data is None:
            return None
        if not isinstance(usage_data, Mapping):
            raise LLMResponseError(
                "The LLM provider usage value must be a JSON object."
            )

        return LLMUsage(
            input_tokens=usage_data.get(
                "prompt_tokens",
                usage_data.get("input_tokens"),
            ),
            output_tokens=usage_data.get(
                "completion_tokens",
                usage_data.get("output_tokens"),
            ),
            total_tokens=usage_data.get("total_tokens"),
        )


class LLMClient:
    """Single application-facing gateway around an internal adapter."""

    def __init__(self, adapter: LLMAdapter) -> None:
        if not callable(getattr(adapter, "generate", None)):
            raise LLMConfigurationError(
                "LLM adapter must provide generate(request)."
            )
        self._adapter = adapter

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not isinstance(request, LLMRequest):
            raise LLMValidationError(
                "request must be an LLMRequest object."
            )

        started_at = perf_counter()
        try:
            response = self._adapter.generate(request)
        except LLMError as exc:
            _log_llm_call(
                request,
                started_at=started_at,
                outcome=_llm_error_outcome(exc),
                error_type=type(exc).__name__,
            )
            raise
        except Exception as exc:
            _log_llm_call(
                request,
                started_at=started_at,
                outcome="unexpected_error",
                error_type=type(exc).__name__,
            )
            raise LLMRequestError(
                "The LLM provider request failed."
            ) from exc

        if not isinstance(response, LLMResponse):
            _log_llm_call(
                request,
                started_at=started_at,
                outcome="response_error",
                error_type="InvalidAdapterResponse",
            )
            raise LLMResponseError(
                "LLM adapter must return an LLMResponse object."
            )

        _log_llm_call(
            request,
            started_at=started_at,
            outcome="success",
            response=response,
        )
        return response


def get_default_llm_client() -> LLMClient:
    """Build the client selected entirely through central settings."""

    if settings.LLM_PROVIDER != "openai_compatible":
        raise LLMConfigurationError(
            f"Unsupported LLM_PROVIDER '{settings.LLM_PROVIDER}'. "
            "Supported provider protocol: openai_compatible."
        )

    return LLMClient(
        OpenAICompatibleAdapter(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            timeout=settings.LLM_TIMEOUT,
        )
    )


def call_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 1000,
    client: LLMClient | None = None,
) -> LLMResponse:
    """Call an LLM without exposing provider-specific SDK details."""

    request = LLMRequest(
        messages=(
            LLMMessage(
                role="system",
                content=_require_text(
                    system_prompt,
                    "system_prompt",
                ),
            ),
            LLMMessage(
                role="user",
                content=_require_text(
                    user_prompt,
                    "user_prompt",
                ),
            ),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
    )

    active_client = (
        client
        if client is not None
        else get_default_llm_client()
    )

    if not isinstance(active_client, LLMClient):
        raise LLMConfigurationError(
            "client must be an LLMClient object."
        )

    return active_client.generate(request)


__all__ = [
    "LLMAdapter",
    "LLMAuthenticationError",
    "LLMClient",
    "LLMConfigurationError",
    "LLMError",
    "LLMMessage",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMRequestError",
    "LLMResponse",
    "LLMResponseError",
    "LLMRole",
    "LLMTimeoutError",
    "LLMUsage",
    "LLMValidationError",
    "OpenAICompatibleAdapter",
    "call_llm",
    "get_default_llm_client",
]
