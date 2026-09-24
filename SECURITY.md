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
