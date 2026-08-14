"""Stage 122 dependency-security release-gate tests."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from tools.run_dependency_audit import (
    DependencyAuditError,
    audit_environment,
    load_exceptions,
)


pytestmark = pytest.mark.stage122_dependency_security_gate


def _audit_json(*, vulnerability_id: str = "GHSA-test-0000") -> str:
    return json.dumps(
        [
            {
                "name": "example-package",
                "version": "1.2.3",
                "vulns": [{"id": vulnerability_id}],
            }
        ]
    )


def _exception_file(path: Path, **overrides: object) -> Path:
    record = {
        "advisory_id": "GHSA-test-0000",
        "package": "example-package",
        "version": "1.2.3",
        "rationale": "Reviewed temporary exception for deterministic test coverage.",
        "owner": "security@example.test",
        "expires_on": "2099-01-01",
    }
    record.update(overrides)
    path.write_text(json.dumps({"exceptions": [record]}), encoding="utf-8")
    return path


def test_audit_invokes_pip_audit_against_the_installed_environment(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def runner(*args, **kwargs):
        observed["command"] = args[0]
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, "[]", "")

    assert audit_environment(
        exception_path=tmp_path / "missing.json",
        runner=runner,
    ) == 0
    assert observed["command"][1:] == (
        "-m",
        "pip_audit",
        "--local",
        "--strict",
        "--format",
        "json",
        "--progress-spinner",
        "off",
    )
    assert observed["kwargs"] == {
        "cwd": Path(__file__).resolve().parents[1],
        "capture_output": True,
        "text": True,
        "check": False,
    }


def test_exact_unexpired_exception_is_accepted(tmp_path: Path) -> None:
    exception_path = _exception_file(tmp_path / "exceptions.json")

    def runner(*args, **_kwargs):
        return subprocess.CompletedProcess(args[0], 1, _audit_json(), "")

    assert audit_environment(exception_path=exception_path, runner=runner) == 0


def test_audit_accepts_current_pip_audit_json_envelope(tmp_path: Path) -> None:
    def runner(*args, **_kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps({"dependencies": [], "fixes": []}),
            "",
        )

    assert audit_environment(
        exception_path=tmp_path / "missing.json",
        runner=runner,
    ) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_on": "2026-08-13"},
        {"package": "*"},
        {"version": "*"},
        {"owner": ""},
        {"unexpected": "field"},
    ],
)
def test_expired_unknown_broad_or_malformed_exceptions_fail_closed(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    exception_path = _exception_file(tmp_path / "exceptions.json", **overrides)

    with pytest.raises(DependencyAuditError):
        load_exceptions(exception_path, today=date(2026, 8, 14))


def test_unknown_advisory_exception_fails_closed(tmp_path: Path) -> None:
    exception_path = _exception_file(
        tmp_path / "exceptions.json",
        advisory_id="GHSA-other-0000",
    )

    def runner(*args, **_kwargs):
        return subprocess.CompletedProcess(args[0], 1, _audit_json(), "")

    with pytest.raises(DependencyAuditError, match="does not match"):
        audit_environment(exception_path=exception_path, runner=runner)


def test_audit_does_not_replay_scanner_stderr_or_environment_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "stage122-environment-secret"

    def runner(*args, **_kwargs):
        return subprocess.CompletedProcess(args[0], 2, "", f"token={secret}")

    with pytest.raises(DependencyAuditError):
        audit_environment(exception_path=tmp_path / "missing.json", runner=runner)

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
