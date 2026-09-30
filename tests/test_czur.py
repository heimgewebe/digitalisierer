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


def test_newest_scan_folder_ignores_symlinked_directory_outside_capture_root(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    legitimate = capture_root / "legitimate"
    legitimate.mkdir()
    legitimate_image = legitimate / "image1.jpg"
    legitimate_image.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_image = outside / "image2.jpg"
    outside_image.write_bytes(b"outside")
    os.utime(legitimate_image, ns=(1_000_000_000, 1_000_000_000))
    os.utime(outside_image, ns=(2_000_000_000, 2_000_000_000))
    (capture_root / "escape").symlink_to(outside, target_is_directory=True)
    backend = CzurCaptureBackend(capture_root=capture_root)

    assert backend.newest_scan_folder() == legitimate.resolve()


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
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
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

    backend.start(tmp_path / "session")

    assert calls == [("focus", "123"), (CURVED_BOOKS_HOTKEY, "123")]

def test_start_rejects_missing_xdotool_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}),
        encoding="utf-8",
    )
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    capture_root = tmp_path / "captures"
    output_dir = tmp_path / "session"
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(tmp_path / "missing-xdotool"),
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    def unexpected_visible_windows() -> list[str]:
        pytest.fail("window discovery must not run without a usable xdotool")

    monkeypatch.setattr(backend, "_visible_windows", unexpected_visible_windows)

    with pytest.raises(CzurAdapterError, match="xdotool is not executable"):
        backend.start(output_dir)

    assert config.read_bytes() == before
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


@pytest.mark.parametrize("launcher_state", ["missing", "non-executable"])
def test_start_rejects_unusable_launcher_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launcher_state: str,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}),
        encoding="utf-8",
    )
    launcher = tmp_path / "czur-scanner"
    if launcher_state == "non-executable":
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o600)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    capture_root = tmp_path / "captures"
    output_dir = tmp_path / "session"
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    with pytest.raises(CzurAdapterError, match="launcher is not executable"):
        backend.start(output_dir)

    assert config.read_bytes() == before
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_start_rolls_back_preset_when_executable_launcher_cannot_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "setting": {
                    "unrelated": "keep",
                    "scan_preview_capture_type": "single",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config.chmod(0o640)
    launcher = tmp_path / "czur-scanner"
    launcher.write_text(
        "#!/definitely/missing/digitalisierer-interpreter\nexit 0\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    xdotool.chmod(0o700)
    capture_root = tmp_path / "captures"
    output_dir = tmp_path / "session"
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])
    before = config.read_bytes()
    before_mode = config.stat().st_mode & 0o7777
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    assert backend._launcher_ready() is True
    with pytest.raises(CzurAdapterError, match="cannot execute CZUR launcher"):
        backend.start(output_dir)

    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o7777 == before_mode
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_start_refuses_rollback_after_foreign_config_mode_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "setting": {
                    "unrelated": "keep",
                    "scan_preview_capture_type": "single",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config.chmod(0o640)
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    xdotool.chmod(0o700)
    capture_root = tmp_path / "captures"
    output_dir = tmp_path / "session"
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    def fail_after_mode_change(*args: object, **kwargs: object) -> None:
        config.chmod(0o644)
        raise OSError("forced launcher failure")

    monkeypatch.setattr("digitalisierer.czur.subprocess.Popen", fail_after_mode_change)

    with pytest.raises(
        CzurAdapterError,
        match="CZUR config changed after preset; rollback refused",
    ):
        backend.start(output_dir)

    assert config.read_bytes() != before
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["setting"]["scan_preview_capture_type"] == "mul_page"
    assert config.stat().st_mode & 0o7777 == 0o644
    assert backup.is_file()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


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


def test_status_rejects_capture_root_that_is_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"setting": {}}', encoding="utf-8")
    capture_root = tmp_path / "captures"
    capture_root.write_text("not a directory", encoding="utf-8")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])

    assert backend.status().ready is False
    with pytest.raises(CzurAdapterError, match="capture root cannot be created or used"):
        backend.start(tmp_path / "session")


def test_status_rejects_dangling_capture_root_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"setting": {}}', encoding="utf-8")
    capture_root = tmp_path / "captures"
    capture_root.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])

    assert backend.status().ready is False
    with pytest.raises(CzurAdapterError, match="capture root cannot be created or used"):
        backend.start(tmp_path / "session")


def test_status_rejects_unwritable_capture_root_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"setting": {}}', encoding="utf-8")
    capture_parent = tmp_path / "capture-parent"
    capture_parent.mkdir()
    capture_root = capture_parent / "nested" / "captures"
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])
    original_access = os.access

    def access(path: os.PathLike[str] | str, mode: int) -> bool:
        if Path(path) == capture_parent and mode == (os.W_OK | os.X_OK):
            return False
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", access)

    assert backend.status().ready is False
    with pytest.raises(CzurAdapterError, match="capture root cannot be created or used"):
        backend.start(tmp_path / "session")


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

def test_status_handles_xdotool_execution_failure_before_start_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}),
        encoding="utf-8",
    )
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o700)
    capture_root = tmp_path / "captures"
    output_dir = tmp_path / "session"
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    def fail_xdotool(*args: object, **kwargs: object) -> object:
        raise OSError("synthetic xdotool execution failure")

    monkeypatch.setattr("digitalisierer.czur.subprocess.run", fail_xdotool)

    status = backend.status()
    assert status.ready is False
    assert status.connected is False

    with pytest.raises(CzurAdapterError, match="cannot execute xdotool"):
        backend.start(output_dir)

    assert config.read_bytes() == before
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_status_rejects_non_executable_xdotool_path(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"setting": {}}', encoding="utf-8")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\n", encoding="utf-8")
    xdotool.chmod(0o600)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )

    status = backend.status()

    assert status.ready is False
    assert status.connected is False