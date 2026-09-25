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
3. **Review order is export order.** Export-facing asset access uses the reviewed sequence rather than insertion order.
4. **Replacement state is validated.** Asset ids are unique inside a session and an included replacement cannot coexist with its included original.
5. **Every derived artifact should be explainable.** Inputs, parameters, engine/version and output hashes belong in provenance.
6. **No hidden cloud requirement.** Local execution is the default; remote engines must be explicit adapters.
7. **Vendor-specific behavior stays at the edge.** CZUR, Tesseract, Whisper, ffmpeg or another engine must not shape the core domain.
8. **Automation may flag; it must not silently destroy.** Blank/duplicate/low-quality detection produces findings, not deletions.

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

Very early foundation. The repository currently contains the domain/port skeleton, architectural decisions, CI and a `doctor` command. The existing working CZUR/OCR scripts on the development machine are migration input, not a reason to copy implementation accidents into the architecture.

Run:

```bash
PYTHONPATH=src python -m digitalisierer doctor
```

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

Do not commit source scans, recordings, transcripts, secrets, credentials or private datasets. Local digitization data is ignored by default. The CI secret scan also fails closed when a tracked file is too large or not UTF-8, because an unscannable tracked payload must not be reported as clean. See [SECURITY.md](SECURITY.md).

## License

No open-source license has been selected yet. Public repository visibility does **not** grant reuse rights. Licensing is intentionally a separate decision.
