"""Temporary manual smoke test for .env, config.py, and the LLM endpoint."""

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings


def _redact_secrets(text: str) -> str:
    """Remove the configured API key from diagnostic output."""
    return text.replace(settings.LLM_API_KEY, "<redacted>")


def check_configuration() -> None:
    """Validate settings and create the configured application directories."""
    settings.initialize()

    print("Configuration: OK")
    print(f"App name: {settings.APP_NAME}")
    print(f"LLM base URL: {settings.LLM_BASE_URL}")
    print(f"LLM model: {settings.LLM_MODEL}")
    print("LLM API key: configured")
    print(f"Request timeout: {settings.REQUEST_TIMEOUT} seconds")
    print(f"Upload directory: {settings.UPLOAD_DIR}")
    print(f"Report directory: {settings.REPORT_DIR}")
    print(f"Cache directory: {settings.CACHE_DIR}")
    print(f"HPO data directory: {settings.HPO_DATA_DIR}")


def check_llm_connection() -> None:
    """Send one minimal request to an OpenAI-compatible chat endpoint."""
    endpoint = f"{settings.LLM_BASE_URL}/chat/completions"
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": "Reply with exactly the word OK.",
            }
        ],
        "max_tokens": 8,
        "temperature": 0,
    }
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=settings.REQUEST_TIMEOUT) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        safe_body = _redact_secrets(error_body)
        raise RuntimeError(
            f"LLM request failed with HTTP {exc.code}: {safe_body}"
        ) from exc
    except URLError as exc:
        safe_reason = _redact_secrets(str(exc.reason))
        raise RuntimeError(
            f"Could not connect to the LLM endpoint: {safe_reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "The LLM endpoint returned a non-JSON response."
        ) from exc

    try:
        content = response_data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            "The LLM endpoint returned an unexpected response structure."
        ) from exc

    if not content:
        raise RuntimeError("The LLM endpoint returned an empty response.")

    print(f"LLM connection: OK ({content})")


def main() -> int:
    """Run configuration validation and one live LLM request."""
    try:
        check_configuration()
        if "--config-only" not in sys.argv:
            check_llm_connection()
    except RuntimeError as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1

    print("Smoke test: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
