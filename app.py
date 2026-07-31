"""Streamlit entry point for the clinical variant application."""

from backend.logging_config import configure_logging
from config import settings
from frontend.ui import render_app


def main() -> None:
    """Validate runtime configuration before starting the UI."""
    settings.initialize()
    configure_logging()
    render_app()


if __name__ == "__main__":
    main()
