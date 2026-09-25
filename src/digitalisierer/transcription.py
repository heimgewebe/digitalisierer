from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import secrets
import shutil

from .domain import ExportArtifact, TranscriptSegment, TranscriptionResult
from .ports import TranscriptionBackend


class TranscriptionWorkflowError(RuntimeError):
    """Raised when a transcription session cannot be finalized safely."""



@dataclass(frozen=True, slots=True)
class TranscriptionExport:
    output_dir: Path
    artifacts: tuple[ExportArtifact, ...]


def default_output_dir(source: Path) -> Path:
    source_path = source.expanduser()
    return source_path.parent / f"{source_path.stem}.digitalisierer-transcript"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _stable_source_hash(path: Path) -> tuple[str, os.stat_result]:
    before = path.stat()
    digest = _sha256_file(path)
    after = path.stat()
    if _stat_identity(before) != _stat_identity(after):
        raise TranscriptionWorkflowError(
            "transcription source changed while hashing"
        )
    return digest, after


def _json_text(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _segment_payload(segment: TranscriptSegment) -> dict[str, object]:
    return {
        "text": segment.text,
        "start": segment.start,
        "end": segment.end,
        "speaker": segment.speaker,
        "confidence": segment.confidence,
    }


def _all_segments_timed(result: TranscriptionResult) -> bool:
    segments = result.transcript.segments
    return bool(segments) and all(
        segment.start is not None and segment.end is not None
        for segment in segments
    )


def _timestamp(seconds: float, *, separator: str) -> str:
    scaled_ms = seconds * 1000.0
    if not math.isfinite(scaled_ms):
        raise TranscriptionWorkflowError(
            "subtitle timestamp exceeds the supported range"
        )
    try:
        total_ms = max(0, round(scaled_ms))
    except (OverflowError, ValueError) as exc:
        raise TranscriptionWorkflowError(
            "subtitle timestamp exceeds the supported range"
        ) from exc
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def _subtitle_text(segment: TranscriptSegment) -> str:
    raw = (
        segment.text
        if segment.speaker is None
        else f"{segment.speaker}: {segment.text}"
    )
    single_line = " ".join(
        part.strip()
        for part in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if part.strip()
    )
    return html.escape(single_line, quote=False)


def _srt(result: TranscriptionResult) -> str:
    lines: list[str] = []
    for index, segment in enumerate(result.transcript.segments, start=1):
        assert segment.start is not None
        assert segment.end is not None
        lines.extend(
            [
                str(index),
                f"{_timestamp(segment.start, separator=',')} --> "
                f"{_timestamp(segment.end, separator=',')}",
                _subtitle_text(segment),
                "",
            ]
        )
    return "\n".join(lines)


def _vtt(result: TranscriptionResult) -> str:
    lines = ["WEBVTT", ""]
    for segment in result.transcript.segments:
        assert segment.start is not None
        assert segment.end is not None
        lines.extend(
            [
                f"{_timestamp(segment.start, separator='.')} --> "
                f"{_timestamp(segment.end, separator='.')}",
                _subtitle_text(segment),
                "",
            ]
        )
    return "\n".join(lines)


def _write_artifact(
    root: Path,
    *,
    name: str,
    kind: str,
    content: str,
) -> ExportArtifact:
    path = root / name
    path.write_text(content, encoding="utf-8")
    return ExportArtifact(kind=kind, path=path, sha256=_sha256_file(path))


FileIdentity = tuple[int, int]
FileFingerprint = tuple[FileIdentity, str]


def _path_identity(path: Path) -> FileIdentity:
    stat = path.stat(follow_symlinks=False)
    return (stat.st_dev, stat.st_ino)


def _path_fingerprint(path: Path) -> FileFingerprint:
    return (_path_identity(path), _sha256_file(path))


def _path_matches_fingerprint(path: Path, fingerprint: FileFingerprint) -> bool:
    try:
        identity, sha256 = fingerprint
        return _path_identity(path) == identity and _sha256_file(path) == sha256
    except FileNotFoundError:
        return False


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _prepare_staging_dir(final_dir: Path) -> Path:
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if _path_exists(final_dir):
        raise TranscriptionWorkflowError(
            f"output directory already exists: {final_dir}"
        )

    for _ in range(16):
        staging_dir = final_dir.parent / (
            f".{final_dir.name}.staging-{secrets.token_hex(8)}"
        )
        try:
            staging_dir.mkdir(mode=0o700)
            return staging_dir
        except FileExistsError:
            continue
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
    raise TranscriptionWorkflowError(
        f"unable to reserve staging directory for: {final_dir}"
    )


def _verify_published_artifacts(
    final_dir: Path,
    published: dict[Path, FileFingerprint],
) -> None:
    expected_names = {destination.name for destination in published}
    actual_names = {entry.name for entry in final_dir.iterdir()}
    if actual_names != expected_names:
        raise TranscriptionWorkflowError(
            "output directory changed during publication"
        )
    for destination, fingerprint in published.items():
        if not _path_matches_fingerprint(destination, fingerprint):
            raise TranscriptionWorkflowError(
                f"output directory changed during publication: {destination}"
            )


def _cleanup_failed_publication(
    final_dir: Path,
    staging_dir: Path,
    published: dict[Path, FileFingerprint],
    *,
    final_dir_created: bool,
) -> None:
    shutil.rmtree(staging_dir, ignore_errors=True)

    # Once any artifact reached final_dir, failure cleanup must be
    # non-destructive: leave the partial state for explicit inspection.
    if published or not final_dir_created:
        return

    try:
        remaining = list(final_dir.iterdir())
    except FileNotFoundError:
        return
    if remaining:
        return
    try:
        final_dir.rmdir()
    except OSError:
        pass


def _publish_staged_artifacts(
    final_dir: Path,
    staging_dir: Path,
    artifacts: list[ExportArtifact],
) -> None:
    published: dict[Path, FileFingerprint] = {}
    final_dir_created = False
    try:
        try:
            final_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise TranscriptionWorkflowError(
                f"output directory changed during publication: {final_dir}"
            ) from exc
        final_dir_created = True

        for artifact in artifacts:
            destination = final_dir / artifact.path.name
            try:
                destination.hardlink_to(artifact.path)
            except FileExistsError as exc:
                raise TranscriptionWorkflowError(
                    f"output directory changed during publication: {destination}"
                ) from exc
            published[destination] = (
                _path_identity(destination),
                artifact.sha256,
            )

        _verify_published_artifacts(final_dir, published)

        for artifact in artifacts:
            artifact.path.unlink()
        staging_dir.rmdir()

        # This is the commit point: after this exact verification there are no
        # further mutations in final_dir.
        _verify_published_artifacts(final_dir, published)
    except BaseException:
        _cleanup_failed_publication(
            final_dir,
            staging_dir,
            published,
            final_dir_created=final_dir_created,
        )
        raise

def _result_payload(
    result: TranscriptionResult,
    *,
    source_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "digitalisierer.transcript",
        "source_sha256": source_sha256,
        "text": result.transcript.text,
        "language": result.transcript.language,
        "segments": [_segment_payload(segment) for segment in result.transcript.segments],
        "backend": {
            "provider": result.provider,
            "engine": result.engine,
            "model": result.model,
            "model_revision": result.model_revision,
            "backend_version": result.backend_version,
            "cloud_used": result.cloud_used,
        },
    }


def transcribe_and_export(
    source: Path,
    output_dir: Path,
    backend: TranscriptionBackend,
) -> TranscriptionExport:
    source_path = source.expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise TranscriptionWorkflowError("transcription source must be a regular file")

    final_dir = output_dir.expanduser().absolute()
    staging_dir = _prepare_staging_dir(final_dir)
    artifacts: list[ExportArtifact] = []

    try:
        source_sha256, source_stat = _stable_source_hash(source_path)
        result = backend.transcribe(source_path)
        verified_sha256, verified_stat = _stable_source_hash(source_path)
        if (
            verified_sha256 != source_sha256
            or _stat_identity(verified_stat) != _stat_identity(source_stat)
        ):
            raise TranscriptionWorkflowError(
                "transcription source changed while the backend was running"
            )
        source_stat = verified_stat
        if result.cloud_used:
            raise TranscriptionWorkflowError(
                "transcription backend used cloud without Digitalisierer authorization"
            )

        artifacts.append(
            _write_artifact(
                staging_dir,
                name="transcript.txt",
                kind="transcript-text",
                content=result.transcript.text + "\n",
            )
        )
        artifacts.append(
            _write_artifact(
                staging_dir,
                name="transcript.json",
                kind="transcript-json",
                content=_json_text(
                    _result_payload(result, source_sha256=source_sha256)
                ),
            )
        )

        subtitles_written = _all_segments_timed(result)
        if subtitles_written:
            artifacts.append(
                _write_artifact(
                    staging_dir,
                    name="transcript.srt",
                    kind="subtitle-srt",
                    content=_srt(result),
                )
            )
            artifacts.append(
                _write_artifact(
                    staging_dir,
                    name="transcript.vtt",
                    kind="subtitle-vtt",
                    content=_vtt(result),
                )
            )

        output_hashes = {
            artifact.path.name: artifact.sha256
            for artifact in artifacts
        }
        manifest = {
            "schema_version": 1,
            "kind": "digitalisierer.transcription-manifest",
            "source": {
                "file_name": source_path.name,
                "sha256": source_sha256,
                "media_metadata": {
                    "bytes": source_stat.st_size,
                    "suffix": source_path.suffix.casefold(),
                },
            },
            "capability": backend.capability,
            "adapter": backend.name,
            "authority": backend.authority,
            "provider": result.provider,
            "engine": result.engine,
            "model": result.model,
            "model_revision": result.model_revision,
            "backend_version": result.backend_version,
            "language": result.transcript.language,
            "parameters": {
                "strategy": "local-first",
            },
            "cloud_used": result.cloud_used,
            "subtitles_written": subtitles_written,
            "output_hashes": output_hashes,
        }
        artifacts.append(
            _write_artifact(
                staging_dir,
                name="manifest.json",
                kind="manifest",
                content=_json_text(manifest),
            )
        )

        _publish_staged_artifacts(
            final_dir,
            staging_dir,
            artifacts,
        )
    except BaseException:
        # Before publication final_dir does not exist. Publication owns any
        # final-dir cleanup; this outer path only removes private staging.
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    finalized_artifacts = tuple(
        ExportArtifact(
            kind=artifact.kind,
            path=final_dir / artifact.path.name,
            sha256=artifact.sha256,
        )
        for artifact in artifacts
    )
    return TranscriptionExport(output_dir=final_dir, artifacts=finalized_artifacts)