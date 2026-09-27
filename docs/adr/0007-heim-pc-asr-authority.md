# ADR 0007 — Reuse the generic heimgewebe/asr authority

Status: accepted

## Context

Digitalisierer needs transcription as a media-neutral extraction capability, but a reviewed local ASR subsystem already exists in `heimgewebe/asr`. The installed
operator-entry contract exposes the `audio.transcribe` capability through the
`heimgewebe_asr_open_engine` authority. That authority resolves engine policy at
execution time and owns the shared runtime/model cache.

Duplicating faster-whisper, model selection, caches or cloud-routing policy inside
Digitalisierer would create two authorities for the same capability.

## Decision

Digitalisierer consumes the installed `audioTranscription` capability locator and
provides a `HeimgewebeAsrBackend` adapter.

The adapter:

- requires authority `heimgewebe_asr_open_engine`;
- requires policy resolution at execution time;
- rejects consumer-side engine pinning and locator-authorized metered/cloud use;
- invokes the authority as `route --audio <source> --json` without engine or cloud
  flags;
- accepts only a local-first route result with `cloud_used=false`;
- validates the external `heimgewebe.asr-transcript` contract before mapping it into
  Digitalisierer domain objects;
- leaves unavailable language, timestamps, speakers and confidence unset instead of
  inventing them.

Digitalisierer owns the workflow after inference: domain mapping, review-facing
representation, TXT/JSON/SRT/VTT export and provenance. SRT/VTT are emitted only
when every exported segment has timestamps.

CI does not execute the real ASR runtime. Unit and integration tests use synthetic
contract payloads and a fake transcription backend. A real Heim-PC recording is an
explicit dogfood gate outside the public repository.

## Consequences

- The ASR engine/model can change through the `heimgewebe/asr` policy without changing
  Digitalisierer.
- Digitalisierer does not create a second model cache or Python ASR runtime.
- Automatic cloud escalation remains impossible from this adapter.
- Running transcription requires the installed operator-entry capability projection
  and a ready `heimgewebe/asr` authority.
- Transcript correction, richer media probing/normalization and quality findings are
  separate workflow capabilities, not responsibilities of the ASR adapter.