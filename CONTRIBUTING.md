# Contributing

Digitalisierer is early-stage. Architectural clarity is more valuable than feature count.

## Development

```bash
python -m pip install -e . pytest
pytest -q
PYTHONPATH=src python -m digitalisierer doctor
```

## Pull requests

Prefer small changes with one clear capability or architectural decision.

A change should preserve these invariants:

- source assets are not silently mutated or deleted;
- review state is separate from source identity;
- vendor/engine-specific behavior stays behind adapters;
- network use is explicit;
- derived outputs are intended to be traceable to inputs and parameters.

For a material architectural decision, add or update an ADR.

## Test data

Do not commit private scans, recordings, transcripts or credentials. Use synthetic or redistributable fixtures.
