"""Run the deterministic offline Stage 16 MVP acceptance gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

ACCEPTANCE_COMMANDS = (
    (
        "Python compilation",
        (
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "app.py",
            "config.py",
            "backend",
            "frontend",
            "tests",
        ),
    ),
    (
        "Deterministic MVP workflow",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "stage16_mvp",
        ),
    ),
)


def main() -> int:
    """Return success only when the offline MVP workflow passes."""

    for label, command in ACCEPTANCE_COMMANDS:
        print(f"--- {label} ---", flush=True)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            print(
                f"Stage 16 MVP acceptance failed: {label}",
                file=sys.stderr,
            )
            return completed.returncode

    print("Stage 16 deterministic MVP acceptance: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
