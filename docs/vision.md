# Vision

## Product thesis

Scanning is only one kind of digitization.

A useful digitization tool should accept a source, preserve it, derive useful representations, expose uncertainty and defects, and produce reproducible outputs.

Digitalisierer therefore treats books, documents, PDFs, images, audio and video as different source kinds in the same broader workflow.

## Core workflow

```
ingest
  -> preserve source
  -> normalize
  -> extract
  -> quality analysis
  -> human review
  -> finalize
  -> archival handoff
```

Not every source uses every stage. A scanned book may use dewarp and OCR; an interview recording may use audio normalization, transcription and subtitles.

## Core outcomes

A successful run may leave behind:

- immutable or checksummed source assets;
- explicit ordering and exclusions;
- derived working assets;
- OCR text or speech transcript;
- timestamps/subtitles where relevant;
- machine-generated quality findings;
- human corrections;
- master/export artifacts;
- checksums and a provenance manifest.

## Local-first

The default path should work locally without requiring a cloud account.

Cloud or external engines may be added later as optional adapters, never as a hidden requirement.

## Growth model

New capabilities should enter as adapters or processing stages rather than by expanding one vendor-specific workflow.

Examples:

- scanners and cameras;
- OCR engines;
- transcription engines;
- PDF normalizers;
- audio cleanup;
- layout analysis;
- barcode/QR extraction;
- structured metadata extraction;
- optional AI-assisted enrichment.

The core should remain understandable even as the adapter catalogue grows.
