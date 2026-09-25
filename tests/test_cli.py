import json
import os
from pathlib import Path

import pytest

from digitalisierer import cli
from digitalisierer.domain import ExportArtifact
from digitalisierer.transcription import TranscriptionExport, default_output_dir


def _all_tools_present(name: str) -> cli.ToolCheck:
    return {"found": True, "path": f"/tools/{name}"}


def _unexpected_transcription_capability() -> cli.CapabilityStatus:
    pytest.fail("transcription must not be probed unless it is required")


def _transcription_status(ready: bool) -> cli.CapabilityStatus:
    return {
        "ready": ready,
        "checks": {
            "heim-pc-asr": {
                "found": ready,
                "path": "/tools/heim-pc-asr" if ready else None,
            }
        },
        "detail": "ready" if ready else "not ready",
    }


def test_cli_entrypoint_is_callable() -> None:
    assert callable(cli.main)


def test_doctor_reports_uniform_capability_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = tmp_path / "CzurScanner"
    app.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    app.chmod(0o755)

    monkeypatch.setattr(cli, "_which", _all_tools_present)
    monkeypatch.setattr(cli, "_czur_app_path", lambda: app)
    monkeypatch.setattr(cli, "_transcription_capability", _unexpected_transcription_capability)

    assert cli.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is True
    assert payload["required_capabilities"] == ["media", "ocr"]
    assert payload["capabilities"]["media"]["ready"] is True
    assert payload["capabilities"]["ocr"]["ready"] is True
    assert payload["capabilities"]["capture-czur"]["ready"] is True
    assert payload["capabilities"]["capture-czur"]["checks"]["czur-app"] == {
        "found": True,
        "path": str(app),
    }
    assert payload["capabilities"]["transcription"] == {
        "ready": None,
        "checks": {},
        "detail": "not checked; use --require transcription for a live ASR readiness probe",
    }


def test_doctor_can_require_executable_czur_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = tmp_path / "CzurScanner"
    app.write_text("not executable\n", encoding="utf-8")
    app.chmod(0o644)

    monkeypatch.setattr(cli, "_which", _all_tools_present)
    monkeypatch.setattr(cli, "_czur_app_path", lambda: app)
    monkeypatch.setattr(cli, "_transcription_capability", _unexpected_transcription_capability)

    assert cli.main(["doctor", "--require", "capture-czur"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is False
    assert payload["required_capabilities"] == ["capture-czur"]
    assert payload["capabilities"]["capture-czur"]["ready"] is False
    assert (
        payload["capabilities"]["capture-czur"]["checks"]["czur-app"]["found"]
        is False
    )


def test_doctor_default_exit_fails_when_required_core_capability_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = tmp_path / "CzurScanner"
    app.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    app.chmod(0o755)

    def missing_tesseract(name: str) -> cli.ToolCheck:
        if name == "tesseract":
            return {"found": False, "path": None}
        return {"found": True, "path": f"/tools/{name}"}

    monkeypatch.setattr(cli, "_which", missing_tesseract)
    monkeypatch.setattr(cli, "_czur_app_path", lambda: app)
    monkeypatch.setattr(cli, "_transcription_capability", _unexpected_transcription_capability)

    assert cli.main(["doctor"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is False
    assert payload["capabilities"]["ocr"]["ready"] is False


@pytest.mark.parametrize(("ready", "expected_exit"), [(True, 0), (False, 1)])
def test_doctor_require_transcription_reflects_backend_readiness(
    ready: bool,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_transcription_capability",
        lambda: _transcription_status(ready),
    )

    assert cli.main(["doctor", "--require", "transcription"]) == expected_exit
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is ready
    assert payload["required_capabilities"] == ["transcription"]
    assert payload["capabilities"]["transcription"]["ready"] is ready


def test_doctor_emits_ascii_json_for_non_utf8_tool_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_tool_path = b"/tools/ffmpeg-\xff"
    weird_path = os.fsdecode(raw_tool_path)

    monkeypatch.setattr(
        cli,
        "_which",
        lambda _name: {"found": True, "path": weird_path},
    )
    monkeypatch.setattr(cli, "_transcription_capability", _unexpected_transcription_capability)

    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    output.encode("ascii")
    payload = json.loads(output)

    observed = payload["capabilities"]["media"]["checks"]["ffmpeg"]["path"]
    assert os.fsencode(observed) == raw_tool_path


def test_transcribe_emits_ascii_json_for_non_utf8_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_name = b"sample-\xff.wav"
    source = tmp_path / os.fsdecode(raw_name)
    source.write_bytes(b"synthetic-audio")

    monkeypatch.setattr(cli, "_transcription_backend", lambda: object())

    def fake_export(
        source_path: Path,
        output_dir: Path,
        _backend: object,
    ) -> TranscriptionExport:
        assert source_path == source
        final_dir = output_dir.absolute()
        return TranscriptionExport(
            output_dir=final_dir,
            artifacts=(
                ExportArtifact(
                    kind="transcript-json",
                    path=final_dir / "transcript.json",
                    sha256="a" * 64,
                ),
            ),
        )

    monkeypatch.setattr(cli, "transcribe_and_export", fake_export)

    assert cli.main(["transcribe", str(source)]) == 0
    output = capsys.readouterr().out
    output.encode("ascii")
    payload = json.loads(output)

    expected_output = str(default_output_dir(source).absolute())
    assert os.fsencode(payload["output_dir"]) == os.fsencode(expected_output)
    assert os.fsencode(payload["artifacts"][0]["path"]) == os.fsencode(
        str(Path(expected_output) / "transcript.json")
    )