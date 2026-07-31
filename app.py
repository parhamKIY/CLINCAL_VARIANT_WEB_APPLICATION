"""Streamlit entry point for the clinical variant application."""

from backend.logging_config import configure_logging
from frontend.ui import render_app


if __name__ == "__main__":
    configure_logging()
    render_app()
