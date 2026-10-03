import json
from pathlib import Path

from PIL import Image
import pytest

import digitalisierer.scanner as scanner_module
from digitalisierer.czur import CzurCaptureBackend
from digitalisierer.scanner import create_or_resume_scan_session, observe_scan_folder


def _image(path: Path, value: int) -> None:
    Image.new("RGB", (64, 96), color=(value, value, value)).save(
        path,
        format="JPEG",
    )


def test_observe_keyboard_interrupt_rolls_back_new_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "capture-keyboard-interrupt"
    capture.mkdir()
    _image(capture / "image00001.jpg", 120)
    paths = create_or_resume_scan_session(
        "book",
        "keyboard-interrupt",
        tmp_path / "library",
    )
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }

    def interrupt_preview(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        scanner_module,
        "_verified_preview_source_snapshot",
        interrupt_preview,
    )

    with pytest.raises(KeyboardInterrupt):
        observe_scan_folder(paths, capture)

    assert list(paths.sources.iterdir()) == []
    assert list(paths.thumbnails.iterdir()) == []
    assert {path: path.read_bytes() for path in before} == before


def test_start_rolls_back_launched_process_on_keyboard_interrupt(
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

    process = object()
    visible_calls = 0

    def visible_windows() -> list[str]:
        nonlocal visible_calls
        visible_calls += 1
        if visible_calls == 1:
            return []
        raise KeyboardInterrupt

    stopped: list[object] = []

    def stop_process(launched: object) -> list[str]:
        stopped.append(launched)
        return []

    monkeypatch.setattr(backend, "_visible_windows", visible_windows)
    monkeypatch.setattr(
        "digitalisierer.czur.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(backend, "_stop_launched_process", stop_process)

    before = config.read_bytes()
    before_mode = config.stat().st_mode & 0o7777
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    with pytest.raises(KeyboardInterrupt):
        backend.start(output_dir)

    assert stopped == [process]
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o7777 == before_mode
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None
