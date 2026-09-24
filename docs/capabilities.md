# Capability map

This is a product map, not a promise that every cell is implemented immediately.

| Source | Ingest | Processing / extraction | Typical outputs |
| --- | --- | --- | --- |
| Books / paper | scanner, camera, folder | split, dewarp, deskew, crop, blank/duplicate checks, OCR | source images, master PDF, searchable PDF, TXT/Markdown, manifest |
| Images | folder, camera | rotate, crop, normalize, OCR, metadata, quality checks | normalized image, OCR text, JSON, manifest |
| PDFs | file import | page analysis, OCR, text extraction, normalization | searchable PDF, PDF/A later, TXT, JSON |
| Audio | file, recorder later | normalize, VAD later, transcription, diarization later | WAV/FLAC derivative, TXT, JSON, SRT/VTT |
| Video | file, capture later | probe, audio extraction, transcription, scene/frame extraction later | normalized/proxy video, transcript, subtitles, thumbnails |
| Mixed projects | many sessions | ordering, grouping, review, provenance | project bundle, manifests, assembled exports |

## Cross-cutting capabilities

- immutable-source policy;
- resumable sessions;
- quality findings with confidence/evidence;
- explicit human corrections;
- deterministic export profiles;
- checksums and provenance;
- batch operation;
- CLI and GUI surfaces;
- pluggable local or remote engines.

## Explicitly not automatic

A quality analyzer may mark something as blank, duplicate, suspicious, noisy or low-confidence. It must not silently destroy the source asset.
