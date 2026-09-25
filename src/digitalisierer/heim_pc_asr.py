from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from typing import cast

from .domain import Transcript, TranscriptSegment, TranscriptionResult


ASR_CAPABILITY = "audio.transcribe"
ASR_AUTHORITY = "heim_pc_asr_open_engine"
TRANSCRIPT_KIND = "heim-pc.asr-transcript"
ROUTE_RESULT_KIND = "heim-pc.asr-route-result"


class AsrAdapterError(RuntimeError):
    """Raised when the external ASR authority cannot be consumed safely."""


@dataclass(frozen=True, slots=True)
class AsrLocator:
    authority: str
    argv_prefix: tuple[str, ...]
    operator_entry_path: Path


@dataclass(frozen=True, slots=True)
class AsrBackendStatus:
    ready: bool
    detail: str
    entrypoint: str | None


def _json_object(path: Path) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AsrAdapterError(f"cannot read ASR capability contract: {path}") from exc
    if not isinstance(raw, dict):
        raise AsrAdapterError("ASR capability contract must be a JSON object")
    return cast(dict[str, object], raw)


def _object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AsrAdapterError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AsrAdapterError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field=field)


def _expand_home(value: str, home: Path) -> str:
    expanded = value.replace("${HOME}", str(home))
    if "${" in expanded:
        raise AsrAdapterError("ASR capability contract contains unresolved variables")
    return expanded


def load_asr_locator(
    operator_entry_path: Path | None = None,
    *,
    home: Path | None = None,
) -> AsrLocator:
    home_dir = (home or Path.home()).expanduser().resolve()
    contract_path = (
        operator_entry_path
        if operator_entry_path is not None
        else home_dir / ".config/heimgewebe/operator-entry.v1.json"
    ).expanduser()
    payload = _json_object(contract_path)

    locators = _object(payload.get("capabilityLocators"), field="capabilityLocators")
    locator = _object(locators.get("audioTranscription"), field="audioTranscription")

    intents = locator.get("intents")
    if not isinstance(intents, list) or ASR_CAPABILITY not in intents:
        raise AsrAdapterError("audioTranscription does not declare audio.transcribe")
    if locator.get("authority") != ASR_AUTHORITY:
        raise AsrAdapterError("audioTranscription authority mismatch")
    if locator.get("authorityKind") != "capability_locator_only":
        raise AsrAdapterError("audioTranscription authority kind mismatch")
    if locator.get("policyResolution") != "read_at_execution_time":
        raise AsrAdapterError("ASR policy must be resolved at execution time")
    if locator.get("consumerEnginePinningAllowed") is not False:
        raise AsrAdapterError("consumer-side ASR engine pinning must remain disabled")
    if locator.get("cloudOrMeteredUseAuthorizedByLocator") is not False:
        raise AsrAdapterError("ASR locator must not authorize cloud or metered use")
    if locator.get("entryKind") != "argv":
        raise AsrAdapterError("ASR locator must expose an argv entrypoint")

    prefix = locator.get("entryArgvPrefix")
    if not isinstance(prefix, list) or not prefix:
        raise AsrAdapterError("ASR locator has no entry argv prefix")
    argv: list[str] = []
    for index, item in enumerate(prefix):
        argv.append(
            _expand_home(
                _string(item, field=f"entryArgvPrefix[{index}]"),
                home_dir,
            )
        )

    return AsrLocator(
        authority=ASR_AUTHORITY,
        argv_prefix=tuple(argv),
        operator_entry_path=contract_path,
    )


