# Digitalisierer

**Digitalisierer** is a local-first workbench for turning physical and digital source material into durable, searchable, reviewable and machine-usable artifacts.

It is intentionally broader than a scanner frontend and intentionally narrower than a document-management or knowledge system.

```
ingest -> preserve -> normalize -> extract -> quality -> review -> export
```

The first real vertical slice is the **CZUR ET24 Pro** book workflow on Linux because that path is already proven end-to-end. The architecture itself is media-neutral and is intended to support OCR, transcription, PDF/image processing, audio/video digitization and additional extraction capabilities behind replaceable adapters.

## Why this exists

Digitization is not the same as capture.

A useful workflow must also answer:

- What exactly was ingested?
- Was the source preserved?
- Which transformations were applied?
- Which pages or segments look suspicious?
- What did OCR or transcription actually produce?
- What did a human change?
- Can the result be reproduced from the recorded inputs and parameters?

Digitalisierer makes those concerns first-class.

## Capability direction

Digitalisierer may support:

- book and document scanning;
- camera, folder and PDF ingest;
- OCR and searchable PDF generation;
- transcription of audio and video;
- TXT / Markdown / JSON / SRT / VTT export;
- dewarp, crop, deskew and non-destructive normalization;
- blank, duplicate, blur, clipping, silence and other quality findings;
- metadata and technical inspection;
- chapter/session/project assembly;
- checksums, manifests and provenance;
- optional layout, structure, barcode/QR and AI-assisted extraction.

New functionality should normally enter as a **capability + adapter**, not as a special case in the core.

## Product boundary

Digitalisierer owns the **digitization layer**:

> bring material in, preserve the source, derive useful representations, surface uncertainty and defects, let a human correct the result, and emit trustworthy artifacts.

It is not intended to become a general DMS, media player, note system, library catalogue or RAG/knowledge-base product.

## Architectural invariants

1. **Source identity and review state are separate.** Excluding a page from an export does not mutate the source asset.
2. **Sources are immutable by default.** Corrections create metadata, replacement associations or derived assets.
3. **Review order is export order.** Export-facing asset access uses reviewed sequence rather than insertion order. A replacement with no explicit sequence inherits the position of the asset it replaces, and conflicting effective sequence positions are rejected.
4. **Replacement state is validated.** Asset ids are unique inside a session; replacement targets must exist in that session; replacement cycles are invalid; and an included replacement cannot coexist with its included original.
5. **Quality scope is explicit.** Per-asset analyzers receive one asset. Cross-asset analyzers receive the complete session. A `QualityFinding` may reference zero assets (session-wide), one asset, or several assets.
6. **Every derived artifact should be explainable.** Inputs, parameters, engine/version and output hashes belong in provenance.
7. **No hidden cloud requirement.** Local execution is the default; remote engines must be explicit adapters.
8. **Vendor-specific behavior stays at the edge.** CZUR, Tesseract, Whisper, ffmpeg or another engine must not shape the core domain.
9. **Automation may flag; it must not silently destroy.** Blank/duplicate/low-quality detection produces findings, not deletions.

## Architecture sketch

```
UI / CLI
   |
Project + Session + Review
   |
Capability jobs
   |-- normalize
   |-- OCR
   |-- transcribe
   |-- analyze quality
   |-- export
   |
Ports / adapters
   |-- CaptureBackend          -> CZUR first
   |-- OCRBackend              -> OCRmyPDF / Tesseract
   |-- TranscriptionBackend    -> pluggable
   |-- MediaProbeBackend       -> ffprobe
   |-- AssetQualityAnalyzer    -> per-asset checks
   |-- SessionQualityAnalyzer  -> duplicate/order/gap checks
   |-- Storage                 -> local filesystem
   |
Provenance
   input hashes + parameters + engine identity + output hashes
```

## Current status

The public foundation is in place, and the first transcription integration now exercises the media-neutral capability boundary. The existing CZUR/OCR scripts on the development machine remain migration input rather than architecture.

Transcription reuses the installed Heim-PC `audio.transcribe` authority. Digitalisierer does not install another ASR runtime, pin an engine, create a second model cache or authorize cloud use. It validates the structured transcript contract and owns the resulting export/provenance workflow.

Install development dependencies and run the default readiness check:

```bash
python -m pip install -e ".[dev]"
digitalisierer doctor
```

The default doctor exit status requires the media and OCR capabilities. Require another capability explicitly when a workflow depends on it:

```bash
digitalisierer doctor --require capture-czur
digitalisierer doctor --require transcription
```

The JSON output always reports all known capabilities, while the exit status is determined only by the selected required capabilities. Expensive optional probes are lazy: transcription reports `ready: null` unless it is explicitly required, so the default doctor does not invoke the Heim-PC ASR runtime.

Transcribe one local media file through the canonical local ASR authority:

```bash
digitalisierer transcribe /path/to/recording.m4a
```

The output directory contains `transcript.txt`, `transcript.json`, `manifest.json` and, when complete segment timing is available, `transcript.srt` and `transcript.vtt`. The manifest records the source hash, adapter/authority, provider, engine/model/backend metadata, language when supplied, output hashes and whether cloud processing was used.

## Roadmap

The scanner flow remains the first vertical slice:

```
CZUR capture -> page review -> QA -> master PDF -> OCR -> searchable PDF
```

The second vertical slice is deliberately different:

```
audio/video ingest -> media probe -> audio preparation -> transcription
                  -> transcript review -> TXT/JSON/SRT/VTT
```

If both fit the same project/session/job/provenance core without special pleading, the architecture is doing its job.

See:

- [Vision](docs/vision.md)
- [Capability map](docs/capabilities.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [ADRs](docs/adr)

## Security and privacy

Do not commit source scans, recordings, transcripts, secrets, credentials or private datasets. Local digitization payload directories are ignored by default.

The CI secret scan fails closed when a tracked file cannot be safely inspected, including oversized or non-UTF-8 files. Deliberate binary/large fixtures require an explicit path + SHA-256 + reason entry in `.secret-scan-allowlist.json`; changing the file invalidates that exception. See [SECURITY.md](SECURITY.md).

## License

No open-source license has been selected yet. Public repository visibility does **not** grant reuse rights. Licensing is intentionally a separate decision.
