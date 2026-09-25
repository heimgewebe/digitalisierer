from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from digitalisierer import cli
from digitalisierer.domain import Transcript, TranscriptSegment, TranscriptionResult
from digitalisierer.heim_pc_asr import (
    ASR_AUTHORITY,
    AsrAdapterError,
    HeimPcAsrBackend,
    load_asr_locator,
    parse_route_result,
)
from digitalisierer.transcription import (
    TranscriptionWorkflowError,
    transcribe_and_export,
)


def _route_payload(
    *,
    segments: list[dict[str, object]] | None = None,
    cloud_used: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "heim-pc.asr-route-result",
        "strategy": "local-first",
        "selected": {
            "schema_version": 1,
            "kind": "heim-pc.asr-transcript",
            "provider": "local",
            "engine": "faster-whisper",
            "model": "Systran/faster-whisper-large-v3",
            "model_revision": None,
            "backend_version": "1.2.1",
            "text": "Hallo Welt.",
            "language": "de",
            "segments": segments if segments is not None else [],
        },
        "comparison": None,
        "cloud_recommended": False,
        "cloud_used": cloud_used,
    }


def _operator_entry(
    path: Path,
    *,
    cloud_authorized: bool = False,
    entrypoint: str | None = None,
) -> Path:
    payload = {
        "capabilityLocators": {
            "audioTranscription": {
                "schemaVersion": 1,
                "intents": ["audio.transcribe", "transcription"],
                "authority": ASR_AUTHORITY,
                "authorityKind": "capability_locator_only",
                "entryArgvPrefix": [
                    "python3",
                    entrypoint or "${HOME}/repos/heim-pc/scripts/asr_engine.py",
                ],
                "policyResolution": "read_at_execution_time",
                "consumerEnginePinningAllowed": False,
                "cloudOrMeteredUseAuthorizedByLocator": cloud_authorized,
                "entryKind": "argv",
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _result(*, timed: bool = True) -> TranscriptionResult:
    segments = (
        (
            TranscriptSegment("Hallo", start=0.0, end=0.8),
            TranscriptSegment("Welt", start=0.8, end=1.5, speaker="A"),
        )
        if timed
        else (TranscriptSegment("Hallo Welt"),)
    )
    return TranscriptionResult(
        transcript=Transcript(text="Hallo Welt", language="de", segments=segments),
        provider="local",
        engine="faster-whisper",
        model="Systran/faster-whisper-large-v3",
        backend_version="1.2.1",
        cloud_used=False,
    )


class FakeBackend:
    name = "fake-asr"
    capability = "audio.transcribe"
    authority = "fake-local-authority"

    def __init__(self, result: TranscriptionResult) -> None:
        self.result = result

    def transcribe(self, source: Path) -> TranscriptionResult:
        assert source.is_file()
        return self.result


def test_route_contract_maps_nullable_segment_fields_without_invention() -> None:
    result = parse_route_result(
        _route_payload(
            segments=[
                {
                    "start": None,
                    "end": None,
                    "speaker": None,
                    "text": "Hallo Welt.",
                }
            ]
        )
    )

    assert result.transcript.language == "de"
    assert len(result.transcript.segments) == 1
    segment = result.transcript.segments[0]
    assert segment.start is None
    assert segment.end is None
    assert segment.speaker is None
    assert segment.confidence is None


def test_route_contract_accepts_no_segments() -> None:
    result = parse_route_result(_route_payload(segments=[]))
    assert result.transcript.segments == ()


def test_route_contract_rejects_cloud_use() -> None:
    with pytest.raises(AsrAdapterError, match="did not authorize cloud"):
        parse_route_result(_route_payload(cloud_used=True))


def test_route_contract_rejects_missing_required_transcript_field() -> None:
    payload = _route_payload()
    selected = payload["selected"]
    assert isinstance(selected, dict)
    selected.pop("engine")

    with pytest.raises(AsrAdapterError, match="missing fields: engine"):
        parse_route_result(payload)


def test_locator_uses_installed_contract_and_rejects_cloud_authority(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    contract = _operator_entry(tmp_path / "operator-entry.json")
    locator = load_asr_locator(contract, home=home)

    assert locator.authority == ASR_AUTHORITY
    assert locator.argv_prefix == (
        "python3",
        str(home / "repos/heim-pc/scripts/asr_engine.py"),
    )

    unsafe = _operator_entry(
        tmp_path / "operator-entry-unsafe.json",
        cloud_authorized=True,
    )
    with pytest.raises(AsrAdapterError, match="must not authorize cloud"):
        load_asr_locator(unsafe, home=home)


def test_backend_invokes_local_first_route_without_engine_or_cloud_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _operator_entry(
        tmp_path / "operator-entry.json",
        entrypoint=str(tmp_path / "asr_engine.py"),
    )
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    captured: list[str] = []

    def fake_run(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        captured.extend(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(_route_payload()),
            stderr="",
        )

    monkeypatch.setattr("digitalisierer.heim_pc_asr.subprocess.run", fake_run)

    backend = HeimPcAsrBackend(contract, timeout_seconds=10)
    result = backend.transcribe(source)

    assert result.engine == "faster-whisper"
    assert captured[-4:] == ["route", "--audio", str(source.resolve()), "--json"]
    assert "route" in captured
    assert "--engine" not in captured
    assert "--allow-metered-cloud" not in captured
    assert "--escalate-to-cloud" not in captured


def test_export_writes_manifest_hashes_and_timed_subtitles(tmp_path: Path) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"

    exported = transcribe_and_export(source, output, FakeBackend(_result(timed=True)))

    names = {artifact.path.name for artifact in exported.artifacts}
    assert names == {
        "transcript.txt",
        "transcript.json",
        "transcript.srt",
        "transcript.vtt",
        "manifest.json",
    }
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["capability"] == "audio.transcribe"
    assert manifest["adapter"] == "fake-asr"
    assert manifest["authority"] == "fake-local-authority"
    assert manifest["provider"] == "local"
    assert manifest["engine"] == "faster-whisper"
    assert manifest["cloud_used"] is False
    assert manifest["parameters"] == {"strategy": "local-first"}
    assert manifest["subtitles_written"] is True
    assert manifest["source"]["sha256"] == hashlib.sha256(b"synthetic-audio").hexdigest()
    assert set(manifest["output_hashes"]) == {
        "transcript.txt",
        "transcript.json",
        "transcript.srt",
        "transcript.vtt",
    }


def test_export_omits_subtitles_without_complete_timing(tmp_path: Path) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"

    transcribe_and_export(source, output, FakeBackend(_result(timed=False)))

    assert (output / "transcript.txt").is_file()
    assert (output / "transcript.json").is_file()
    assert not (output / "transcript.srt").exists()
    assert not (output / "transcript.vtt").exists()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["subtitles_written"] is False


def test_export_fails_closed_if_source_changes_during_transcription(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"before")
    output = tmp_path / "export"

    class MutatingBackend(FakeBackend):
        def transcribe(self, source: Path) -> TranscriptionResult:
            source.write_bytes(b"after")
            return self.result

    with pytest.raises(TranscriptionWorkflowError, match="source changed"):
        transcribe_and_export(source, output, MutatingBackend(_result()))

    assert not output.exists()


def test_cli_transcribe_uses_backend_and_reports_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "out"
    backend = FakeBackend(_result(timed=False))
    monkeypatch.setattr(cli, "_transcription_backend", lambda: backend)

    assert cli.main(["transcribe", str(source), "--output-dir", str(output)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["output_dir"] == str(output.absolute())
    assert (output / "manifest.json").is_file()