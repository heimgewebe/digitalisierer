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
            "heimgewebe-asr": {
                "found": ready,
                "path": "/tools/heimgewebe-asr" if ready else None,
            }
        },
        "detail": "ready" if ready else "not ready",
    }


def _capture_status(ready: bool) -> cli.CapabilityStatus:
    return {
        "ready": ready,
        "checks": {
            "xdotool": {
                "found": ready,
                "path": "/tools/xdotool" if ready else None,
            },
            "czur-launcher": {
                "found": ready,
                "path": "/tools/czur-scanner" if ready else None,
            },
            "czur-config": {
                "found": ready,
                "path": "/config/czur.json" if ready else None,
            },
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
    monkeypatch.setattr(cli, "_which", _all_tools_present)
    monkeypatch.setattr(cli, "_czur_capture_capability", lambda: _capture_status(True))
    monkeypatch.setattr(cli, "_transcription_capability", _unexpected_transcription_capability)

    assert cli.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is True
    assert payload["required_capabilities"] == ["media", "ocr"]
    assert payload["capabilities"]["media"]["ready"] is True
    assert payload["capabilities"]["ocr"]["ready"] is True
    assert payload["capabilities"]["capture-czur"]["ready"] is True
    assert payload["capabilities"]["capture-czur"] == _capture_status(True)
    assert payload["capabilities"]["transcription"] == {
        "ready": None,
        "checks": {},
        "detail": "not checked; use --require transcription for a live ASR readiness probe",
    }


def test_doctor_capture_czur_uses_backend_readiness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_which", _all_tools_present)
    monkeypatch.setattr(cli, "_czur_capture_capability", lambda: _capture_status(False))
    monkeypatch.setattr(cli, "_transcription_capability", _unexpected_transcription_capability)

    assert cli.main(["doctor", "--require", "capture-czur"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is False
    assert payload["required_capabilities"] == ["capture-czur"]
    assert payload["capabilities"]["capture-czur"] == _capture_status(False)


def test_doctor_default_exit_fails_when_required_core_capability_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_tesseract(name: str) -> cli.ToolCheck:
        if name == "tesseract":
            return {"found": False, "path": None}
        return {"found": True, "path": f"/tools/{name}"}

    monkeypatch.setattr(cli, "_which", missing_tesseract)
    monkeypatch.setattr(cli, "_czur_capture_capability", lambda: _capture_status(True))
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

    library_root = tmp_path / "library"

    def fake_library_export(
        source_path: Path,
        _backend: object,
        selected_library_root: Path | None = None,
    ) -> TranscriptionExport:
        assert source_path == source
        assert selected_library_root == library_root
        final_dir = default_output_dir(source, library_root).absolute()
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

    monkeypatch.setattr(cli, "transcribe_to_library", fake_library_export)

    assert cli.main(
        ["transcribe", str(source), "--library-root", str(library_root)]
    ) == 0
    output = capsys.readouterr().out
    output.encode("ascii")
    payload = json.loads(output)

    expected_output = str(default_output_dir(source, library_root).absolute())
    assert os.fsencode(payload["output_dir"]) == os.fsencode(expected_output)
    assert os.fsencode(payload["artifacts"][0]["path"]) == os.fsencode(
        str(Path(expected_output) / "transcript.json")
    )


def test_default_output_dir_uses_standard_library_layout(tmp_path: Path) -> None:
    source = tmp_path / "My Recording.m4a"
    source.write_bytes(b"synthetic-audio")
    library_root = tmp_path / "Digitalisierer"

    output = default_output_dir(source, library_root)

    source_sha256 = __import__("hashlib").sha256(b"synthetic-audio").hexdigest()
    assert output == (
        library_root
        / "projects"
        / "inbox"
        / "sessions"
        / f"My-Recording--{source_sha256[:12]}"
    )

def test_invalid_scanner_jobs_env_does_not_break_unrelated_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CZUR_OCR_JOBS", "not-an-integer")
    monkeypatch.setattr(cli, "_which", _all_tools_present)
    monkeypatch.setattr(cli, "_transcription_capability", _unexpected_transcription_capability)

    assert cli.main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True


def test_invalid_scanner_jobs_env_is_reported_by_scan_finalize(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CZUR_OCR_JOBS", "not-an-integer")

    assert (
        cli.main(
            [
                "scan",
                "finalize",
                "--project",
                "book",
                "--session",
                "chapter",
            ]
        )
        == 1
    )
    assert "scan failed:" in capsys.readouterr().err

def test_invalid_scan_identity_is_reported_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.main(
            [
                "scan",
                "init",
                "--project",
                "../bad",
                "--session",
                "chapter",
                "--library-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("scan failed: ")
    assert "Traceback" not in captured.err
