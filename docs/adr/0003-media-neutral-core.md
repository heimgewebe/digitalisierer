# ADR-0003: Core domain is media-neutral

- Status: accepted
- Date: 2026-09-24

## Context

The repository is intended to handle OCR and transcription in addition to scanning.

A page-centric model would force audio/video into unnatural abstractions. A transcription-centric model would have the inverse problem.

## Decision

The core models generic source and derived assets plus media-specific metadata.

Page order, time ranges, OCR blocks and transcript segments belong to capability-specific structures layered on top of generic assets.

## Consequences

- scanner and transcription workflows can share projects, jobs, findings, provenance and export infrastructure;
- adapters can evolve independently;
- media-specific review UIs remain possible without contaminating the whole domain.
