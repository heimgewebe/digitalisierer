# ADR-0004: Processing is composed from capability jobs

- Status: accepted
- Date: 2026-09-24

## Context

The intended scope spans scanning, OCR, transcription, media inspection, quality analysis and export.

A single hard-coded pipeline would become a growing conditional tree. A plugin system with no shared execution contract would produce the opposite problem: many adapters with no reproducibility.

## Decision

Processing steps are modeled conceptually as capability jobs.

A job binds:

- a stable capability id;
- exact inputs;
- parameters;
- one adapter/engine identity;
- outputs;
- findings;
- provenance.

Workflow profiles compose jobs for a user-facing task such as a book chapter or interview transcription.

The implementation may begin synchronously; the contract must allow later background/resumable execution.

## Consequences

Positive:

- scanning and transcription can share orchestration infrastructure;
- alternative engines remain replaceable;
- reruns can be explained and compared;
- failures become bounded to one job.

Negative:

- job identity and provenance need explicit schemas;
- orchestration is slightly more work than calling tools directly.
