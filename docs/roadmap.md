# Roadmap

The roadmap is organized around **vertical slices** that stress different parts of the same core. Feature count is not the optimization target.

## M0 — Public foundation

- [x] broad but bounded digitization scope
- [x] media-neutral asset model
- [x] review state separated from source identity
- [x] adapter boundaries
- [x] capability-job direction
- [x] immutable-source invariant
- [x] provenance as a first-class requirement
- [x] runnable doctor CLI
- [x] public-repository security/privacy guidance
- [x] CI with read-only GitHub permissions

## M1 — Scanner vertical slice

Use the already proven ET24 path to exercise real capture, review and export:

- [ ] create/resume a project and scan session
- [ ] CZUR launch/focus adapter
- [ ] Curved Books preset
- [ ] capture-folder observation
- [ ] thumbnail generation
- [ ] page ordering/exclusion/replacement
- [ ] blank/duplicate/anomaly findings
- [ ] master PDF
- [ ] OCR PDF + text
- [ ] manifest/report with exact input/output hashes
- [ ] large HiDPI review UI
- [ ] migrate the proven local `czur-finalize` behavior into tested adapters/jobs

**Exit criterion:** one real chapter can be captured, corrected and finalized without needing the CZUR UI for review/export.

## M2 — Audio/video transcription slice

Choose a workflow that is structurally different from page scanning:

- [ ] import audio/video
- [ ] ffprobe metadata
- [ ] audio extraction/normalization
- [x] first local transcription adapter (reuses the Heim-PC ASR authority)
- [x] timestamped transcript model with nullable timing/speaker/confidence
- [x] TXT/JSON export plus SRT/VTT when complete timing is supplied
- [ ] confidence/quality findings
- [ ] transcript correction workflow
- [x] provenance records source hash, authority/adapter, engine/model/version, language, parameters and output hashes

**Exit criterion:** one real recording can be imported, transcribed, corrected and exported with no scanner-specific assumptions in the core.

## M3 — Job and project substrate

Build only what M1/M2 prove is necessary:

- [ ] explicit job schema
- [ ] persisted job state
- [ ] failure/retry semantics
- [ ] multiple sessions per project
- [ ] book chapter assembly
- [ ] mixed media assets
- [ ] export profiles
- [ ] provenance viewer
- [ ] optional cache/reuse for deterministic jobs

## M4 — More digitization adapters

Add by demonstrated need:

- [ ] generic UVC/camera ingest
- [ ] SANE scanner adapter
- [ ] PDF import
- [ ] image-folder import
- [ ] alternative OCR engine
- [ ] alternative transcription engine
- [ ] optional explicit remote engines

## M5 — Enrichment

Only where it improves digitization outcomes:

- [ ] layout/structure extraction
- [ ] barcode/QR extraction
- [ ] language detection
- [ ] document classification
- [ ] structured metadata extraction
- [ ] optional AI-assisted cleanup/extraction

## Explicitly outside the core

- general DMS;
- library catalogue;
- e-book/media player;
- note-taking;
- RAG/knowledge-base product;
- cloud sync as a requirement;
- silent destructive cleanup;
- native CZUR laser reverse engineering before a concrete need justifies it.