"""Provider-neutral LLM contract for clinical interpretation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from time import perf_counter, sleep
from typing import Literal, Protocol
from urllib.parse import urlsplit

import requests

from backend.execution_trace import record_execution_event
from backend.logging_config import get_logger
from config import settings


LLMRole = Literal["system", "user", "assistant"]
LOGGER = get_logger("llm")
LLM_RESPONSE_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_LLM_RESPONSE_SCHEMA_BYTES = 64 * 1024
MAX_LLM_RETRY_DELAY_SECONDS = 30.0


class LLMError(RuntimeError):
    """Base error for the LLM boundary."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        attempt: int = 1,
        finish_reason: str | None = None,
        schema_error: str | None = None,
        failure_type: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.attempt = attempt
        self.finish_reason = finish_reason
        self.schema_error = schema_error
        self.failure_type = failure_type
        self.retry_after_seconds = retry_after_seconds


class LLMConfigurationError(LLMError):
    """Raised when no usable LLM provider configuration exists."""


class LLMValidationError(LLMError):
    """Raised when a provider-neutral request violates the contract."""


class LLMRequestError(LLMError):
    """Raised when an LLM provider request cannot be completed."""


class LLMQuotaError(LLMRequestError):
    """Raised when provider quota or account credit is insufficient."""


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
    if isinstance(error, LLMQuotaError):
        return "quota_exhausted"
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