def _timestamp(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AsrAdapterError(f"{field} must be numeric or null")
    return float(value)


def _parse_transcript(payload: object) -> TranscriptionResult:
    transcript = _object(payload, field="selected transcript")
    required = {
        "schema_version",
        "kind",
        "provider",
        "engine",
        "model",
        "model_revision",
        "backend_version",
        "text",
        "language",
        "segments",
    }
    missing = sorted(required.difference(transcript))
    if missing:
        raise AsrAdapterError(f"ASR transcript is missing fields: {', '.join(missing)}")
    if transcript.get("schema_version") != 1 or transcript.get("kind") != TRANSCRIPT_KIND:
        raise AsrAdapterError("ASR transcript contract identity mismatch")

    provider = _string(transcript.get("provider"), field="provider")
    if provider != "local":
        raise AsrAdapterError("Digitalisierer transcription requires the local ASR path")
    engine = _string(transcript.get("engine"), field="engine")
    model = _string(transcript.get("model"), field="model")
    model_revision = _optional_string(
        transcript.get("model_revision"),
        field="model_revision",
    )
    backend_version = _optional_string(
        transcript.get("backend_version"),
        field="backend_version",
    )
    text = transcript.get("text")
    if not isinstance(text, str):
        raise AsrAdapterError("ASR transcript text must be a string")
    language = transcript.get("language")
    if language is not None and not isinstance(language, str):
        raise AsrAdapterError("ASR transcript language must be a string or null")

    raw_segments = transcript.get("segments")
    if not isinstance(raw_segments, list):
        raise AsrAdapterError("ASR transcript segments must be a list")
    segments: list[TranscriptSegment] = []
    for index, item in enumerate(raw_segments):
        segment = _object(item, field=f"segments[{index}]")
        segment_required = {"start", "end", "speaker", "text"}
        segment_missing = sorted(segment_required.difference(segment))
        if segment_missing:
            raise AsrAdapterError(
                f"ASR segment {index} is missing fields: {', '.join(segment_missing)}"
            )
        segment_text = segment.get("text")
        if not isinstance(segment_text, str):
            raise AsrAdapterError(f"segments[{index}].text must be a string")
        speaker = segment.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise AsrAdapterError(f"segments[{index}].speaker must be a string or null")
        try:
            segments.append(
                TranscriptSegment(
                    text=segment_text,
                    start=_timestamp(segment.get("start"), field=f"segments[{index}].start"),
                    end=_timestamp(segment.get("end"), field=f"segments[{index}].end"),
                    speaker=speaker,
                    confidence=None,
                )
            )
        except ValueError as exc:
            raise AsrAdapterError(f"invalid ASR segment {index}: {exc}") from exc

    return TranscriptionResult(
        transcript=Transcript(
            text=text,
            language=language,
            segments=tuple(segments),
        ),
        provider=provider,
        engine=engine,
        model=model,
        model_revision=model_revision,
        backend_version=backend_version,
        cloud_used=False,
    )


def parse_route_result(payload: object) -> TranscriptionResult:
    route = _object(payload, field="ASR route result")
    if route.get("schema_version") != 1 or route.get("kind") != ROUTE_RESULT_KIND:
        raise AsrAdapterError("ASR route result contract identity mismatch")
    if route.get("strategy") != "local-first":
        raise AsrAdapterError("Digitalisierer requires the local-first ASR strategy")
    if route.get("cloud_used") is not False:
        raise AsrAdapterError("Digitalisierer did not authorize cloud ASR")
    return _parse_transcript(route.get("selected"))


class HeimPcAsrBackend:
    name = "heim-pc-asr"
    capability = ASR_CAPABILITY
    authority = ASR_AUTHORITY

    def __init__(
        self,
        operator_entry_path: Path | None = None,
        *,
        timeout_seconds: int = 21_600,
    ) -> None:
        self._operator_entry_path = operator_entry_path
        self._timeout_seconds = timeout_seconds

    def _locator(self) -> AsrLocator:
        return load_asr_locator(self._operator_entry_path)

    def status(self) -> AsrBackendStatus:
        try:
            locator = self._locator()
            executable = locator.argv_prefix[0]
            if "/" not in executable and shutil.which(executable) is None:
                return AsrBackendStatus(
                    ready=False,
                    detail="ASR entrypoint executable is unavailable",
                    entrypoint=executable,
                )
            completed = subprocess.run(
                [*locator.argv_prefix, "doctor"],
                check=False,
                capture_output=True,
                text=True,
                timeout=min(self._timeout_seconds, 120),
            )
        except (AsrAdapterError, OSError, subprocess.TimeoutExpired):
            return AsrBackendStatus(
                ready=False,
                detail="heim-pc ASR authority is not ready",
                entrypoint=None,
            )
        return AsrBackendStatus(
            ready=completed.returncode == 0,
            detail=(
                "heim-pc audio.transcribe authority ready"
                if completed.returncode == 0
                else "heim-pc ASR doctor reported an incomplete runtime"
            ),
            entrypoint=locator.argv_prefix[-1],
        )

    def transcribe(self, source: Path) -> TranscriptionResult:
        source_path = source.expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise AsrAdapterError("ASR source must be a regular file")
        locator = self._locator()
        try:
            completed = subprocess.run(
                [
                    *locator.argv_prefix,
                    "route",
                    "--audio",
                    str(source_path),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AsrAdapterError("heim-pc ASR invocation failed") from exc
        if completed.returncode != 0:
            raise AsrAdapterError(
                f"heim-pc ASR exited with status {completed.returncode}"
            )
        try:
            payload: object = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AsrAdapterError("heim-pc ASR returned invalid JSON") from exc
        return parse_route_result(payload)