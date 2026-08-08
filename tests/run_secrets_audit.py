"""Audit the repository and Git history for high-confidence secrets."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_FILE_BYTES = 2_000_000
ALLOWED_ENV_FILES = {".env.example"}
SENSITIVE_SUFFIXES = {
    ".db",
    ".jks",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
}
PRIVATE_DATA_PREFIXES = (
    "storage/database/",
    "storage/logs/",
    "storage/reports/",
    "storage/uploads/",
)
PLACEHOLDER_WORDS = {
    "changeme",
    "dummy",
    "example",
    "placeholder",
    "replace",
    "sample",
    "test",
    "your",
}
SECRET_PATTERNS = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}\b"),
    ),
    (
        "openai_api_key",
        re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{20,}\b"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    ),
)
ENV_SECRET_ASSIGNMENT = re.compile(
    r"(?im)\b(?:"
    r"LLM_API_KEY|OPENAI_API_KEY|GOOGLE_API_KEY|GEMINI_API_KEY|"
    r"ANTHROPIC_API_KEY|AWS_SECRET_ACCESS_KEY|CLIENT_SECRET"
    r")\b\s*[:=]\s*[\"']?([^\s\"'#]{12,})"
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One safe audit finding without the detected secret value."""

    scope: str
    location: str
    rule: str


