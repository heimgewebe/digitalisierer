from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
from typing import Any

import pytest

from digitalisierer import cli, transcription as transcription_module
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


def test_route_contract_ignores_undeclared_confidence_field() -> None:
    result = parse_route_result(
        _route_payload(
            segments=[
                {
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": None,
                    "text": "Hallo Welt.",
                    "confidence": 0.99,
                }
            ]
        )
    )

    assert result.transcript.segments[0].confidence is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", float("nan")),
        ("start", float("inf")),
        ("end", float("-inf")),
        ("start", 10**1000),
    ],
)
def test_route_contract_rejects_non_finite_or_overflowing_timestamps(
    field: str,
    value: object,
) -> None:
    segment: dict[str, object] = {
        "start": 0.0,
        "end": 1.0,
        "speaker": None,
        "text": "Hallo Welt.",
    }
    segment[field] = value

    with pytest.raises(AsrAdapterError, match="finite number or null"):
        parse_route_result(_route_payload(segments=[segment]))


@pytest.mark.parametrize("field", ["text", "language"])
def test_route_contract_rejects_lone_surrogate_in_transcript_strings(
    field: str,
) -> None:
    payload = _route_payload()
    selected = payload["selected"]
    assert isinstance(selected, dict)
    selected[field] = "\udcff"

    with pytest.raises(AsrAdapterError, match="valid UTF-8 text"):
        parse_route_result(payload)


