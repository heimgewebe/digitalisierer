import json
from pathlib import Path

import pytest

from digitalisierer import cli


def _all_tools_present(name: str) -> cli.ToolCheck:
    return {"found": True, "path": f"/tools/{name}"}


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
    monkeypatch.setattr(cli, "_transcription_capability", lambda: _transcription_status(True))

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
    assert set(payload["capabilities"]["transcription"]) == {
        "ready",
        "checks",
        "detail",
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
    monkeypatch.setattr(cli, "_transcription_capability", lambda: _transcription_status(True))

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
    monkeypatch.setattr(cli, "_transcription_capability", lambda: _transcription_status(True))

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
