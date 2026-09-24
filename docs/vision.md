# Vision

## Product thesis

Scanning is only one kind of digitization.

A useful digitization tool should accept a source, preserve it, derive useful representations, expose uncertainty and defects, allow human correction, and produce reproducible outputs.

Digitalisierer therefore treats books, documents, PDFs, images, audio and video as different source kinds in one broader workflow.

## Core workflow

```
ingest
  -> preserve source
  -> normalize
  -> extract
  -> quality analysis
  -> human review
  -> finalize/export
```

Not every source uses every stage. A scanned book may use dewarp and OCR; an interview recording may use audio normalization, transcription and subtitles.

## Core outcomes

A successful run may leave behind:

- immutable or checksummed source assets;
- explicit ordering and inclusion decisions;
- derived working assets;
- OCR text or speech transcripts;
- timestamps/subtitles where relevant;
- machine-generated quality findings;
- human corrections;
- master/export artifacts;
- checksums and a provenance manifest.

## Local-first

The default path works locally without requiring a cloud account.

Cloud or external engines may be added later as explicit adapters. A workflow must not silently upload source material.

## Breadth without a monolith

“Supports many kinds of digitization” must not mean “one enormous application that knows every engine”.

New abilities enter as:

1. a capability with a clear contract;
2. one or more adapters;
3. a workflow profile that composes capabilities;
4. review/export behavior appropriate to that media type.

This keeps the core small enough to reason about while allowing the capability catalogue to grow.

## Evidence-driven growth

The scanner and transcription vertical slices are intentionally different.

Architecture is accepted only insofar as both real workflows fit it without forcing scanner concepts into audio or timeline concepts into documents.

Premature abstractions should be replaced when real use contradicts them.

## Growth examples

Potential adapters/capabilities include:

- scanners and cameras;
- OCR engines;
- transcription engines;
- PDF normalizers;
- audio cleanup;
- layout analysis;
- barcode/QR extraction;
- structured metadata extraction;
- optional AI-assisted enrichment.

The repository can eventually contain many capabilities; the domain core should remain understandable.
