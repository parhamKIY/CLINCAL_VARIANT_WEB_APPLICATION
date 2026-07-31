"""Central application logging with bounded files and secret redaction."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from contextvars import ContextVar, Token
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from config import settings


APP_LOGGER_NAME = "clinical_variant_app"
LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "run_id=%(analysis_run_id)s %(message)s"
)
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REDACTED = "[REDACTED]"
SENSITIVE_FIELD_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "client_secret",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
LABELED_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)"
    r"(\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)"
)
BEARER_PATTERN = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"
)
RUN_ID_PATTERN = re.compile(r"run-[0-9a-f]{32}")
_ANALYSIS_RUN_ID: ContextVar[str] = ContextVar(
    "clinical_variant_analysis_run_id",
    default="-",
)


class SecretRedactor:
    """Remove configured and conventionally labelled secrets."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self._secrets = tuple(
            secret
            for secret in secrets
            if isinstance(secret, str) and secret
        )

    def text(self, value: str) -> str:
        """Redact secrets from one string."""

        sanitized = value
        for secret in self._secrets:
            sanitized = sanitized.replace(secret, REDACTED)
        sanitized = LABELED_SECRET_PATTERN.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}{REDACTED}"
            ),
            sanitized,
        )
        return BEARER_PATTERN.sub(
            f"Bearer {REDACTED}",
            sanitized,
        )

    def value(self, value: Any) -> Any:
        """Recursively redact common logging argument containers."""

        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {
                key: (
                    REDACTED
                    if (
                        str(key)
                        .strip()
                        .casefold()
                        .replace("-", "_")
                    )
                    in SENSITIVE_FIELD_NAMES
                    else self.value(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return tuple(self.value(item) for item in value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value


class RedactingFilter(logging.Filter):
    """Sanitize a log record before any handler emits it."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the message template and its interpolation arguments."""

        record.args = self._redactor.value(record.args)
        rendered_message = record.getMessage()
        record.msg = self._redactor.text(rendered_message)
        record.args = ()
        record.analysis_run_id = _ANALYSIS_RUN_ID.get()
        if record.exc_info is not None:
            record.exc_text = None
        return True


class RedactingFormatter(logging.Formatter):
    """Retain exception types while omitting unsafe traceback text."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__(
            LOG_FORMAT,
            datefmt=LOG_DATE_FORMAT,
        )
        self._redactor = redactor
        self.converter = time.gmtime

    def formatException(self, exc_info: Any) -> str:
        """Return only the bounded exception class name."""

        exception_type = exc_info[0]
        return self._redactor.text(exception_type.__name__)


def _resolve_level(level: str | None) -> int:
    """Resolve one configured logging level."""

    level_name = (
        settings.LOG_LEVEL if level is None else level
    )
    if not isinstance(level_name, str):
        raise ValueError("Logging level must be a string.")
    normalized = level_name.strip().upper()
    if normalized not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        raise ValueError(
            "Logging level must be DEBUG, INFO, WARNING, ERROR, "
            "or CRITICAL."
        )
    return logging.getLevelNamesMapping()[normalized]


def _close_handlers(logger: logging.Logger) -> None:
    """Detach and close handlers owned by the application logger."""

    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def configure_logging(
    *,
    level: str | None = None,
    log_path: str | Path | None = None,
    force: bool = False,
    additional_secrets: tuple[str, ...] = (),
) -> logging.Logger:
    """Configure one idempotent console and rotating-file logger."""

    logger = logging.getLogger(APP_LOGGER_NAME)
    if getattr(logger, "_clinical_variant_configured", False):
        if not force:
            return logger
        _close_handlers(logger)
        setattr(logger, "_clinical_variant_configured", False)

    resolved_level = _resolve_level(level)
    resolved_path = Path(
        settings.LOG_PATH if log_path is None else log_path
    ).expanduser().resolve()
    if resolved_path.exists() and resolved_path.is_dir():
        raise ValueError(
            "Logging path must point to a file, not a directory."
        )
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    redactor = SecretRedactor(
        (
            settings.LLM_API_KEY,
            *additional_secrets,
        )
    )
    formatter = RedactingFormatter(redactor)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(resolved_level)
    console_handler.addFilter(RedactingFilter(redactor))
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        resolved_path,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(resolved_level)
    file_handler.addFilter(RedactingFilter(redactor))
    file_handler.setFormatter(formatter)

    logger.setLevel(resolved_level)
    logger.propagate = False
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    setattr(logger, "_clinical_variant_configured", True)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child of the central application logger."""

    normalized = name.strip().strip(".")
    if not normalized:
        return logging.getLogger(APP_LOGGER_NAME)
    return logging.getLogger(f"{APP_LOGGER_NAME}.{normalized}")


def bind_analysis_run_id(run_id: str) -> Token[str]:
    """Bind one application-generated run ID to the current context."""

    if (
        not isinstance(run_id, str)
        or RUN_ID_PATTERN.fullmatch(run_id) is None
    ):
        raise ValueError(
            "Analysis run ID must use the application-generated format."
        )
    return _ANALYSIS_RUN_ID.set(run_id)


def reset_analysis_run_id(token: Token[str]) -> None:
    """Restore the previous analysis logging context."""

    _ANALYSIS_RUN_ID.reset(token)


def shutdown_logging() -> None:
    """Close application-owned handlers."""

    logger = logging.getLogger(APP_LOGGER_NAME)
    _close_handlers(logger)
    setattr(logger, "_clinical_variant_configured", False)


__all__ = [
    "APP_LOGGER_NAME",
    "REDACTED",
    "RedactingFilter",
    "RedactingFormatter",
    "SecretRedactor",
    "bind_analysis_run_id",
    "configure_logging",
    "get_logger",
    "reset_analysis_run_id",
    "shutdown_logging",
]
