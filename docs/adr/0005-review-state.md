# ADR-0005: Review state is separate from asset identity

- Status: accepted
- Date: 2026-09-24

## Context

The initial scaffold placed fields such as exclusion and replacement directly on media assets.

That makes a source artifact appear to change identity when a user merely changes one session/export decision. It also creates a correctness risk when export code reads insertion order instead of reviewed sequence, or when both an original and its replacement remain active.

## Decision

A `MediaAsset` describes the artifact itself.

Ordering, inclusion/exclusion and replacement relationships belong to the association between an asset and a processing session.

Within one `ProcessingSession`:

- `MediaAsset.asset_id` is unique;
- `replacement_for` references another asset id in the same session;
- an included replacement requires the replaced item to be excluded;
- at most one included replacement may target a given asset;
- export-facing asset access follows reviewed sequence through `ordered_items()` / `ordered_assets()`.

Quality findings reference assets but do not mutate them.

## Consequences

- the same source asset may participate in several sessions/exports;
- source identity remains stable;
- review decisions are auditable and reversible;
- manual reordering is preserved by export-facing access;
- invalid replacement states fail before processing/export;
- session models become slightly more explicit.
