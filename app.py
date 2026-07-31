"""Streamlit entry point for the clinical variant application."""

from pathlib import Path
import subprocess
import sys
from typing import Sequence

from streamlit.runtime import exists as streamlit_runtime_exists

from backend.logging_config import configure_logging
from config import settings
from frontend.ui import render_app


def streamlit_command(arguments: Sequence[str] = ()) -> list[str]:
    """Build the command used when this file is run as a normal script."""

    app_path = Path(__file__).resolve()
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        *arguments,
    ]


def launch_streamlit(arguments: Sequence[str] = ()) -> int:
    """Launch this entry point through Streamlit."""

    app_path = Path(__file__).resolve()
    try:
        completed = subprocess.run(
            streamlit_command(arguments),
            cwd=app_path.parent,
            check=False,
        )
    except KeyboardInterrupt:
        return 130
    return completed.returncode


def main() -> None:
    """Validate runtime configuration before starting the UI."""
    settings.initialize()
    configure_logging()
    render_app()


if __name__ == "__main__":
    if streamlit_runtime_exists():
        main()
    else:
        raise SystemExit(launch_streamlit(sys.argv[1:]))
