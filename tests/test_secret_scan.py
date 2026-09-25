from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "secret_scan.py"
_SPEC = spec_from_file_location("digitalisierer_secret_scan_test_subject", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
secret_scan = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = secret_scan
_SPEC.loader.exec_module(secret_scan)


def test_secret_rules_detect_classic_and_fine_grained_github_tokens() -> None:
    classic = "gh" + "p_" + ("A" * 24)
    fine_grained = "github" + "_pat_" + ("B" * 32)

    assert secret_scan.find_matches(classic) == [("github-token", 1)]
    assert secret_scan.find_matches(fine_grained) == [("github-fine-grained-token", 1)]


def test_credential_assignment_supports_unquoted_and_matching_quotes() -> None:
    unquoted = "API_KEY=" + ("a" * 24)
    double_quoted = "pass" + "word=" + '"abc\'defghijk"'
    single_quoted = "sec" + "ret=" + "'abc\"defghijk'"

    assert secret_scan.find_matches(unquoted) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(double_quoted) == [("credential-assignment", 1)]
    assert secret_scan.find_matches(single_quoted) == [("credential-assignment", 1)]


def test_credential_assignment_rejects_mismatched_quotes_and_env_references() -> None:
    mismatched = "pass" + "word=" + "'abcdefghijk\""
    env_reference = "API_KEY=${DIGITALISIERER_API_KEY}"

    assert secret_scan.find_matches(mismatched) == []
    assert secret_scan.find_matches(env_reference) == []


def test_openai_rule_does_not_match_ordinary_sk_kebab_identifier() -> None:
    ordinary = "sk-normalization-profile-default"
    realish = "sk-" + "proj-" + ("A" * 32)

    assert secret_scan.find_matches(ordinary) == []
    assert secret_scan.find_matches(realish) == [("openai-key", 1)]


def test_documentation_placeholders_do_not_trigger() -> None:
    text = "\n".join(
        [
            "Set GITHUB_TOKEN in CI.",
            "Fine-grained tokens begin with github_pat_ followed by redacted data.",
            "Authorization: Bearer <redacted>",
            "pass" + "word=<redacted>",
        ]
    )

    assert secret_scan.find_matches(text) == []


def test_scan_fails_closed_for_oversized_and_non_utf8_files(
    tmp_path: Path, monkeypatch
) -> None:
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"x" * (secret_scan.MAX_BYTES + 1))
    non_utf8 = tmp_path / "legacy.txt"
    non_utf8.write_bytes(b"\xff\xfe\x00\x00")

    monkeypatch.setattr(
        secret_scan,
        "tracked_files",
        lambda root: [oversized, non_utf8],
    )

    findings = secret_scan.scan(tmp_path)

    assert findings == [
        secret_scan.ScanFinding("oversized-tracked-file", Path("oversized.bin")),
        secret_scan.ScanFinding("non-utf8-tracked-file", Path("legacy.txt")),
    ]


def test_credential_finding_does_not_echo_secret() -> None:
    value = "not-a-real-" + "credential"
    text = "pass" + "word=" + repr(value)
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
