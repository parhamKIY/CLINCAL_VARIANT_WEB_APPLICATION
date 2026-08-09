"""Run the deterministic Stage 78 provider-resilience acceptance gate."""

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
            "tools",
        ),
    ),
    (
        "Repository secrets audit",
        (sys.executable, "tests/run_secrets_audit.py"),
    ),
    (
        "Multi-variant resilience scenario",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "stage78_acceptance",
        ),
    ),
    (
        "Complete offline regression suite",
        (sys.executable, "-m", "pytest", "-q"),
    ),
    (
        "Testing V3 coverage gate",
        (sys.executable, "tests/run_stage59_testing_v3.py"),
    ),
)


def main() -> int:
    for label, command in ACCEPTANCE_COMMANDS:
        print(f"--- {label} ---", flush=True)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            print(
                f"Stage 78 resilience acceptance failed: {label}",
                file=sys.stderr,
            )
            return completed.returncode

    print("Stage 78 Resilience Acceptance Gate: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