@pytest.mark.parametrize("field", ["text", "speaker"])
def test_route_contract_rejects_lone_surrogate_in_segment_strings(
    field: str,
) -> None:
    segment: dict[str, object] = {
        "start": 0.0,
        "end": 1.0,
        "speaker": None,
        "text": "Hallo",
    }
    segment[field] = "\udcff"

    with pytest.raises(AsrAdapterError, match="valid UTF-8 text"):
        parse_route_result(_route_payload(segments=[segment]))


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
    captured_kwargs: dict[str, Any] = {}

    def fake_run(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        captured_kwargs.update(kwargs)
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
    assert captured_kwargs["text"] is True
    assert captured_kwargs["encoding"] == "utf-8"


def test_backend_invalid_utf8_is_controlled_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _operator_entry(
        tmp_path / "operator-entry.json",
        entrypoint=str(tmp_path / "asr_engine.py"),
    )
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")

    def fake_run(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs["encoding"] == "utf-8"
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("digitalisierer.heim_pc_asr.subprocess.run", fake_run)

    with pytest.raises(AsrAdapterError, match="heim-pc ASR invocation failed"):
        HeimPcAsrBackend(contract, timeout_seconds=10).transcribe(source)


def test_backend_error_includes_sanitized_stderr_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _operator_entry(
        tmp_path / "operator-entry.json",
        entrypoint=str(tmp_path / "asr_engine.py"),
    )
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")

    def fake_run(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            argv,
            7,
            stdout="",
            stderr="backend failed\n  model cache unavailable  ",
        )

    monkeypatch.setattr("digitalisierer.heim_pc_asr.subprocess.run", fake_run)

    with pytest.raises(
        AsrAdapterError,
        match="status 7: backend failed model cache unavailable",
    ):
        HeimPcAsrBackend(contract, timeout_seconds=10).transcribe(source)


def test_backend_status_preserves_locator_error_detail(tmp_path: Path) -> None:
    unsafe = _operator_entry(
        tmp_path / "operator-entry-unsafe.json",
        cloud_authorized=True,
    )

    status = HeimPcAsrBackend(unsafe).status()

    assert status.ready is False
    assert "must not authorize cloud" in status.detail


def test_backend_status_includes_doctor_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _operator_entry(
        tmp_path / "operator-entry.json",
        entrypoint=str(tmp_path / "asr_engine.py"),
    )

    captured_kwargs: dict[str, Any] = {}

    def fake_run(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            3,
            stdout="",
            stderr="runtime incomplete\n model missing",
        )

    monkeypatch.setattr("digitalisierer.heim_pc_asr.subprocess.run", fake_run)

    status = HeimPcAsrBackend(contract, timeout_seconds=10).status()

    assert status.ready is False
    assert status.detail == "heim-pc ASR doctor exited with status 3: runtime incomplete model missing"
    assert captured_kwargs["text"] is True
    assert captured_kwargs["encoding"] == "utf-8"


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
    assert {entry.name for entry in output.iterdir()} == {
        "transcript.txt",
        "transcript.json",
        "transcript.srt",
        "transcript.vtt",
        "manifest.json",
    }
    assert manifest["source"]["sha256"] == hashlib.sha256(b"synthetic-audio").hexdigest()
    assert set(manifest["output_hashes"]) == {
        "transcript.txt",
        "transcript.json",
        "transcript.srt",
        "transcript.vtt",
    }


def test_export_serializes_non_utf8_source_filename(tmp_path: Path) -> None:
    raw_name = b"sample-\xff.wav"
    source = tmp_path / os.fsdecode(raw_name)
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"

    transcribe_and_export(source, output, FakeBackend(_result(timed=False)))

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert os.fsencode(manifest["source"]["file_name"]) == raw_name


def test_subtitle_export_escapes_markup_and_collapses_line_breaks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"
    raw_text = "Er sagte <unverstaendlich> & zeigte -->\n\ndorthin"
    result = TranscriptionResult(
        transcript=Transcript(
            text=raw_text,
            language="de",
            segments=(
                TranscriptSegment(
                    raw_text,
                    start=0.0,
                    end=1.25,
                    speaker="A&B",
                ),
            ),
        ),
        provider="local",
        engine="faster-whisper",
        model="Systran/faster-whisper-large-v3",
        backend_version="1.2.1",
        cloud_used=False,
    )

    transcribe_and_export(source, output, FakeBackend(result))

    safe = (
        "A&amp;B: Er sagte &lt;unverstaendlich&gt; "
        "&amp; zeigte --&gt; dorthin"
    )
    assert (output / "transcript.srt").read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,000 --> 00:00:01,250\n"
        f"{safe}\n"
    )
    assert (output / "transcript.vtt").read_text(encoding="utf-8") == (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.250\n"
        f"{safe}\n"
    )
    payload = json.loads((output / "transcript.json").read_text(encoding="utf-8"))
    assert payload["text"] == raw_text
    assert payload["segments"][0]["text"] == raw_text
    assert payload["segments"][0]["speaker"] == "A&B"


def test_subtitle_export_rejects_timestamp_outside_supported_range(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"
    result = TranscriptionResult(
        transcript=Transcript(
            text="Hallo Welt",
            language="de",
            segments=(
                TranscriptSegment(
                    "Hallo Welt",
                    start=0.0,
                    end=1e308,
                ),
            ),
        ),
        provider="local",
        engine="faster-whisper",
        model="Systran/faster-whisper-large-v3",
        backend_version="1.2.1",
        cloud_used=False,
    )

    with pytest.raises(
        TranscriptionWorkflowError,
        match="subtitle timestamp exceeds the supported range",
    ):
        transcribe_and_export(source, output, FakeBackend(result))

    assert not output.exists()


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


def test_keyboard_interrupt_during_staging_creation_cleans_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"
    original_mkdir = Path.mkdir

    def interrupting_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.parent == tmp_path and path.name.startswith(".export.staging-"):
            original_mkdir(path, *args, **kwargs)
            raise KeyboardInterrupt
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", interrupting_mkdir)

    with pytest.raises(KeyboardInterrupt):
        transcribe_and_export(source, output, FakeBackend(_result()))

    assert not output.exists()
    assert list(tmp_path.glob(".export.staging-*")) == []


def test_keyboard_interrupt_during_backend_cleans_reserved_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"

    class InterruptingBackend(FakeBackend):
        def transcribe(self, source: Path) -> TranscriptionResult:
            assert source.is_file()
            assert not output.exists()
            assert len(list(tmp_path.glob(".export.staging-*"))) == 1
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        transcribe_and_export(source, output, InterruptingBackend(_result()))

    assert not output.exists()
    assert list(tmp_path.glob(".export.staging-*")) == []


def test_export_rejects_existing_output_before_starting_backend(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"
    output.mkdir()

    class MustNotRunBackend(FakeBackend):
        def transcribe(self, source: Path) -> TranscriptionResult:
            pytest.fail("backend must not run when output directory already exists")

    with pytest.raises(TranscriptionWorkflowError, match="output directory already exists"):
        transcribe_and_export(source, output, MustNotRunBackend(_result()))


def test_export_validates_parent_before_starting_backend(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    output = blocked_parent / "export"

    class MustNotRunBackend(FakeBackend):
        def transcribe(self, source: Path) -> TranscriptionResult:
            pytest.fail("backend must not run when output parent is invalid")

    with pytest.raises(OSError):
        transcribe_and_export(source, output, MustNotRunBackend(_result()))


def test_export_reservation_prevents_late_destination_clobber(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"

    class InterferingBackend(FakeBackend):
        def transcribe(self, source: Path) -> TranscriptionResult:
            assert not output.exists()
            output.mkdir()
            (output / "transcript.txt").write_text("foreign", encoding="utf-8")
            return self.result

    with pytest.raises(
        TranscriptionWorkflowError,
        match="output directory changed during publication",
    ):
        transcribe_and_export(source, output, InterferingBackend(_result()))

    assert (output / "transcript.txt").read_text(encoding="utf-8") == "foreign"
    assert not (output / "manifest.json").exists()


def test_failure_after_atomic_exposure_never_deletes_final_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"
    original_verify = transcription_module._verify_bound_artifacts

    def fail_after_exposure(
        directory_path: Path,
        directory_fd: int,
        directory_identity: tuple[int, int],
        published: dict[str, tuple[tuple[int, int], str]],
    ) -> None:
        original_verify(
            directory_path,
            directory_fd,
            directory_identity,
            published,
        )
        if directory_path == output:
            raise TranscriptionWorkflowError(
                "output directory changed during publication"
            )

    monkeypatch.setattr(
        transcription_module,
        "_verify_bound_artifacts",
        fail_after_exposure,
    )

    with pytest.raises(
        TranscriptionWorkflowError,
        match="output directory changed during publication",
    ):
        transcribe_and_export(source, output, FakeBackend(_result()))

    assert {entry.name for entry in output.iterdir()} == {
        "transcript.txt",
        "transcript.json",
        "transcript.srt",
        "transcript.vtt",
        "manifest.json",
    }
    assert (output / "transcript.txt").read_text(encoding="utf-8") == "Hallo Welt\n"
    assert list(tmp_path.glob(".export.staging-*")) == []


def test_keyboard_interrupt_before_atomic_publish_cleans_private_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"

    def interrupting_rename(source: Path, destination: Path) -> None:
        del source, destination
        raise KeyboardInterrupt

    monkeypatch.setattr(
        transcription_module,
        "_rename_noreplace",
        interrupting_rename,
    )

    with pytest.raises(KeyboardInterrupt):
        transcribe_and_export(source, output, FakeBackend(_result()))

    assert not output.exists()
    assert list(tmp_path.glob(".export.staging-*")) == []


def test_atomic_publish_refuses_public_path_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"
    original_rename = transcription_module._rename_noreplace

    def racing_rename(source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "foreign.txt").write_text("foreign", encoding="utf-8")
        original_rename(source, destination)

    monkeypatch.setattr(
        transcription_module,
        "_rename_noreplace",
        racing_rename,
    )

    with pytest.raises(
        TranscriptionWorkflowError,
        match="output directory changed during publication",
    ):
        transcribe_and_export(source, output, FakeBackend(_result()))

    assert (output / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    assert not (output / "transcript.txt").exists()
    assert not (output / "manifest.json").exists()
    assert list(tmp_path.glob(".export.staging-*")) == []


def test_export_does_not_require_hardlink_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"

    def forbidden_hardlink(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        pytest.fail("export publication must not require hard links")

    monkeypatch.setattr(Path, "hardlink_to", forbidden_hardlink)

    transcribe_and_export(source, output, FakeBackend(_result()))

    assert (output / "transcript.txt").is_file()
    assert (output / "manifest.json").is_file()


def test_export_detects_same_name_replacement_during_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"synthetic-audio")
    output = tmp_path / "export"
    original_rename = transcription_module._rename_noreplace
    swapped = False

    def swapping_rename(source: Path, destination: Path) -> None:
        nonlocal swapped
        artifact = source / "transcript.txt"
        artifact.unlink()
        artifact.write_text("foreign", encoding="utf-8")
        swapped = True
        original_rename(source, destination)

    monkeypatch.setattr(
        transcription_module,
        "_rename_noreplace",
        swapping_rename,
    )

    with pytest.raises(
        TranscriptionWorkflowError,
        match="output directory changed during publication",
    ):
        transcribe_and_export(source, output, FakeBackend(_result()))

    assert swapped is True
    assert (output / "transcript.txt").read_text(encoding="utf-8") == "foreign"
    assert (output / "manifest.json").is_file()
    assert list(tmp_path.glob(".export.staging-*")) == []


def test_export_fails_if_source_changes_during_initial_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"before")
    output = tmp_path / "export"
    original_hash = transcription_module._sha256_file
    mutated = False

    def mutating_hash(path: Path) -> str:
        nonlocal mutated
        if path == source.resolve() and not mutated:
            mutated = True
            path.write_bytes(b"after-and-larger")
        return original_hash(path)

    class MustNotRunBackend(FakeBackend):
        def transcribe(self, source: Path) -> TranscriptionResult:
            pytest.fail("backend must not run when the initial source hash is unstable")

    monkeypatch.setattr(transcription_module, "_sha256_file", mutating_hash)

    with pytest.raises(TranscriptionWorkflowError, match="changed while hashing"):
        transcribe_and_export(source, output, MustNotRunBackend(_result()))

    assert mutated is True
    assert not output.exists()


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