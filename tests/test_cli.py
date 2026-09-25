import json
from pathlib import Path

from digitalisierer import cli


def test_cli_entrypoint_is_callable() -> None:
    assert callable(cli.main)


def test_doctor_reports_valid_capability_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    app = tmp_path / "CzurScanner"
    app.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    app.chmod(0o755)

    monkeypatch.setattr(
        cli,
        "_which",
        lambda name: {"found": True, "path": f"/tools/{name}"},
    )
    monkeypatch.setattr(cli, "_czur_app_path", lambda: app)

    assert cli.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["digitalisierer"]
    assert payload["capabilities"]["media"]["ready"] is True
    assert payload["capabilities"]["ocr"]["ready"] is True
    assert payload["capabilities"]["capture-czur"]["ready"] is True
    assert payload["capabilities"]["capture-czur"]["checks"]["czur-app"] == {
        "found": True,
        "path": str(app),
    }


def test_doctor_requires_executable_czur_app(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    app = tmp_path / "CzurScanner"
    app.write_text("not executable\n", encoding="utf-8")
    app.chmod(0o644)

    monkeypatch.setattr(
        cli,
        "_which",
        lambda name: {"found": True, "path": f"/tools/{name}"},
    )
    monkeypatch.setattr(cli, "_czur_app_path", lambda: app)

    assert cli.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["capabilities"]["capture-czur"]["ready"] is False
    assert (
        payload["capabilities"]["capture-czur"]["checks"]["czur-app"]["found"]
        is False
    )
