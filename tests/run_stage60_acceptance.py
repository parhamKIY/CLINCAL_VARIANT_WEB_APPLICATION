"""Run the deterministic post-professor-review V3 release gate."""

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
        (sys.executable, "-m", "pip", "check"),
    ),
    (
        "Repository secrets audit",
        (sys.executable, "tests/run_secrets_audit.py"),
    ),
    (
        "Ten-variant redesigned end-to-end workflow",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "stage60_acceptance",
        ),
    ),
    (
        "Complete deterministic Testing V3 regression gate",
        (sys.executable, "tests/run_stage59_testing_v3.py"),
    ),
)


def main() -> int:
    """Return success only when every V3 release check passes."""

    for label, command in ACCEPTANCE_COMMANDS:
        print(f"--- {label} ---", flush=True)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            print(
                f"Stage 60 acceptance failed: {label}",
                file=sys.stderr,
            )
            return completed.returncode

    print("Stage 60 End-to-End Acceptance Gate V3: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
