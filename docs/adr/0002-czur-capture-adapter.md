# ADR-0002: Keep CZUR as an isolated capture adapter

- Status: accepted
- Date: 2026-09-24

## Context

The ET24 Pro exposes a usable Linux UVC path, while the official application provides device-specific book processing and laser/dewarp behavior that is already working.

Reimplementing that proprietary behavior now would delay the user-facing product.

## Decision

The first scanner backend wraps the official CZUR application behind the generic `CaptureBackend` port.

The adapter may use configured hotkeys, window automation, output-folder observation and device health checks.

No proprietary CZUR binary or library is committed to this repository.

## Consequences

- the first vertical slice can be useful quickly;
- CZUR-specific fragility is contained;
- a later native backend can replace it without changing project/session/review/export concepts.
