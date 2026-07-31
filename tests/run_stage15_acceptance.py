"""Run the repeatable offline Stage 15 security acceptance gate."""

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
        "Installed dependency consistency",
        (
            sys.executable,
            "-m",
            "pip",
            "check",
        ),
    ),
    (
        "Repository secrets audit",
        (
            sys.executable,
            "tests/run_secrets_audit.py",
        ),
    ),
    (
        "Focused Stage 15 security checks",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "stage15_security",
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
    """Return success only when every offline security check passes."""

    for label, command in ACCEPTANCE_COMMANDS:
        print(f"--- {label} ---", flush=True)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            print(
                f"Stage 15 acceptance failed: {label}",
                file=sys.stderr,
            )
            return completed.returncode

    print("Stage 15 offline security acceptance: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
