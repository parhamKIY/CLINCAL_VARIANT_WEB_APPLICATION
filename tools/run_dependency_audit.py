"""Fail-closed vulnerability audit for the resolved Python environment."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXCEPTION_PATH = PROJECT_ROOT / "docs" / "dependency_audit_exceptions.json"
_PACKAGE_NAME_PATTERN = re.compile(r"[-_.]+")
_CANONICAL_PACKAGE_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class DependencyAuditError(RuntimeError):
    """Raised when audit output or an exception record is unsafe to use."""


@dataclass(frozen=True, slots=True)
class Vulnerability:
    """One installed-package vulnerability reported by pip-audit."""

    advisory_id: str
    package: str
    version: str


@dataclass(frozen=True, slots=True)
class AuditException:
    """One reviewed, exact, time-bounded vulnerability exception."""

    advisory_id: str
    package: str
    version: str
    rationale: str
    owner: str
    expires_on: date


def _normalized_package_name(value: str) -> str:
    return _PACKAGE_NAME_PATTERN.sub("-", value.strip().casefold())


def _required_text(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DependencyAuditError(f"Exception {field} must be a non-empty string.")
    return value.strip()


def load_exceptions(path: Path, *, today: date | None = None) -> set[AuditException]:
    """Load exact, non-expired exceptions or fail closed."""

    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyAuditError("Dependency audit exceptions are unreadable.") from exc
    if not isinstance(payload, dict) or set(payload) != {"exceptions"}:
        raise DependencyAuditError("Exception file must contain only an exceptions list.")
    records = payload["exceptions"]
    if not isinstance(records, list) or not records:
        raise DependencyAuditError("Exception file must contain a non-empty exceptions list.")

    current_date = today or date.today()
    exceptions: set[AuditException] = set()
    identities: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise DependencyAuditError("Each dependency exception must be an object.")
        if set(record) != {
            "advisory_id",
            "package",
            "version",
            "rationale",
            "owner",
            "expires_on",
        }:
            raise DependencyAuditError("Dependency exception has unsupported fields.")
        advisory_id = _required_text(record, "advisory_id")
        package = _normalized_package_name(_required_text(record, "package"))
        version = _required_text(record, "version")
        if not _CANONICAL_PACKAGE_PATTERN.fullmatch(package):
            raise DependencyAuditError("Exception package must name one exact package.")
        if version == "*" or any(character.isspace() for character in version):
            raise DependencyAuditError("Exception version must name one exact version.")
        rationale = _required_text(record, "rationale")
        owner = _required_text(record, "owner")
        try:
            expires_on = date.fromisoformat(_required_text(record, "expires_on"))
        except ValueError as exc:
            raise DependencyAuditError("Exception expiry date must use YYYY-MM-DD.") from exc
        if expires_on < current_date:
            raise DependencyAuditError("Dependency audit exception has expired.")
        identity = (advisory_id, package, version)
        if identity in identities:
            raise DependencyAuditError("Dependency audit exceptions must be unique.")
        identities.add(identity)
        exceptions.add(
            AuditException(
                advisory_id=advisory_id,
                package=package,
                version=version,
                rationale=rationale,
                owner=owner,
                expires_on=expires_on,
            )
        )
    return exceptions


def parse_audit_output(output: str) -> list[Vulnerability]:
    """Validate pip-audit JSON without replaying scanner output."""

    try:
        records = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DependencyAuditError("Dependency scanner returned invalid JSON.") from exc
    if isinstance(records, dict):
        if set(records) != {"dependencies", "fixes"}:
            raise DependencyAuditError("Dependency scanner JSON has unsupported fields.")
        records = records["dependencies"]
    if not isinstance(records, list):
        raise DependencyAuditError("Dependency scanner JSON must contain dependencies.")

    vulnerabilities: list[Vulnerability] = []
    for record in records:
        if not isinstance(record, dict):
            raise DependencyAuditError("Dependency scanner returned an invalid package record.")
        name = record.get("name")
        version = record.get("version")
        findings = record.get("vulns")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(version, str)
            or not version.strip()
            or not isinstance(findings, list)
        ):
            raise DependencyAuditError("Dependency scanner returned an incomplete package record.")
        for finding in findings:
            if not isinstance(finding, dict):
                raise DependencyAuditError("Dependency scanner returned an invalid vulnerability.")
            advisory_id = finding.get("id")
            if not isinstance(advisory_id, str) or not advisory_id.strip():
                raise DependencyAuditError("Dependency scanner returned an unnamed vulnerability.")
            vulnerabilities.append(
                Vulnerability(
                    advisory_id=advisory_id.strip(),
                    package=_normalized_package_name(name),
                    version=version.strip(),
                )
            )
    return vulnerabilities


def _exception_identities(exceptions: set[AuditException]) -> set[tuple[str, str, str]]:
    return {
        (exception.advisory_id, exception.package, exception.version)
        for exception in exceptions
    }


def audit_environment(
    *,
    exception_path: Path = DEFAULT_EXCEPTION_PATH,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Audit installed local packages and allow only exact reviewed exceptions."""

    exceptions = load_exceptions(exception_path)
    command: Sequence[str] = (
        sys.executable,
        "-m",
        "pip_audit",
        "--local",
        "--strict",
        "--format",
        "json",
        "--progress-spinner",
        "off",
    )
    try:
        completed = runner(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DependencyAuditError("Dependency scanner could not be started.") from exc
    if completed.returncode not in {0, 1}:
        raise DependencyAuditError("Dependency scanner did not complete successfully.")

    vulnerabilities = parse_audit_output(completed.stdout)
    exception_identities = _exception_identities(exceptions)
    unapproved = [
        finding
        for finding in vulnerabilities
        if (finding.advisory_id, finding.package, finding.version)
        not in exception_identities
    ]
    used_exception_identities = {
        (finding.advisory_id, finding.package, finding.version)
        for finding in vulnerabilities
        if (finding.advisory_id, finding.package, finding.version)
        in exception_identities
    }
    unused_exceptions = exception_identities - used_exception_identities
    if unused_exceptions:
        raise DependencyAuditError("Dependency audit exception does not match an installed vulnerability.")
    if unapproved:
        for finding in unapproved:
            print(
                f"VULNERABLE {finding.package}=={finding.version} {finding.advisory_id}",
                file=sys.stderr,
            )
        return 1

    print(
        "Dependency vulnerability audit: PASSED "
        f"({len(vulnerabilities)} known vulnerabilities, "
        f"{len(used_exception_identities)} approved exact exceptions)."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the release-gate dependency vulnerability audit."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=DEFAULT_EXCEPTION_PATH,
        help="Optional reviewed exception file; absent means no exceptions.",
    )
    arguments = parser.parse_args(argv)
    try:
        return audit_environment(exception_path=arguments.exceptions)
    except DependencyAuditError as exc:
        print(f"Dependency vulnerability audit: FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
