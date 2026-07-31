"""Run the repeatable offline Stage 13 acceptance gate."""

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
        "Critical regression suite",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "regression",
        ),
    ),
    (
        "Complete suite and coverage gate",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--cov=config",
            "--cov=backend",
            "--cov=frontend",
            "--cov-report=term",
            "--cov-fail-under=80",
        ),
    ),
)


def main() -> int:
    """Return success only when every offline acceptance check passes."""

    for label, command in ACCEPTANCE_COMMANDS:
        print(f"--- {label} ---", flush=True)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            print(
                f"Stage 13 acceptance failed: {label}",
                file=sys.stderr,
            )
            return completed.returncode

    print("Stage 13 offline acceptance: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
