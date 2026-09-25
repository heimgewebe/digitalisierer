from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys


MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class ScanFinding:
    rule: str
    path: Path
    line: int | None = None


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
)

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*"
    r"(?P<value>"
    r"\"(?:\\.|[^\"\\\r\n]){8,}\""
    r"|'(?:\\.|[^'\\\r\n]){8,}'"
    r"|[^\s'\"#;]{8,}"
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


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [root / os.fsdecode(item) for item in result.stdout.split(b"\0") if item]


def _credential_value_is_placeholder(value: str) -> bool:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    normalized = value.strip()
    return (
        normalized.casefold() in _PLACEHOLDERS
        or _ENV_REFERENCE.fullmatch(normalized) is not None
        or (normalized.startswith("{{") and normalized.endswith("}}"))
    )


def find_matches(text: str) -> list[tuple[str, int]]:
    findings: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append((rule.name, lineno))

        for match in _CREDENTIAL_ASSIGNMENT.finditer(line):
            if not _credential_value_is_placeholder(match.group("value")):
                findings.append(("credential-assignment", lineno))
    return findings


def format_finding(finding: ScanFinding) -> str:
    location = str(finding.path)
    if finding.line is not None:
        location = f"{location}:{finding.line}"
    return f"{location}: potential secret or unscannable tracked file ({finding.rule})"


def scan(root: Path) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for path in tracked_files(root):
        try:
            metadata = path.stat()
        except OSError:
            findings.append(ScanFinding("unreadable-tracked-file", path.relative_to(root)))
            continue

        if not path.is_file():
            continue
        if metadata.st_size > MAX_BYTES:
            findings.append(ScanFinding("oversized-tracked-file", path.relative_to(root)))
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(ScanFinding("non-utf8-tracked-file", path.relative_to(root)))
            continue
        except OSError:
            findings.append(ScanFinding("unreadable-tracked-file", path.relative_to(root)))
            continue

        findings.extend(
            ScanFinding(rule, path.relative_to(root), lineno)
            for rule, lineno in find_matches(text)
        )
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan(root)
    if findings:
        for finding in findings:
            print(format_finding(finding), file=sys.stderr)
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
