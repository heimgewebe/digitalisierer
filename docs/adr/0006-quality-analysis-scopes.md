# ADR-0006: Quality analysis has explicit asset and session scopes

- Status: accepted
- Date: 2026-09-25

## Context

Some quality checks are intrinsically local to one asset, such as blur or exposure. Others require comparison or sequence context, such as duplicate/near-duplicate pages, relative-size anomalies, missing segments or sequence gaps.

A single protocol of the form `analyze(asset)` cannot establish cross-asset findings without hidden external state.

## Decision

Quality analysis exposes two explicit ports:

- `AssetQualityAnalyzer.analyze_asset(asset)` for isolated checks;
- `SessionQualityAnalyzer.analyze_session(session)` for checks that require complete session context.

The capability-job/application layer keeps these scopes explicit: it invokes asset analyzers over the relevant reviewed assets and session analyzers once with the complete `ProcessingSession`.

There is deliberately no runtime union/marker `QualityAnalyzer` that callers must introspect to discover which method exists.

## Consequences

- cross-page and cross-segment checks are expressible without hidden mutable state;
- analyzer implementations state their required context in their type;
- orchestration remains deterministic and testable;
- a plugin that needs both scopes may implement both protocols;
- the application layer must keep two analyzer collections or otherwise retain the scope explicitly.
