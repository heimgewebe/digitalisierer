# Digitalisierer

**Digitalisierer** is a local-first workbench for turning physical and digital source material into durable, searchable and machine-usable artifacts.

It is deliberately broader than a scanner frontend:

```
ingest -> preserve -> normalize -> extract -> review -> export -> archive handoff
```

The first vertical slice uses the **CZUR ET24 Pro** on Linux, because that path is already proven end-to-end. The core is media-neutral and is intended to grow into OCR, transcription, document/image/audio/video processing and related digitization workflows.

## What belongs here

Digitalisierer may handle:

- book and document scanning;
- camera and folder ingest;
- PDF import and normalization;
- OCR and searchable PDF generation;
- audio/video ingest;
- speech-to-text transcription;
- subtitle generation (SRT/VTT);
- page/segment ordering, replacement and exclusion;
- dewarp, crop, deskew and other non-destructive preprocessing;
- image/audio/video quality checks;
- blank, duplicate and anomaly detection;
- metadata extraction and technical inspection;
- chapter/session/project assembly;
- reproducible exports, checksums, manifests and provenance;
- optional later enrichment such as layout extraction, structured text extraction or classification.

## Product boundary

Digitalisierer is the **digitization layer**.

It should not become a general-purpose DMS, media player, note-taking system or knowledge base merely because digitized artifacts can feed those systems.

The useful boundary is:

> get material in, preserve the source, extract usable information, make defects visible, let the user correct the result, and emit trustworthy artifacts.

## Initial architecture

Hardware and engines live behind adapters:

```
UI / CLI
   |
Project + Session + Review
   |
Processing / Extraction / QA
   |
Export + Provenance
   |
Ports
   +-- CaptureBackend        -> CZUR first
   +-- OCRBackend            -> OCRmyPDF/Tesseract
   +-- TranscriptionBackend  -> pluggable
   +-- MediaProbeBackend     -> ffprobe
   +-- Storage               -> local filesystem
```

CZUR's proprietary application is an implementation detail of one capture adapter. No proprietary CZUR binary or library belongs in this repository.

## First milestones

1. Scanner session with large review UI.
2. Existing CZUR capture/dewarp integration.
3. Page QA and non-destructive correction.
4. Master PDF + OCR PDF + text + manifest.
5. Audio/video import and transcription.
6. Unified project/session model across media types.

See [docs/vision.md](docs/vision.md), [docs/capabilities.md](docs/capabilities.md), [docs/architecture.md](docs/architecture.md) and [docs/roadmap.md](docs/roadmap.md).
