"""Run the complete deterministic offline Testing V3 suite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_GROUPS = (
    ("Input", "testing_v3_input"),
    ("Phenotype LLM and HPO", "testing_v3_phenotype"),
    ("Variant interpretation", "testing_v3_interpretation"),
    ("Draft report", "testing_v3_draft_report"),
    ("Selection", "testing_v3_selection"),
    ("Canonical references", "testing_v3_references"),
    ("Final report", "testing_v3_final_report"),
    ("Recovery and retry", "testing_v3_recovery"),
)


def _offline_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("RUN_LIVE_PROVIDER_TESTS", None)
    return environment


def _verify_group_collection(environment: dict[str, str]) -> int:
    for label, marker in REQUIRED_GROUPS:
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                f"stage59_testing_v3 and {marker}",
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            print(completed.stdout, end="", file=sys.stderr)
            print(completed.stderr, end="", file=sys.stderr)
            print(
                f"Testing V3 group is missing or invalid: {label}",
                file=sys.stderr,
            )
            return completed.returncode or 1
        summary = next(
            (
                line
                for line in reversed(completed.stdout.splitlines())
                if "collected" in line
            ),
            "collection passed",
        )
        print(f"--- {label}: {summary} ---", flush=True)
    return 0


def main() -> int:
    """Return success only when every V3 group and offline test passes."""

    environment = _offline_environment()
    group_status = _verify_group_collection(environment)
    if group_status != 0:
        return group_status

    print("--- Complete deterministic Testing V3 suite ---", flush=True)
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "stage59_testing_v3",
            "--cov=config",
            "--cov=backend",
            "--cov=frontend",
            "--cov-report=term",
            "--cov-fail-under=80",
        ),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        print("Stage 59 Testing V3: FAILED", file=sys.stderr)
        return completed.returncode

    print("Stage 59 Testing V3: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
