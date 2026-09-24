# ADR-0005: Review state is separate from asset identity

- Status: accepted
- Date: 2026-09-24

## Context

The initial scaffold placed fields such as exclusion and replacement directly on media assets.

That makes a source artifact appear to change identity when a user merely changes one session/export decision.

## Decision

A `MediaAsset` describes the artifact itself.

Ordering, inclusion/exclusion and replacement relationships belong to the association between an asset and a processing session.

Quality findings reference assets but do not mutate them.

## Consequences

- the same source asset may participate in several sessions/exports;
- source identity remains stable;
- review decisions are auditable and reversible;
- session models become slightly more explicit.
