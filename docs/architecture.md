# Architecture

## Principle

**Own the workflow, isolate capture devices and processing engines.**

Digitalisierer should not be architecturally shaped around CZUR, Tesseract, Whisper, ffmpeg or any other single implementation.

## Domain

Core concepts are media-neutral:

- `DigitizationProject`
- `ProcessingSession`
- `SourceAsset`
- `DerivedAsset`
- `QualityFinding`
- `ExportArtifact`
- provenance records

A page is a source asset with sequence semantics. An audio recording is a source asset with time semantics. The core can model both without pretending they are identical.

## Pipeline stages

### Ingest

Accept material from scanners, cameras, folders, PDFs, audio/video files or future capture devices.

### Preserve

Record source identity, checksums, size, timestamps and relevant technical metadata. Source mutation is forbidden by default.

### Normalize

Optional derived processing:

- image orientation/crop/dewarp;
- PDF normalization;
- audio normalization;
- video/audio demuxing;
- thumbnails/proxies.

### Extract

Pluggable extraction:

- OCR;
- speech transcription;
- subtitles;
- metadata;
- layout/structure;
- future structured or AI-assisted extraction.

### Quality analysis

Examples:

- blank or near-blank page;
- duplicate/near-duplicate page;
- suspicious page size;
- blur/exposure later;
- OCR confidence later;
- silence/noise/clipping later;
- missing or duplicated segments.

### Review

Humans may reorder, exclude, replace, rescan, edit transcripts or accept/reject findings without deleting source material.

### Export

Create stable artifacts such as:

- master PDF;
- searchable PDF;
- TXT/Markdown;
- JSON;
- SRT/VTT;
- normalized audio/video;
- manifests and reports.

## Ports

### CaptureBackend

Device or capture-program integration.

First adapter: CZUR Linux application.

### OCRBackend

Image/PDF text recognition.

First adapter: OCRmyPDF/Tesseract.

### TranscriptionBackend

Speech-to-text engine. The initial implementation is intentionally left pluggable so local Whisper/faster-whisper or other engines can be selected later without changing the domain.

### MediaProbeBackend

Technical metadata and stream inspection.

First adapter: ffprobe.

### Storage

Local filesystem first.

## UI

The UI should be task-oriented, not engine-oriented.

Examples:

- **Book session:** large preview, scan/rescan, page strip, findings, finish chapter.
- **Transcription session:** waveform/timeline later, transcript segments, confidence/findings, export subtitles.
- **Import session:** inspect files, choose processing profile, review output.

## Data layout

A project may contain multiple sessions and media types:

```
<project>/
  project.json
  sessions/
    <session-id>/
      session.json
      source/
      derived/
      review/
      export/
        <run-id>/
          manifest.json
          report.txt
          ...
```

Source assets are immutable after ingestion. Corrections create metadata changes or derived/replacement assets.

## Adapter rule

External binaries, proprietary libraries and engine-specific state never leak into the domain model.

That rule is what allows a future native ET24 backend, another OCR engine or a transcription engine to coexist without rewriting the product.
