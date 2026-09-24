from pathlib import Path

from tools import secret_scan


def test_secret_rules_detect_classic_and_fine_grained_github_tokens() -> None:
    classic = "gh" + "p_" + ("A" * 24)
    fine_grained = "github" + "_pat_" + ("B" * 32)

    assert secret_scan.find_matches(classic) == [("github-token", 1)]
    assert secret_scan.find_matches(fine_grained) == [("github-fine-grained-token", 1)]


def test_documentation_placeholders_do_not_trigger() -> None:
    text = "\n".join(
        [
            "Set GITHUB_TOKEN in CI.",
            "Fine-grained tokens begin with github_pat_ followed by redacted data.",
            "Authorization: Bearer <redacted>",
        ]
    )

    assert secret_scan.find_matches(text) == []


def test_credential_assignment_is_detected_without_echoing_secret() -> None:
    value = "not-a-real-" + "credential"
    text = "password=" + repr(value)
    findings = secret_scan.find_matches(text)

    assert findings == [("credential-assignment", 1)]
    message = secret_scan.format_finding(findings[0][0], Path("fixture.txt"), findings[0][1])
    assert value not in message
    assert message == "fixture.txt:1: potential secret (credential-assignment)"
