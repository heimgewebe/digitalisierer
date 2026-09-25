# Architecture

## Principle

**Own the workflow; isolate capture devices and processing engines.**

Digitalisierer should not be architecturally shaped around CZUR, Tesseract, Whisper, ffmpeg or any other single implementation.

## 1. Domain model

The core is media-neutral, but not media-blind.

### Asset identity

A `MediaAsset` describes an artifact that exists: path, media kind, role and optionally a content hash.

It does **not** contain mutable review decisions such as “exclude this page from the export”.

### Session membership and review state

A `SessionAsset` associates an asset with a processing session and may carry:

- sequence/order;
- inclusion/exclusion;
- replacement relationship.

`replacement_for` names another `MediaAsset.asset_id` in the same session. Asset ids are unique within a session. An included replacement requires the replaced item to be excluded, and only one included replacement may target a given asset.

Export-facing access uses `ordered_items()` / `ordered_assets()`, so manual review order is not lost when assets are finalized.

This separation matters because the same source may participate in different exports without changing source identity.

### Project and session

A `DigitizationProject` groups related sessions.

A `ProcessingSession` is one bounded workflow such as:

- scan chapter 3;
- OCR an imported PDF;
- transcribe one interview;
- process one video.

## 2. Lifecycle

The default lifecycle is:

```
ingest
  -> preserve
  -> normalize
  -> extract
  -> quality analysis
  -> human review
  -> export
```

Not every workflow needs every stage.

### Ingest

Accept material from scanners, cameras, folders, PDFs, audio/video files or future capture devices.

### Preserve

Record identity and technical facts before interpretation:

- content hash where practical;
- size;
- timestamps;
- media type;
- source path / acquisition context.

Source mutation is forbidden by default.

### Normalize

Produce derived working assets where needed:

- image orientation/crop/dewarp;
- PDF normalization;
- audio normalization;
- video/audio demuxing;
- thumbnails/proxies.

### Extract

Run pluggable extraction capabilities:

- OCR;
- transcription;
- subtitles;
- metadata;
- layout/structure;
- barcode/QR;
- future structured or AI-assisted extraction.

### Quality analysis

Generate explicit findings. Examples:

- blank/near-blank page;
- duplicate/near-duplicate;
- suspicious dimensions;
- blur/exposure;
- OCR confidence;
- silence/noise/clipping;
- missing or duplicated segments.

Per-asset checks use `AssetQualityAnalyzer`. Cross-asset checks such as duplicates, sequence gaps, suspicious relative dimensions, or missing segments use `SessionQualityAnalyzer` so the analyzer can inspect the complete reviewed session context.

A finding is evidence for review, not permission to destroy source data.

### Review

Humans may:

- reorder;
- include/exclude;
- replace/rescan;
- correct OCR/transcripts;
- accept/reject findings.

Review changes session metadata or creates a derived/replacement asset. It does not rewrite source identity.

### Export

Create durable artifacts such as:

- master PDF;
- searchable PDF;
- TXT/Markdown;
- JSON;
- SRT/VTT;
- normalized audio/video;
- manifests and reports.

## 3. Capability jobs

Digitalisierer should not grow into one giant hard-coded pipeline.

A processing step is represented conceptually as a **capability job**:

```
capability id
+ exact input asset ids / hashes
+ parameters
+ adapter / engine identity and version
= derived outputs + findings + provenance
```

Examples:

- `document.ocr`
- `speech.transcribe`
- `media.probe`
- `document.blank-detect`
- `document.export-searchable-pdf`

Workflow profiles compose jobs. “Book chapter” and “Interview transcription” are profiles, not special domain models.

This creates a natural path to:

- resumable/background jobs;
- deterministic reruns;
- cache/reuse later;
- alternative engines;
- explicit failure states.

## 4. Ports and adapters

### CaptureBackend

Device or capture-program integration.

First adapter: CZUR Linux application.

### OCRBackend

Image/PDF text recognition.

First adapter: OCRmyPDF/Tesseract.

### TranscriptionBackend

Speech-to-text engine. The implementation remains pluggable so an existing local ASR authority or another backend can be selected without changing the domain.

### MediaProbeBackend

Technical metadata and stream inspection.

First adapter: ffprobe.

### AssetQualityAnalyzer

Per-asset quality inspection that returns explicit findings.

### SessionQualityAnalyzer

Cross-asset/session quality inspection for comparisons, ordering/gap checks, and other findings that require context beyond one isolated asset.

### Storage

Local filesystem first.

## 5. Provenance

Every finalized derived artifact should be traceable to:

- exact source/derived inputs;
- hashes where practical;
- capability id;
- parameters;
- adapter/engine/version;
- timestamps;
- output hashes;
- relevant human review decisions.

Provenance is part of the product, not debugging exhaust.

## 6. UI

The UI should be task-oriented, not engine-oriented.

Examples:

### Book session

- large preview;
- Scan / Rescan;
- page strip;
- clear findings;
- fast include/exclude/reorder;
- Finish chapter.

### Transcription session

- waveform/timeline later;
- timestamped transcript;
- confidence/findings;
- text correction;
- subtitle export.

### Generic import

- inspect sources;
- select workflow profile;
- observe running jobs;
- review findings;
- finalize outputs.

The UI toolkit is deliberately **not** locked in yet. The first vertical slice should establish interaction requirements before choosing a long-lived desktop/web framework.

## 7. Data layout

A project may contain several sessions and media types:

```
<project>/
  project.json
  assets/
    source/
    derived/
  sessions/
    <session-id>/
      session.json
      review.json
      jobs/
      export/
        <run-id>/
          manifest.json
          report.txt
          ...
```

The exact layout may evolve, but source assets and derived/export artifacts remain distinct.

## 8. Adapter rule

External binaries, proprietary libraries and engine-specific state do not leak into the core domain model.

That rule is what allows a future native ET24 backend, another OCR engine, or several transcription engines to coexist without rewriting the product.
