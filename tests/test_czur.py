import json
import os
from pathlib import Path

import pytest

from digitalisierer.czur import (
    CURVED_BOOKS_HOTKEY,
    CURVED_BOOKS_SETTINGS,
    CzurAdapterError,
    CzurCaptureBackend,
)


def test_curved_books_preset_is_atomic_and_preserves_unrelated_settings(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".czur" / "ScannerInfo" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"setting": {"unrelated": "keep", "scan_preview_capture_type": "single"}}),
        encoding="utf-8",
    )
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool="/usr/bin/xdotool",
    )

    before = backend.apply_curved_books_preset()
    payload = json.loads(config.read_text(encoding="utf-8"))

    assert before["scan_preview_capture_type"] == "single"
    assert payload["setting"]["unrelated"] == "keep"
    for key, value in CURVED_BOOKS_SETTINGS.items():
        assert payload["setting"][key] == value
    assert config.with_name("config.pre-digitalisierer-curved-books.json").is_file()


def test_capture_fails_closed_without_explicit_hotkey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DIGITALISIERER_CZUR_CAPTURE_KEY", raising=False)
    backend = CzurCaptureBackend(
        capture_root=tmp_path,
        config_path=tmp_path / "config.json",
        launcher=tmp_path / "launcher",
    )
    with pytest.raises(CzurAdapterError, match="no capture key is configured"):
        backend.capture()


def test_newest_scan_folder_ignores_internal_directories(tmp_path: Path) -> None:
    first = tmp_path / "old"
    second = tmp_path / "new"
    internal = tmp_path / "_export"
    for folder in (first, second, internal):
        folder.mkdir()
    (first / "image1.jpg").write_bytes(b"a")
    (second / "image2.jpg").write_bytes(b"b")
    (internal / "image3.jpg").write_bytes(b"c")
    (first / "image1.jpg").touch()
    (second / "image2.jpg").touch()
    backend = CzurCaptureBackend(capture_root=tmp_path)

    assert backend.newest_scan_folder() in {first.resolve(), second.resolve()}
    assert backend.newest_scan_folder() != internal.resolve()


def test_newest_scan_folder_can_select_capture_root(tmp_path: Path) -> None:
    nested = tmp_path / "older"
    nested.mkdir()
    nested_image = nested / "image1.jpg"
    root_image = tmp_path / "image2.jpg"
    nested_image.write_bytes(b"old")
    root_image.write_bytes(b"new")
    os.utime(nested_image, ns=(1_000_000_000, 1_000_000_000))
    os.utime(root_image, ns=(2_000_000_000, 2_000_000_000))
    backend = CzurCaptureBackend(capture_root=tmp_path)

    assert backend.newest_scan_folder() == tmp_path.resolve()

def test_status_accepts_existing_window_without_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"setting": {}}), encoding="utf-8")
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=tmp_path / "missing-launcher",
        xdotool=str(xdotool),
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(backend, "_visible_windows", lambda: ["123"])
    monkeypatch.setattr(backend, "_focus", lambda window_id: calls.append(("focus", window_id)))
    monkeypatch.setattr(
        backend,
        "_activate_curved_books_mode",
        lambda window_id: calls.append((CURVED_BOOKS_HOTKEY, window_id)),
    )

    status = backend.status()
    assert status.connected is True
    assert status.ready is True

    backend.start(tmp_path / "session")
    assert calls == [("focus", "123"), (CURVED_BOOKS_HOTKEY, "123")]


def test_start_activates_curved_books_in_existing_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"setting": {}}), encoding="utf-8")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool="/usr/bin/xdotool",
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(backend, "_visible_windows", lambda: ["123"])
    monkeypatch.setattr(backend, "_focus", lambda window_id: calls.append(("focus", window_id)))
    monkeypatch.setattr(
        backend,
        "_activate_curved_books_mode",
        lambda window_id: calls.append((CURVED_BOOKS_HOTKEY, window_id)),
    )

    backend.start(tmp_path / "session")

    assert calls == [("focus", "123"), (CURVED_BOOKS_HOTKEY, "123")]

@pytest.mark.parametrize(
    ("raw_config", "message"),
    [
        ("{not-json", "not valid JSON"),
        ("[]", "root must be an object"),
        ('{"setting": []}', "setting must be an object"),
        ('{"setting": null}', "setting must be an object"),
    ],
)
def test_status_rejects_config_that_start_would_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_config: str,
    message: str,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(raw_config, encoding="utf-8")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])

    assert backend.status().ready is False
    with pytest.raises(CzurAdapterError, match=message):
        backend.apply_curved_books_preset()

def test_status_rejects_unwritable_config_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config-dir"
    config_dir.mkdir()
    config = config_dir / "config.json"
    config.write_text('{"setting": {}}', encoding="utf-8")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])
    original_access = os.access

    def access(path: os.PathLike[str] | str, mode: int) -> bool:
        if Path(path) == config_dir and mode == (os.W_OK | os.X_OK):
            return False
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", access)

    assert backend.status().ready is False
    with pytest.raises(CzurAdapterError, match="config directory is not writable"):
        backend.apply_curved_books_preset()


def test_status_handles_non_utf8_config_without_traceback(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_bytes(b"\xff")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )

    assert backend.status().ready is False
    with pytest.raises(CzurAdapterError, match="cannot be read as UTF-8"):
        backend.apply_curved_books_preset()


def test_status_handles_config_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"setting": {}}', encoding="utf-8")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    original_read_text = Path.read_text

    def fail_config_read(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if self == config:
            raise OSError("synthetic config read failure")
        return original_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", fail_config_read)

    assert backend.status().ready is False
    with pytest.raises(CzurAdapterError, match="cannot be read as UTF-8"):
        backend.apply_curved_books_preset()