def _git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run one non-interactive Git query without rendering its content."""

    return subprocess.run(
        ("git", *arguments),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
    )


def _normalize_path(path: str) -> str:
    """Normalize a Git path for deterministic policy checks."""

    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_sensitive_path(path: str) -> bool:
    """Return whether a repository path may contain private material."""

    normalized = _normalize_path(path)
    pure_path = PurePosixPath(normalized)
    name = pure_path.name.casefold()
    if name == ".env" or (
        name.startswith(".env.")
        and name not in ALLOWED_ENV_FILES
    ):
        return True
    if pure_path.suffix.casefold() in SENSITIVE_SUFFIXES:
        return True
    lowered = normalized.casefold()
    return any(
        lowered.startswith(prefix)
        and name not in {".gitkeep", ".keep"}
        for prefix in PRIVATE_DATA_PREFIXES
    )


def _looks_like_placeholder(value: str) -> bool:
    """Allow explicit documentation and test placeholders."""

    normalized = value.casefold()
    return any(word in normalized for word in PLACEHOLDER_WORDS)


def _scan_text(
    text: str,
    *,
    scope: str,
    location: str,
) -> list[Finding]:
    """Return named high-confidence findings without retaining matches."""

    findings = [
        Finding(scope, location, rule)
        for rule, pattern in SECRET_PATTERNS
        if pattern.search(text)
    ]
    if any(
        not _looks_like_placeholder(match.group(1))
        for match in ENV_SECRET_ASSIGNMENT.finditer(text)
    ):
        findings.append(
            Finding(scope, location, "secret_assignment")
        )
    return findings


def _repository_paths() -> tuple[list[str], list[Finding]]:
    """Return tracked and non-ignored untracked repository paths."""

    completed = _git(
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if completed.returncode != 0:
        return [], [
            Finding("repository", "git ls-files", "git_query_failed")
        ]
    deleted = _git("ls-files", "--deleted", "-z")
    if deleted.returncode != 0:
        return [], [
            Finding("repository", "git ls-files", "git_query_failed")
        ]
    deleted_paths = {
        _normalize_path(item.decode("utf-8", errors="replace"))
        for item in deleted.stdout.split(b"\0")
        if item
    }
    paths: list[str] = []
    for item in completed.stdout.split(b"\0"):
        if not item:
            continue
        path = _normalize_path(
            item.decode("utf-8", errors="replace")
        )
        if path not in deleted_paths:
            paths.append(path)
    return paths, []


def _scan_repository_files(paths: list[str]) -> list[Finding]:
    """Scan non-ignored text files and reject sensitive tracked paths."""

    findings: list[Finding] = []
    for path in paths:
        if _is_sensitive_path(path):
            findings.append(
                Finding("repository", path, "sensitive_file_tracked")
            )
            continue
        absolute_path = PROJECT_ROOT / path
        try:
            content = absolute_path.read_bytes()
        except OSError:
            findings.append(
                Finding("repository", path, "file_unreadable")
            )
            continue
        if (
            len(content) > MAX_TEXT_FILE_BYTES
            or b"\0" in content
        ):
            continue
        findings.extend(
            _scan_text(
                content.decode("utf-8", errors="replace"),
                scope="repository",
                location=path,
            )
        )
    return findings


def _scan_history() -> list[Finding]:
    """Scan reachable Git blobs and paths without printing content."""

    findings: list[Finding] = []
    names = _git(
        "log",
        "--all",
        "--name-only",
        "--format=",
    )
    if names.returncode != 0:
        return [
            Finding("history", "git log", "git_query_failed")
        ]
    for raw_path in names.stdout.decode(
        "utf-8",
        errors="replace",
    ).splitlines():
        path = _normalize_path(raw_path)
        if path and _is_sensitive_path(path):
            findings.append(
                Finding("history", path, "sensitive_file_committed")
            )

    objects = _git("rev-list", "--objects", "--all")
    if objects.returncode != 0:
        findings.append(
            Finding("history", "git rev-list", "git_query_failed")
        )
        return findings
    object_paths: dict[str, str] = {}
    for line in objects.stdout.decode(
        "utf-8",
        errors="replace",
    ).splitlines():
        object_id, _, path = line.partition(" ")
        object_paths.setdefault(object_id, _normalize_path(path))

    object_ids = tuple(object_paths)
    checks = subprocess.run(
        (
            "git",
            "cat-file",
            "--batch-check="
            "%(objectname) %(objecttype) %(objectsize)",
        ),
        cwd=PROJECT_ROOT,
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        check=False,
        capture_output=True,
    )
    if checks.returncode != 0:
        findings.append(
            Finding("history", "git cat-file", "git_query_failed")
        )
        return findings
    blob_ids = [
        object_id
        for line in checks.stdout.decode(
            "ascii",
            errors="replace",
        ).splitlines()
        if (
            len(parts := line.split()) == 3
            and (object_id := parts[0])
            and parts[1] == "blob"
            and int(parts[2]) <= MAX_TEXT_FILE_BYTES
        )
    ]
    blobs = subprocess.run(
        ("git", "cat-file", "--batch"),
        cwd=PROJECT_ROOT,
        input=("\n".join(blob_ids) + "\n").encode("ascii"),
        check=False,
        capture_output=True,
    )
    if blobs.returncode != 0:
        findings.append(
            Finding("history", "git cat-file", "git_query_failed")
        )
        return findings

    offset = 0
    while offset < len(blobs.stdout):
        header_end = blobs.stdout.find(b"\n", offset)
        if header_end < 0:
            findings.append(
                Finding(
                    "history",
                    "git cat-file",
                    "git_output_invalid",
                )
            )
            break
        header = blobs.stdout[offset:header_end].decode(
            "ascii",
            errors="replace",
        )
        header_parts = header.split()
        if len(header_parts) != 3:
            findings.append(
                Finding(
                    "history",
                    "git cat-file",
                    "git_output_invalid",
                )
            )
            break
        object_id, object_type, raw_size = header_parts
        size = int(raw_size)
        content_start = header_end + 1
        content_end = content_start + size
        content = blobs.stdout[content_start:content_end]
        offset = content_end + 1
        if object_type != "blob" or b"\0" in content:
            continue
        findings.extend(
            _scan_text(
                content.decode("utf-8", errors="replace"),
                scope="history",
                location=(
                    object_paths.get(object_id)
                    or f"blob {object_id[:12]}"
                ),
            )
        )
    return findings


def audit_repository() -> list[Finding]:
    """Run repository, ignore-policy, and Git-history checks."""

    findings: list[Finding] = []
    ignored_env = _git("check-ignore", "--quiet", ".env")
    if ignored_env.returncode != 0:
        findings.append(
            Finding("policy", ".env", "env_file_not_ignored")
        )

    paths, path_findings = _repository_paths()
    findings.extend(path_findings)
    findings.extend(_scan_repository_files(paths))
    findings.extend(_scan_history())
    return sorted(
        set(findings),
        key=lambda item: (item.scope, item.location, item.rule),
    )


def main() -> int:
    """Return success only when no secret-management finding exists."""

    findings = audit_repository()
    if findings:
        print("Stage 15 secrets audit: FAILED", file=sys.stderr)
        for finding in findings:
            print(
                f"- scope={finding.scope} "
                f"location={finding.location} rule={finding.rule}",
                file=sys.stderr,
            )
        return 1
    print("Stage 15 secrets audit: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
