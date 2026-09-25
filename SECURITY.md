# Security

## Scope

Digitalisierer processes potentially sensitive local documents, images, audio, video and transcripts. The project therefore treats accidental data publication as a first-class risk.

## Reporting

If the repository supports GitHub private vulnerability reporting, use it for security-sensitive reports.

Do not put secrets, credentials, private source material, personal documents or exploitable details into a public issue.

If private reporting is unavailable, open a minimal public issue asking for a private reporting channel without including sensitive details.

## Development rules

- Never commit real scans, recordings or transcripts used for private work.
- Never commit API keys, tokens, cookies, SSH material or credentials.
- Keep proprietary vendor binaries and libraries outside the repository.
- Remote/cloud processing must be explicit; local-first is the default.
- Test fixtures should be synthetic, public-domain or otherwise safe to redistribute.

## Secret-scan policy

CI runs `tools/secret_scan.py` over tracked files.

Text files are checked for common token/key shapes and credential assignments. A tracked file that cannot be safely inspected is **not** reported as clean: oversized, non-UTF-8, missing or unreadable tracked files fail the scan.

Legitimate binary or large fixtures may be admitted only through `.secret-scan-allowlist.json`. Each entry must contain:

- the exact repository-relative path;
- the exact lowercase SHA-256 of the reviewed file;
- a non-empty reason.

The exception is content-bound: changing the file changes its hash and invalidates the allowlist entry. The allowlist is for intentionally unscannable content, not for suppressing a detected textual secret.

A tracked file staged for deletion is ignored when it is absent from the worktree; an unexpectedly missing tracked file remains a failure.