def _is_retryable_request_error(error: LLMRequestError) -> bool:
    if isinstance(error, LLMQuotaError):
        return False
    if error.failure_type == "internal_conversion_failure":
        return False
    status = error.http_status
    if status is None:
        return True
    return status == 429 or 500 <= status <= 599


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
        if response_field:
            failure_type = (
                "empty_response"
                if field_name == "LLM response content"
                else "output_schema_failure"
            )
            raise LLMResponseError(
                f"{field_name} must be a non-empty string.",
                failure_type=failure_type,
                schema_error="missing_response_field",
            )
        raise error_type(f"{field_name} must be a non-empty string.")

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
            f"{field_name} must be a non-negative integer or None.",
            failure_type="output_schema_failure",
            schema_error="invalid_token_usage",
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
class LLMJSONSchema:
    """Provider-neutral strict JSON-schema response contract."""

    name: str
    schema: Mapping[str, object]
    strict: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or LLM_RESPONSE_SCHEMA_NAME_PATTERN.fullmatch(self.name) is None
        ):
            raise LLMValidationError(
                "JSON schema name must contain 1-64 letters, digits, "
                "underscores, or hyphens."
            )
        if not isinstance(self.schema, Mapping):
            raise LLMValidationError("JSON schema must be a mapping.")
        if self.schema.get("type") != "object":
            raise LLMValidationError(
                "JSON schema must define a top-level object."
            )
        if not isinstance(self.strict, bool):
            raise LLMValidationError("JSON schema strict must be a boolean.")
        try:
            serialized = json.dumps(
                self.schema,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise LLMValidationError(
                "JSON schema must be JSON serializable."
            ) from exc
        if len(serialized) > MAX_LLM_RESPONSE_SCHEMA_BYTES:
            raise LLMValidationError("JSON schema is too large.")

    def to_provider_payload(self) -> dict[str, object]:
        """Return an isolated OpenAI-compatible response-format value."""

        return {
            "type": "json_schema",
            "json_schema": {
                "name": self.name,
                "schema": deepcopy(dict(self.schema)),
                "strict": self.strict,
            },
        }


@dataclass(frozen=True, slots=True)
class LLMJSONObject:
    """Provider-neutral generic JSON object response contract."""

    def to_provider_payload(self) -> dict[str, object]:
        """Return an isolated OpenAI-compatible response-format value."""
        return {"type": "json_object"}


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Validated input passed from application code to an LLM adapter."""

    messages: tuple[LLMMessage, ...]
    temperature: float = 0.0
    max_tokens: int = 1000
    response_format: LLMJSONSchema | LLMJSONObject | None = None

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
        if (
            self.response_format is not None
            and not isinstance(self.response_format, (LLMJSONSchema, LLMJSONObject))
        ):
            raise LLMValidationError(
                "response_format must be an LLMJSONSchema, LLMJSONObject, or None."
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
                "finish_reason must be a non-empty string or None.",
                failure_type="output_schema_failure",
                schema_error="invalid_finish_reason",
            )

        if (
            self.usage is not None
            and not isinstance(self.usage, LLMUsage)
        ):
            raise LLMResponseError(
                "usage must be an LLMUsage object or None.",
                failure_type="internal_conversion_failure",
                schema_error="invalid_usage_conversion",
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

    def get(
        self,
        url: str,
        **kwargs: object,
    ) -> requests.Response:
        """Send one HTTP GET request."""


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
        self._models_endpoint = f"{base_url.strip().rstrip('/')}/models"
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout = timeout
        self._session = session or requests

    def list_models(self) -> tuple[str, ...]:
        """Return validated model identifiers advertised by the provider."""

        try:
            response = self._session.get(
                self._models_endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                },
                timeout=min(float(self._timeout), 5.0),
                verify=True,
            )
        except requests.Timeout as exc:
            raise LLMTimeoutError(
                "The LLM model catalog request timed out."
            ) from exc
        except requests.RequestException as exc:
            raise LLMRequestError(
                "Could not load the LLM provider model catalog."
            ) from exc
        self._raise_for_status(response)
        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(
                "The LLM provider model catalog returned invalid JSON."
            ) from exc
        if not isinstance(payload, Mapping):
            raise LLMResponseError(
                "The LLM provider model catalog must be a JSON object."
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise LLMResponseError(
                "The LLM provider model catalog has no valid data list."
            )
        models: list[str] = []
        for item in data[:500]:
            if not isinstance(item, Mapping):
                continue
            model_id = item.get("id")
            if (
                isinstance(model_id, str)
                and 0 < len(model_id.strip()) <= 200
                and all(ord(character) >= 32 for character in model_id)
            ):
                models.append(model_id.strip())
        return tuple(dict.fromkeys(models))

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
        if request.response_format is not None:
            payload["response_format"] = (
                request.response_format.to_provider_payload()
            )

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
                "Could not connect to the LLM provider.",
                schema_error="connection_failed",
            ) from exc

        if (
            response.status_code in {400, 422}
            and isinstance(request.response_format, LLMJSONSchema)
        ):
            fallback_payload = dict(payload)
            fallback_payload["response_format"] = {"type": "json_object"}
            LOGGER.info(
                "event=llm_response_format_fallback model=%s "
                "reason=json_schema_unsupported_status_%d",
                self._model,
                response.status_code,
            )
            try:
                fallback_response = self._session.post(
                    self._endpoint,
                    json=fallback_payload,
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
                    "Could not connect to the LLM provider.",
                    schema_error="connection_failed",
                ) from exc

            if 200 <= fallback_response.status_code < 300:
                response = fallback_response

        self._raise_for_status(response)

        try:
            response_data = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(
                "The LLM provider returned invalid JSON.",
                failure_type="output_parse_failure",
                schema_error="invalid_provider_json",
            ) from exc

        return self._parse_response(response_data)

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        status_code = response.status_code

        if 200 <= status_code < 300:
            return
        provider_error_code = OpenAICompatibleAdapter._provider_error_code(
            response
        )
        if provider_error_code in {
            "billing_hard_limit_reached",
            "billing_not_active",
            "credit_balance_too_low",
            "insufficient_quota",
            "insufficient_user_quota",
        }:
            raise LLMQuotaError(
                "The LLM provider quota or credit is insufficient.",
                http_status=status_code,
                failure_type="insufficient_quota",
            )
        if status_code in {401, 403}:
            raise LLMAuthenticationError(
                "The LLM provider rejected the configured credentials "
                f"or permissions (HTTP {status_code}).",
                http_status=status_code,
            )
        if status_code == 429:
            raise LLMRateLimitError(
                "The LLM provider rate limit was exceeded (HTTP 429).",
                http_status=status_code,
                retry_after_seconds=(
                    OpenAICompatibleAdapter._retry_after_seconds(response)
                ),
            )

        raise LLMRequestError(
            f"The LLM provider returned HTTP {status_code}.",
            http_status=status_code,
        )

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> float | None:
        """Return a bounded provider-directed delay without retaining headers."""

        headers = getattr(response, "headers", None)
        if not isinstance(headers, Mapping):
            return None
        value = headers.get("Retry-After")
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized.isdigit():
            return None
        return min(float(int(normalized)), MAX_LLM_RETRY_DELAY_SECONDS)

    @staticmethod
    def _provider_error_code(response: requests.Response) -> str | None:
        """Read one bounded provider error token without retaining its text."""

        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        error = payload.get("error")
        if not isinstance(error, Mapping):
            return None
        for field in ("code", "type"):
            value = error.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip().casefold()
        return None

    def _parse_response(
        self,
        response_data: object,
    ) -> LLMResponse:
        if not isinstance(response_data, Mapping):
            raise LLMResponseError(
                "The LLM provider response must be a JSON object.",
                failure_type="output_schema_failure",
                schema_error="invalid_response_object",
            )

        choices = response_data.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], Mapping)
        ):
            raise LLMResponseError(
                "The LLM provider response has no valid choices.",
                failure_type="empty_response",
                schema_error="missing_choices",
            )

        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise LLMResponseError(
                "The LLM provider response has no valid message.",
                failure_type="empty_response",
                schema_error="missing_message",
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
                "The LLM provider usage value must be a JSON object.",
                failure_type="output_schema_failure",
                schema_error="invalid_usage_object",
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
                "The LLM provider request failed.",
                failure_type="internal_conversion_failure",
            ) from exc

        if not isinstance(response, LLMResponse):
            _log_llm_call(
                request,
                started_at=started_at,
                outcome="response_error",
                error_type="InvalidAdapterResponse",
            )
            raise LLMResponseError(
                "LLM adapter must return an LLMResponse object.",
                failure_type="internal_conversion_failure",
                schema_error="invalid_adapter_response",
            )

        _log_llm_call(
            request,
            started_at=started_at,
            outcome="success",
            response=response,
        )
        return response


def get_default_llm_client(
    *,
    model: str | None = None,
) -> LLMClient:
    """Build the configured client with an optional request model."""

    if settings.LLM_PROVIDER != "openai_compatible":
        raise LLMConfigurationError(
            f"Unsupported LLM_PROVIDER '{settings.LLM_PROVIDER}'. "
            "Supported provider protocol: openai_compatible."
        )
    selected_model = (
        settings.LLM_MODEL
        if model is None
        else _require_text(model, "model")
    )

    return LLMClient(
        OpenAICompatibleAdapter(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=selected_model,
            timeout=settings.LLM_TIMEOUT,
        )
    )


def get_available_llm_models() -> tuple[str, ...]:
    """Load the current model catalog from the configured provider."""

    if settings.LLM_PROVIDER != "openai_compatible":
        raise LLMConfigurationError(
            f"Unsupported LLM_PROVIDER '{settings.LLM_PROVIDER}'."
        )
    adapter = OpenAICompatibleAdapter(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        model=settings.LLM_MODEL,
        timeout=settings.LLM_TIMEOUT,
    )
    return adapter.list_models()


def call_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 1000,
    client: LLMClient | None = None,
    model: str | None = None,
    max_retries: int | None = None,
    response_format: LLMJSONSchema | LLMJSONObject | None = None,
    trace_variant_index: int | None = None,
) -> LLMResponse:
    """Call an LLM without exposing provider-specific SDK details."""

    if client is not None and model is not None:
        raise LLMConfigurationError(
            "Choose either a custom LLM client or a model override."
        )
    resolved_retries = (
        (0 if client is not None else settings.LLM_MAX_RETRIES)
        if max_retries is None
        else max_retries
    )
    if (
        isinstance(resolved_retries, bool)
        or not isinstance(resolved_retries, int)
        or resolved_retries < 0
    ):
        raise LLMValidationError(
            "max_retries must be a non-negative integer."
        )

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
        response_format=response_format,
    )

    active_client = (
        client
        if client is not None
        else get_default_llm_client(model=model)
    )

    if not isinstance(active_client, LLMClient):
        raise LLMConfigurationError(
            "client must be an LLMClient object."
        )

    for attempt in range(resolved_retries + 1):
        attempt_started_at = perf_counter()
        record_execution_event(
            "llm_call_started",
            scope="llm",
            stage="llm",
            variant_index=trace_variant_index,
            attempt=attempt + 1,
            status="running",
            model_identifier=model or settings.LLM_MODEL,
        )
        try:
            response = active_client.generate(request)
            record_execution_event(
                "llm_call_completed",
                scope="llm",
                stage="llm",
                variant_index=trace_variant_index,
                attempt=attempt + 1,
                status="success",
                outcome_category="success",
                duration_ms=max(
                    0,
                    round((perf_counter() - attempt_started_at) * 1000),
                ),
                model_identifier=response.model,
            )
            return response
        except (
            LLMAuthenticationError,
            LLMConfigurationError,
            LLMResponseError,
            LLMValidationError,
        ) as exc:
            outcome = _llm_error_outcome(exc)
            record_execution_event(
                "llm_call_failed",
                scope="llm",
                stage="llm",
                variant_index=trace_variant_index,
                attempt=attempt + 1,
                status="failed",
                outcome_category=outcome,
                duration_ms=max(
                    0,
                    round((perf_counter() - attempt_started_at) * 1000),
                ),
                reason_category=outcome,
                model_identifier=model or settings.LLM_MODEL,
            )
            raise
        except LLMRequestError as exc:
            outcome = _llm_error_outcome(exc)
            record_execution_event(
                "llm_call_failed",
                scope="llm",
                stage="llm",
                variant_index=trace_variant_index,
                attempt=attempt + 1,
                status="failed",
                outcome_category=outcome,
                duration_ms=max(
                    0,
                    round((perf_counter() - attempt_started_at) * 1000),
                ),
                reason_category=outcome,
                model_identifier=model or settings.LLM_MODEL,
            )
            if not _is_retryable_request_error(exc):
                exc.attempt = attempt + 1
                raise
            if attempt >= resolved_retries:
                exc.attempt = attempt + 1
                raise
            provider_delay = getattr(exc, "retry_after_seconds", None)
            delay_seconds = (
                provider_delay
                if isinstance(provider_delay, (int, float))
                and not isinstance(provider_delay, bool)
                and isfinite(provider_delay)
                and provider_delay >= 0
                else min(float(2**attempt), 5.0)
            )
            LOGGER.warning(
                "event=llm_retry_scheduled next_attempt=%d "
                "reason=%s delay_ms=%d",
                attempt + 2,
                _llm_error_outcome(exc),
                round(delay_seconds * 1000),
            )
            record_execution_event(
                "llm_retry_scheduled",
                scope="llm",
                stage="llm",
                variant_index=trace_variant_index,
                attempt=attempt + 2,
                status="scheduled",
                outcome_category="retry",
                reason_category=outcome,
                model_identifier=model or settings.LLM_MODEL,
                retry_scheduled=True,
            )
            sleep(delay_seconds)

    raise LLMRequestError("The LLM provider request failed.")


__all__ = [
    "LLMAdapter",
    "LLMAuthenticationError",
    "LLMClient",
    "LLMConfigurationError",
    "LLMError",
    "LLMJSONObject",
    "LLMJSONSchema",
    "LLMMessage",
    "LLMQuotaError",
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
