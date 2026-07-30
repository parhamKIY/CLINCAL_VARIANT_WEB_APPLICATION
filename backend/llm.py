"""Provider-neutral LLM contract for clinical interpretation."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal, Protocol


LLMRole = Literal["system", "user", "assistant"]


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

        try:
            response = self._adapter.generate(request)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMRequestError(
                "The LLM provider request failed."
            ) from exc

        if not isinstance(response, LLMResponse):
            raise LLMResponseError(
                "LLM adapter must return an LLMResponse object."
            )

        return response


def get_default_llm_client() -> LLMClient:
    """Resolve the configured adapter in Stage 8 step 2."""

    raise LLMConfigurationError(
        "The default LLM provider adapter is not configured."
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
    "call_llm",
    "get_default_llm_client",
]
