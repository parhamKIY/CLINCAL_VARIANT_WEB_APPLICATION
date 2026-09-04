"""Security-audit policy for the runtime evidence repository database."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REPOSITORY_PATH = (
    "storage/evidence_repository/evidence_repository.sqlite3"
)


def _load_audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_secrets_audit_policy_test",
        PROJECT_ROOT / "tests" / "run_secrets_audit.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_repository_is_ignored_but_would_still_be_rejected_if_tracked() -> None:
    audit = _load_audit_module()

    ignored = subprocess.run(
        (
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            RUNTIME_REPOSITORY_PATH,
        ),
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert ignored.returncode == 0
    assert audit._is_sensitive_path(RUNTIME_REPOSITORY_PATH) is True


def test_historical_exception_is_exact_and_does_not_allow_other_databases() -> None:
    audit = _load_audit_module()

    assert audit._is_allowed_historical_sensitive_path(
        RUNTIME_REPOSITORY_PATH
    ) is True
    assert audit._is_allowed_historical_sensitive_path(
        "storage/evidence_repository/another.sqlite3"
    ) is False
    assert audit._is_allowed_historical_sensitive_path(
        "storage/database/clinical.sqlite3"
    ) is False


def test_historical_exception_does_not_disable_secret_content_detection() -> None:
    audit = _load_audit_module()

    synthetic_secret = "".join(
        ("OPENAI_", "API_KEY=", "sk-", "proj-", "abcdefghijklmnopqrstuv")
    )
    findings = audit._scan_text(
        synthetic_secret,
        scope="history",
        location=RUNTIME_REPOSITORY_PATH,
    )

    assert {finding.rule for finding in findings} == {
        "openai_api_key",
        "secret_assignment",
    }
