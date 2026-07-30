"""Live smoke test for the production provider-neutral LLM boundary."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm import call_llm
from config import settings


def check_configuration() -> None:
    """Validate settings and create the configured application directories."""
    settings.initialize()

    print("Configuration: OK")
    print(f"App name: {settings.APP_NAME}")
    print(f"LLM provider protocol: {settings.LLM_PROVIDER}")
    print(f"LLM base URL: {settings.LLM_BASE_URL}")
    print(f"LLM model: {settings.LLM_MODEL}")
    print("LLM API key: configured")
    print(f"LLM timeout: {settings.LLM_TIMEOUT} seconds")
    print(f"Upload directory: {settings.UPLOAD_DIR}")
    print(f"Report directory: {settings.REPORT_DIR}")
    print(f"Cache directory: {settings.CACHE_DIR}")
    print(f"HPO data directory: {settings.HPO_DATA_DIR}")


def check_llm_connection() -> None:
    """Send one request through the production LLM client."""
    response = call_llm(
        "Follow the user's instruction exactly.",
        "Reply with exactly the word OK.",
        max_tokens=8,
        temperature=0.0,
    )

    print(f"LLM connection: OK ({response.content.strip()})")
    print(f"Response model: {response.model}")


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
