from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools import secret_scan


def test_secret_rules_detect_classic_and_fine_grained_github_tokens() -> None:
    classic = "gh" + "p_" + ("A" * 24)
    fine_grained = "github" + "_pat_" + ("B" * 32)

    assert secret_scan.find_matches(classic) == [("github-token", 1)]
    assert secret_scan.find_matches(fine_grained) == [("github-fine-grained-token", 1)]


def test_common_vendor_token_shapes_are_detected() -> None:
    slack = "xox" + "b-" + ("A" * 24)
    google = "AI" + "za" + ("B" * 35)

    assert secret_scan.find_matches(slack) == [("slack-token", 1)]
    assert secret_scan.find_matches(google) == [("google-api-key", 1)]


def test_credential_assignment_supports_quoted_keys_values_and_prefixes() -> None:
    key_name = "api_" + "key"
    password_name = "pass" + "word"
    aws_name = "aws_" + "secret_access_key"
    client_name = "client_" + "secret"
    secret_key_name = "SECRET_" + "KEY"
    signing_key_name = "signing_" + "key"
    encryption_key_name = "encryption_" + "key"
    passphrase_name = "pass" + "phrase"

    unquoted = key_name + "=" + ("a" * 24)
    json_style = '"' + key_name + '": "' + ("b" * 24) + '"'
    inner_quote = password_name + '="' + "abc'defghijk" + '"'
    prefixed = aws_name + "=" + ("c" * 40)
    client = client_name + "=" + ("d" * 24)
    case_one = secret_key_name + "=" + ("e" * 32)
    case_two = "DJANGO_" + secret_key_name + "=" + ("f" * 32)
    case_three = signing_key_name + "=" + ("g" * 32)
    case_four = encryption_key_name + "=" + ("h" * 32)
    case_five = passphrase_name + "=" + ("i" * 32)

    assert secret_scan.find_matches(unquoted) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(json_style) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(inner_quote) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(prefixed) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(client) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(case_one) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(case_two) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(case_three) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(case_four) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(case_five) == [("credential-assignment", 1)]


def test_credential_assignment_rejects_mismatched_quotes_and_env_references() -> None:
    password_name = "pass" + "word"
    key_name = "api_" + "key"

    mismatched = password_name + "='" + 'abcdefghijk"'
    env_reference = key_name + "=${DIGITALISIERER_API_KEY}"

    assert secret_scan.find_matches(mismatched) == []
    assert secret_scan.find_matches(env_reference) == []


def test_credential_assignment_ignores_code_references_and_calls() -> None:
    key_name = "api_" + "key"
    password_name = "pass" + "word"
    secret_name = "sec" + "ret"
    client_name = "client_" + "secret"
    auth_name = "auth_" + "token"

    examples = [
        "self." + key_name + " = " + key_name + "_from_config",
        key_name + ' = os.environ["DIGITALISIERER_API_KEY"]',
        key_name + ' = os.getenv("API_KEY")',
        password_name + " = getpass.getpass()",
        secret_name + " = load_secret(path)",
        client_name + "=settings." + client_name,
        auth_name + ": Optional[str] = None",
    ]

    for example in examples:
        assert secret_scan.find_matches(example) == []


def test_unquoted_literal_with_mixed_alphanumeric_content_is_detected() -> None:
    key_name = "api_" + "key"
    value = "abcdef" + "1234567890"

    assert secret_scan.find_matches(key_name + "=" + value) == [
        ("credential-assignment", 1)
    ]


def test_openai_rule_does_not_match_ordinary_sk_kebab_identifier() -> None:
    ordinary = "sk-normalization-profile-default"
    realish = "sk-" + "proj-" + ("A" * 32)

    assert secret_scan.find_matches(ordinary) == []
    assert secret_scan.find_matches(realish) == [("openai-key", 1)]


def test_documentation_placeholders_do_not_trigger() -> None:
    key_name = "api_" + "key"
    password_name = "pass" + "word"
    text = "\n".join(
        [
            "Set GITHUB_TOKEN in CI.",
            "Fine-grained tokens begin with github_pat_ followed by redacted data.",
            "Authorization: Bearer <redacted>",
            password_name + "=<redacted>",
            key_name + "=${EXAMPLE_API_KEY}",
        ]
    )

    assert secret_scan.find_matches(text) == []


def _patch_file_inventory(
    monkeypatch: pytest.MonkeyPatch,
    paths: list[Path],
    staged_deleted: set[Path] | None = None,
) -> None:
    monkeypatch.setattr(secret_scan, "tracked_files", lambda root: paths)
    monkeypatch.setattr(
        secret_scan,
        "staged_deleted_files",
        lambda root: staged_deleted or set(),
    )


def test_scan_fails_closed_for_oversized_and_non_utf8_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"x" * (secret_scan.MAX_BYTES + 1))
    non_utf8 = tmp_path / "legacy.txt"
    non_utf8.write_bytes(b"\xff\xfe\x00\x00")
    _patch_file_inventory(monkeypatch, [oversized, non_utf8])

    findings = secret_scan.scan(tmp_path)

    assert findings == [
        secret_scan.ScanFinding("oversized-tracked-file", Path("oversized.bin")),
        secret_scan.ScanFinding("non-utf8-tracked-file", Path("legacy.txt")),
    ]


def test_hash_bound_allowlist_permits_deliberate_binary_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "fixture.bin"
    payload = b"\xff\xfe\x00\x00"
    binary.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / secret_scan.ALLOWLIST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allow_unscannable": [
                    {
                        "path": "fixture.bin",
                        "sha256": digest,
                        "reason": "synthetic binary test fixture",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _patch_file_inventory(monkeypatch, [binary])

    assert secret_scan.scan(tmp_path) == []


def test_allowlist_is_content_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "fixture.bin"
    binary.write_bytes(b"\xff\xfe\x00\x00")
    (tmp_path / secret_scan.ALLOWLIST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allow_unscannable": [
                    {
                        "path": "fixture.bin",
                        "sha256": "0" * 64,
                        "reason": "old reviewed content",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _patch_file_inventory(monkeypatch, [binary])

    assert secret_scan.scan(tmp_path) == [
        secret_scan.ScanFinding("non-utf8-tracked-file", Path("fixture.bin"))
    ]


def test_missing_file_is_ignored_only_when_staged_for_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "removed.txt"
    _patch_file_inventory(
        monkeypatch,
        [missing],
        staged_deleted={Path("removed.txt")},
    )

    assert secret_scan.scan(tmp_path) == []

    _patch_file_inventory(monkeypatch, [missing])
    assert secret_scan.scan(tmp_path) == [
        secret_scan.ScanFinding("missing-tracked-file", Path("removed.txt"))
    ]


def test_credential_finding_does_not_echo_secret() -> None:
    value = "not-a-real-" + "credential"
    text = ("pass" + "word") + "=" + repr(value)
    matches = secret_scan.find_matches(text)

    assert matches == [("credential-assignment", 1)]
    finding = secret_scan.ScanFinding(
        matches[0][0],
        Path("fixture.txt"),
        matches[0][1],
    )
    message = secret_scan.format_finding(finding)
    assert value not in message
    assert message == (
        "fixture.txt:1: potential secret or unscannable tracked file "
        "(credential-assignment)"
    )