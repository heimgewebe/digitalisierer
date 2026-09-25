from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any


MAX_BYTES = 2_000_000
ALLOWLIST_NAME = ".secret-scan-allowlist.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class ScanFinding:
    rule: str
    path: Path
    line: int | None = None


@dataclass(frozen=True)
class AllowlistEntry:
    path: Path
    sha256: str
    reason: str


RULES = (
    Rule("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    Rule("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    Rule("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    Rule("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    Rule(
        "bearer-token",
        re.compile(r"(?i)\bAuthorization:\s*Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    Rule(
        "openai-key",
        re.compile(
            r"\bsk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{32,})\b"
        ),
    ),
    Rule("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    Rule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)

_SENSITIVE_KEY = (
    r"(?:[A-Za-z_][A-Za-z0-9_.-]*[_.-])?"
    r"(?:password|passwd|passphrase|client[_-]?secret|secret[_-]?access[_-]?key|"
    r"secret[_-]?key|signing[_-]?key|encryption[_-]?key|api[_-]?key|"
    r"access[_-]?token|auth[_-]?token|private[_-]?key|secret)"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?i)(?<![A-Za-z0-9_])"
    rf"(?P<key_quote>['\"]?)(?P<key>{_SENSITIVE_KEY})(?P=key_quote)\s*[:=]\s*"
    r"(?P<value>"
    r"\"(?:\\.|[^\"\\\r\n]){8,}\""
    r"|'(?:\\.|[^'\\\r\n]){8,}'"
    r"|[^\s'\"#;,}\]]{8,}"
    r")"
)

_PLACEHOLDERS = {
    "changeme",
    "example",
    "not-set",
    "not_set",
    "none",
    "null",
    "placeholder",
    "redacted",
    "<redacted>",
}
_ENV_REFERENCE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?\Z")
_DOTTED_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [root / os.fsdecode(item) for item in result.stdout.split(b"\0") if item]


def staged_deleted_files(root: Path) -> set[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=D", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return {
        Path(os.fsdecode(item))
        for item in result.stdout.split(b"\0")
        if item
    }


def _credential_value_is_placeholder(value: str) -> bool:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    normalized = value.strip()
    return (
        normalized.casefold() in _PLACEHOLDERS
        or _ENV_REFERENCE.fullmatch(normalized) is not None
        or (normalized.startswith("{{") and normalized.endswith("}}"))
    )


def _credential_value_is_likely_literal(value: str) -> bool:
    normalized = value.strip()
    if _credential_value_is_placeholder(normalized):
        return False

    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        return True

    # Avoid treating ordinary references, calls, and type expressions as
    # literal credentials merely because the target name is sensitive.
    if any(char in normalized for char in "()[]{}"):
        return False
    if normalized[0].isdigit():
        # A digit-leading token cannot be a Python/shell identifier reference.
        # In a sensitive assignment, treat it as a literal credential candidate.
        return True
    if _DOTTED_IDENTIFIER.fullmatch(normalized) is not None:
        if "." in normalized or "_" in normalized:
            return False
        if any(char.isdigit() for char in normalized):
            return True
        return len(normalized) >= 20

    # Punctuation outside identifier syntax is useful evidence for a literal token.
    return any(char in normalized for char in "-+/=@:~")


def find_matches(text: str) -> list[tuple[str, int]]:
    findings: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append((rule.name, lineno))

        for match in _CREDENTIAL_ASSIGNMENT.finditer(line):
            if _credential_value_is_likely_literal(match.group("value")):
                findings.append(("credential-assignment", lineno))
    return findings


def format_finding(finding: ScanFinding) -> str:
    location = str(finding.path)
    if finding.line is not None:
        location = f"{location}:{finding.line}"
    return (
        f"{location}: potential secret or unscannable tracked file "
        f"({finding.rule})"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_allowlist(root: Path) -> dict[Path, AllowlistEntry]:
    path = root / ALLOWLIST_NAME
    if not path.is_file():
        return {}

    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("secret-scan allowlist must be a schema_version=1 object")
    entries = raw.get("allow_unscannable")
    if not isinstance(entries, list):
        raise ValueError("secret-scan allowlist allow_unscannable must be a list")

    result: dict[Path, AllowlistEntry] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("secret-scan allowlist entry must be an object")
        raw_path = item.get("path")
        raw_sha = item.get("sha256")
        reason = item.get("reason")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or Path(raw_path).is_absolute()
            or ".." in Path(raw_path).parts
        ):
            raise ValueError("secret-scan allowlist path must be a safe relative path")
        if (
            not isinstance(raw_sha, str)
            or _SHA256_RE.fullmatch(raw_sha) is None
        ):
            raise ValueError("secret-scan allowlist sha256 must be lowercase SHA-256")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("secret-scan allowlist reason must not be empty")

        relative = Path(raw_path)
        if relative in result:
            raise ValueError(f"duplicate secret-scan allowlist path: {raw_path}")
        result[relative] = AllowlistEntry(relative, raw_sha, reason.strip())
    return result


def _allowlisted(
    relative: Path,
    sha256: str,
    allowlist: dict[Path, AllowlistEntry],
) -> bool:
    entry = allowlist.get(relative)
    return entry is not None and entry.sha256 == sha256


def scan(root: Path) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    allowlist = load_allowlist(root)
    staged_deleted = staged_deleted_files(root)

    for path in tracked_files(root):
        relative = path.relative_to(root)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if relative in staged_deleted:
                continue
            findings.append(ScanFinding("missing-tracked-file", relative))
            continue
        except OSError:
            findings.append(ScanFinding("unreadable-tracked-file", relative))
            continue

        if stat.S_ISLNK(metadata.st_mode):
            try:
                text = os.readlink(path)
            except OSError:
                findings.append(ScanFinding("unreadable-tracked-symlink", relative))
                continue
            findings.extend(
                ScanFinding(rule, relative, lineno)
                for rule, lineno in find_matches(text)
            )
            continue

        if not stat.S_ISREG(metadata.st_mode):
            # Gitlinks/submodules do not publish the checked-out directory payload
            # as a file blob in this repository.
            continue

        if metadata.st_size > MAX_BYTES:
            try:
                digest = _file_sha256(path)
            except OSError:
                findings.append(ScanFinding("unreadable-tracked-file", relative))
                continue
            if not _allowlisted(relative, digest, allowlist):
                findings.append(ScanFinding("oversized-tracked-file", relative))
            continue

        try:
            raw = path.read_bytes()
        except OSError:
            findings.append(ScanFinding("unreadable-tracked-file", relative))
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            digest = hashlib.sha256(raw).hexdigest()
            if not _allowlisted(relative, digest, allowlist):
                findings.append(ScanFinding("non-utf8-tracked-file", relative))
            continue

        findings.extend(
            ScanFinding(rule, relative, lineno)
            for rule, lineno in find_matches(text)
        )
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        findings = scan(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"secret scan configuration error: {exc}", file=sys.stderr)
        return 2

    if findings:
        for finding in findings:
            print(format_finding(finding), file=sys.stderr)
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())