# Contributing

Digitalisierer is early-stage. Architectural clarity is more valuable than feature count.

## Development

```bash
python -m pip install -e ".[dev]"
python -m mypy
pytest -q
PYTHONPATH=src python -m digitalisierer doctor
python tools/secret_scan.py
```

## Pull requests

Prefer small changes with one clear capability or architectural decision.

A change should preserve these invariants:

- source assets are not silently mutated or deleted;
- review state is separate from source identity;
- replacement relationships remain valid and export order follows reviewed sequence;
- replacement assets inherit the replaced position unless review explicitly assigns another sequence;
- duplicate effective sequence positions are rejected;
- quality analyzers declare whether they need asset-local or full-session context;
- vendor/engine-specific behavior stays behind adapters;
- network use is explicit;
- derived outputs are intended to be traceable to inputs and parameters.

For a material architectural decision, add or update an ADR.

## Test data

Do not commit private scans, recordings, transcripts or credentials. Use synthetic or redistributable fixtures.

Normal UTF-8 fixtures should be committed normally. If a legitimate tracked fixture is binary, non-UTF-8 or exceeds the secret scanner's inspection bound, add a reviewed path + SHA-256 + reason entry to `.secret-scan-allowlist.json`. Do not weaken the scanner or add broad ignore rules to make such a fixture pass.
