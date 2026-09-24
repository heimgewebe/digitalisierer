# ADR-0001: Digitalisierer owns the digitization workflow

- Status: accepted
- Date: 2026-09-24

## Context

A scanner-only product is too narrow: useful digitization also includes OCR, transcription, correction, quality checks and reproducible exports.

An unconstrained “do everything with documents/media” product is too broad and would become a DMS or knowledge platform.

## Decision

Digitalisierer owns this bounded lifecycle:

`ingest -> preserve -> normalize -> extract -> review -> export -> archival handoff`.

Supported source kinds may include paper, images, PDFs, audio and video.

## Consequences

Positive:

- one place for scanning, OCR, transcription and related digitization;
- reusable quality/provenance infrastructure;
- engines and devices remain replaceable;
- projects may combine multiple media types.

Negative:

- the domain model must handle sequence-based and time-based media;
- background processing and session state become important early.

## Boundary

The core ends at trustworthy digitized artifacts and their provenance. General document management, reading, note-taking and knowledge-base behavior are separate concerns.
