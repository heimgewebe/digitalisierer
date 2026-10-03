import json
import os
import signal
import threading
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


@pytest.mark.parametrize("failure_point", ["focus", "activate"])
def test_start_rolls_back_preset_when_existing_window_activation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
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
    monkeypatch.setattr(backend, "_visible_windows", lambda: ["42"])
    if failure_point == "focus":
        def fail_focus(window_id: str) -> None:
            raise CzurAdapterError(f"forced focus failure for {window_id}")

        monkeypatch.setattr(backend, "_focus", fail_focus)
    else:
        monkeypatch.setattr(backend, "_focus", lambda window_id: None)

        def fail_activate(window_id: str) -> None:
            raise CzurAdapterError(f"forced activate failure for {window_id}")

        monkeypatch.setattr(
            backend,
            "_activate_curved_books_mode",
            fail_activate,
        )

    before = config.read_bytes()
    before_mode = config.stat().st_mode & 0o7777
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    with pytest.raises(
        CzurAdapterError,
        match=f"forced {failure_point} failure",
    ):
        backend.start(output_dir)

    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o7777 == before_mode
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None



@pytest.mark.parametrize("failure_point", ["discover", "focus", "activate"])
def test_start_rolls_back_preset_after_launched_process_setup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
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

    class _FakeProcess:
        pid = 4242

        def __init__(self) -> None:
            self.exited = False

        def poll(self) -> int | None:
            return 0 if self.exited else None

    process = _FakeProcess()
    group_alive = True
    group_signals: list[signal.Signals] = []
    visible_calls = 0

    def fake_killpg(process_group_id: int, sent_signal: int) -> None:
        nonlocal group_alive
        assert process_group_id == process.pid
        if sent_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        group_signals.append(signal.Signals(sent_signal))
        if sent_signal == signal.SIGTERM:
            group_alive = False
            process.exited = True
            return
        pytest.fail(f"unexpected process-group signal: {sent_signal}")

    def visible_windows() -> list[str]:
        nonlocal visible_calls
        visible_calls += 1
        if visible_calls == 1:
            return []
        if failure_point == "discover":
            raise CzurAdapterError("forced post-launch discovery failure")
        return ["42"]

    def fake_popen(*args: object, **kwargs: object) -> _FakeProcess:
        return process

    monkeypatch.setattr(backend, "_visible_windows", visible_windows)
    monkeypatch.setattr("digitalisierer.czur.subprocess.Popen", fake_popen)
    monkeypatch.setattr("digitalisierer.czur.os.killpg", fake_killpg)

    if failure_point == "focus":
        def fail_focus(window_id: str) -> None:
            raise CzurAdapterError(f"forced post-launch focus failure for {window_id}")

        monkeypatch.setattr(backend, "_focus", fail_focus)
    else:
        monkeypatch.setattr(backend, "_focus", lambda window_id: None)

    if failure_point == "activate":
        def fail_activate(window_id: str) -> None:
            raise CzurAdapterError(
                f"forced post-launch activate failure for {window_id}"
            )

        monkeypatch.setattr(backend, "_activate_curved_books_mode", fail_activate)
    else:
        monkeypatch.setattr(
            backend,
            "_activate_curved_books_mode",
            lambda window_id: None,
        )

    before = config.read_bytes()
    before_mode = config.stat().st_mode & 0o7777
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    with pytest.raises(CzurAdapterError, match="forced post-launch"):
        backend.start(output_dir)

    assert group_signals == [signal.SIGTERM]
    assert group_alive is False
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o7777 == before_mode
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_start_preserves_preset_if_launched_process_cannot_be_stopped(
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

    class _UnstoppableProcess:
        pid = 4343

        def poll(self) -> None:
            return None

    process = _UnstoppableProcess()
    visible_calls = 0

    def fail_killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == process.pid
        if sent_signal == 0:
            return
        if sent_signal == signal.SIGTERM:
            raise PermissionError("forced stop failure")
        pytest.fail(f"unexpected process-group signal: {sent_signal}")

    def visible_windows() -> list[str]:
        nonlocal visible_calls
        visible_calls += 1
        if visible_calls == 1:
            return []
        raise CzurAdapterError("forced post-launch discovery failure")

    def fake_popen(*args: object, **kwargs: object) -> _UnstoppableProcess:
        return process

    monkeypatch.setattr(backend, "_visible_windows", visible_windows)
    monkeypatch.setattr("digitalisierer.czur.subprocess.Popen", fake_popen)
    monkeypatch.setattr("digitalisierer.czur.os.killpg", fail_killpg)

    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    with pytest.raises(
        CzurAdapterError,
        match="rollback incomplete: launched CZUR process-group termination failed",
    ):
        backend.start(output_dir)

    assert config.read_bytes() != before
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["setting"]["scan_preview_capture_type"] == "mul_page"
    assert backup.is_file()
    assert output_dir.is_dir()
    assert capture_root.is_dir()
    assert backend._session_output == output_dir



def test_stop_launched_process_reaps_zombie_leader_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ZombieLeader:
        pid = 4545

        def __init__(self) -> None:
            self.terminated = False
            self.reaped = False
            self.poll_calls = 0

        def poll(self) -> int | None:
            self.poll_calls += 1
            if self.terminated:
                self.reaped = True
                return 0
            return None

    process = _ZombieLeader()
    group_signals: list[signal.Signals] = []
    clock = iter((0.0, 0.0, 3.0))

    def fake_killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == process.pid
        if sent_signal == 0:
            if process.terminated and process.reaped:
                raise ProcessLookupError
            return
        group_signals.append(signal.Signals(sent_signal))
        if sent_signal == signal.SIGTERM:
            process.terminated = True
            return
        pytest.fail(f"unexpected process-group signal: {sent_signal}")

    monkeypatch.setattr("digitalisierer.czur.os.killpg", fake_killpg)
    monkeypatch.setattr("digitalisierer.czur.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("digitalisierer.czur.time.sleep", lambda _seconds: None)

    errors = CzurCaptureBackend._stop_launched_process(process)  # type: ignore[arg-type]

    assert errors == []
    assert group_signals == [signal.SIGTERM]
    assert process.poll_calls >= 1
    assert process.reaped is True


def test_stop_launched_process_stops_group_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExitedLeader:
        pid = 4444

        def poll(self) -> int:
            return 0

    group_alive = True
    group_signals: list[signal.Signals] = []

    def fake_killpg(process_group_id: int, sent_signal: int) -> None:
        nonlocal group_alive
        assert process_group_id == 4444
        if sent_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        group_signals.append(signal.Signals(sent_signal))
        if sent_signal == signal.SIGTERM:
            group_alive = False
            return
        pytest.fail(f"unexpected process-group signal: {sent_signal}")

    monkeypatch.setattr("digitalisierer.czur.os.killpg", fake_killpg)

    errors = CzurCaptureBackend._stop_launched_process(_ExitedLeader())  # type: ignore[arg-type]

    assert errors == []
    assert group_signals == [signal.SIGTERM]
    assert group_alive is False


def test_config_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    os.mkfifo(config)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=tmp_path / "launcher",
        xdotool="/usr/bin/xdotool",
    )
    errors: list[BaseException] = []

    def load_config() -> None:
        try:
            backend._load_config_payload()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=load_config, daemon=True)
    worker.start()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert len(errors) == 1
    assert isinstance(errors[0], CzurAdapterError)
    assert "config is not a regular file" in str(errors[0])


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


def test_start_rolls_back_if_launcher_becomes_unready_after_preset(
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
    readiness_calls = 0

    def launcher_ready() -> bool:
        nonlocal readiness_calls
        readiness_calls += 1
        return readiness_calls == 1

    monkeypatch.setattr(backend, "_launcher_ready", launcher_ready)
    before = config.read_bytes()
    before_mode = config.stat().st_mode & 0o7777
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")

    with pytest.raises(CzurAdapterError, match="launcher is not executable"):
        backend.start(output_dir)

    assert readiness_calls == 2
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


def test_start_does_not_overwrite_foreign_config_at_rollback_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "setting": {
                    "unrelated": "original",
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
    original_rename = os.rename
    foreign = b'{"setting":{"unrelated":"foreign-at-rollback"}}\n'
    raced = False

    def racing_rename(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
    ) -> None:
        nonlocal raced
        destination = Path(dst)
        if (
            not raced
            and Path(src) == config
            and destination.name == "published"
            and destination.parent.name.startswith(
                ".config.json.digitalisierer.rollback-claim."
            )
        ):
            raced = True
            replacement = tmp_path / "foreign-config.json"
            replacement.write_bytes(foreign)
            replacement.chmod(0o644)
            os.replace(replacement, config)
        original_rename(src, dst)

    def fail_launcher(*args: object, **kwargs: object) -> None:
        raise OSError("forced launcher failure")

    monkeypatch.setattr("digitalisierer.czur.os.rename", racing_rename)
    monkeypatch.setattr("digitalisierer.czur.subprocess.Popen", fail_launcher)

    with pytest.raises(
        CzurAdapterError,
        match="CZUR config changed after preset; rollback refused",
    ):
        backend.start(output_dir)

    assert raced is True
    assert config.read_bytes() == foreign
    assert config.stat().st_mode & 0o7777 == 0o644
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_start_does_not_restore_snapshot_older_than_preset_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "setting": {
                    "unrelated": "original",
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
    original_snapshot = backend._config_snapshot
    calls = 0
    foreign = (
        json.dumps(
            {
                "setting": {
                    "unrelated": "foreign",
                    "scan_preview_capture_type": "foreign-single",
                }
            }
        )
        + "\n"
    ).encode("utf-8")

    def snapshot_then_foreign_change(
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        nonlocal calls
        snapshot = original_snapshot()
        calls += 1
        if calls == 1:
            config.write_bytes(foreign)
            config.chmod(0o644)
        return snapshot

    monkeypatch.setattr(backend, "_config_snapshot", snapshot_then_foreign_change)

    with pytest.raises(
        CzurAdapterError,
        match="changed before Curved Books preset could be applied",
    ):
        backend.start(output_dir)

    assert config.read_bytes() == foreign
    assert config.stat().st_mode & 0o7777 == 0o644
    assert not config.with_name("config.pre-digitalisierer-curved-books.json").exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_start_does_not_overwrite_foreign_config_created_at_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "setting": {
                    "unrelated": "original",
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
    original_link = os.link
    raced = False
    before = config.read_bytes()
    foreign = b'{"setting":{"unrelated":"foreign-at-publication"}}\n'

    def racing_link(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal raced
        if (
            not raced
            and Path(dst) == config
            and Path(src).name.startswith(".config.json.digitalisierer.")
        ):
            raced = True
            config.write_bytes(foreign)
            config.chmod(0o644)
        original_link(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("digitalisierer.czur.os.link", racing_link)

    with pytest.raises(
        CzurAdapterError,
        match="changed immediately before Curved Books preset publication",
    ):
        backend.start(output_dir)

    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    assert raced is True
    assert config.read_bytes() == foreign
    assert config.stat().st_mode & 0o7777 == 0o644
    assert backup.read_bytes() == before
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_apply_preset_uses_same_fail_if_exists_publication_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    backend = CzurCaptureBackend(config_path=config)
    original_link = os.link
    raced = False
    before = config.read_bytes()
    foreign = b'{"setting":{"scan_preview_capture_type":"foreign"}}\n'

    def racing_link(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal raced
        if (
            not raced
            and Path(dst) == config
            and Path(src).name.startswith(".config.json.digitalisierer.")
        ):
            raced = True
            config.write_bytes(foreign)
        original_link(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("digitalisierer.czur.os.link", racing_link)

    with pytest.raises(
        CzurAdapterError,
        match="changed immediately before Curved Books preset publication",
    ):
        backend.apply_curved_books_preset()

    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    assert raced is True
    assert config.read_bytes() == foreign
    assert backup.read_bytes() == before



def test_apply_preset_crash_after_config_claim_keeps_durable_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "setting": {
                    "unrelated": "original",
                    "scan_preview_capture_type": "single",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config.chmod(0o640)
    backend = CzurCaptureBackend(config_path=config)
    before = config.read_bytes()
    original_rename = os.rename
    crashed = False

    def crash_after_claim(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
    ) -> None:
        nonlocal crashed
        destination = Path(dst)
        if (
            not crashed
            and Path(src) == config
            and destination.name == "preimage"
            and destination.parent.name.startswith(
                ".config.json.digitalisierer.claim."
            )
        ):
            original_rename(src, dst)
            crashed = True
            raise SystemExit("synthetic crash after config claim")
        original_rename(src, dst)

    monkeypatch.setattr("digitalisierer.czur.os.rename", crash_after_claim)

    with pytest.raises(SystemExit, match="synthetic crash after config claim"):
        backend.apply_curved_books_preset()

    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    assert crashed is True
    assert backup.read_bytes() == before
    claims = list(tmp_path.glob(".config.json.digitalisierer.claim.*/preimage"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == before
    assert not config.exists()


def test_start_keeps_preimage_backup_when_foreign_write_follows_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "setting": {
                    "unrelated": "original",
                    "scan_preview_capture_type": "single",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
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
    original_fsync_directory = backend._fsync_directory
    foreign_written = False
    before = config.read_bytes()
    foreign = b'{"setting":{"unrelated":"foreign-after-publication"}}\n'

    def fsync_then_foreign_write(directory: Path) -> None:
        nonlocal foreign_written
        original_fsync_directory(directory)
        if (
            not foreign_written
            and directory == config.parent
            and config.is_file()
            and config.read_bytes() != before
        ):
            foreign_written = True
            config.write_bytes(foreign)

    monkeypatch.setattr(backend, "_fsync_directory", fsync_then_foreign_write)

    with pytest.raises(
        CzurAdapterError,
        match="changed while Curved Books preset was being applied",
    ):
        backend.start(output_dir)

    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    assert foreign_written is True
    assert config.read_bytes() == foreign
    assert backup.read_bytes() == before
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


@pytest.mark.parametrize("backup_kind", ["file", "symlink"])
def test_apply_preset_leaves_existing_backup_untouched(
    tmp_path: Path,
    backup_kind: str,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    target = tmp_path / "foreign-backup-target"
    if backup_kind == "file":
        backup.write_bytes(b"foreign-backup\n")
    else:
        target.write_bytes(b"foreign-symlink-target\n")
        backup.symlink_to(target)
    before_lstat = backup.lstat()
    before_bytes = target.read_bytes() if backup_kind == "symlink" else backup.read_bytes()
    before_target = os.readlink(backup) if backup_kind == "symlink" else None
    backend = CzurCaptureBackend(config_path=config)

    backend.apply_curved_books_preset()

    after_lstat = backup.lstat()
    assert (after_lstat.st_dev, after_lstat.st_ino, after_lstat.st_mode) == (
        before_lstat.st_dev,
        before_lstat.st_ino,
        before_lstat.st_mode,
    )
    if backup_kind == "symlink":
        assert backup.is_symlink()
        assert os.readlink(backup) == before_target
        assert target.read_bytes() == before_bytes
    else:
        assert backup.read_bytes() == before_bytes


def test_failed_preset_cleanup_preserves_foreign_backup_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_link = os.link
    original_rename = os.rename
    foreign = b"foreign-backup-replacement\n"
    cleanup_raced = False

    def fail_publish_link(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        if (
            Path(dst) == config
            and Path(src).name.startswith(".config.json.digitalisierer.")
        ):
            raise OSError("forced preset publication failure")
        original_link(src, dst, follow_symlinks=follow_symlinks)

    def race_backup_cleanup(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
    ) -> None:
        nonlocal cleanup_raced
        source = Path(src)
        destination = Path(dst)
        if (
            not cleanup_raced
            and source == backup
            and destination.name == "backup"
        ):
            cleanup_raced = True
            replacement = tmp_path / "foreign-backup"
            replacement.write_bytes(foreign)
            os.replace(replacement, backup)
        original_rename(src, dst)

    monkeypatch.setattr("digitalisierer.czur.os.link", fail_publish_link)
    monkeypatch.setattr("digitalisierer.czur.os.rename", race_backup_cleanup)

    with pytest.raises(
        CzurAdapterError,
        match="CZUR backup rollback refused",
    ):
        backend.apply_curved_books_preset()

    assert cleanup_raced is True
    assert backup.read_bytes() == foreign


def test_start_rejects_symlinked_config_and_rolls_back_created_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "real-config.json"
    target.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = target.read_bytes()
    config = tmp_path / "config.json"
    config.symlink_to(target)
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

    with pytest.raises(CzurAdapterError, match="config is not a regular file"):
        backend.start(output_dir)

    assert config.is_symlink()
    assert target.read_bytes() == before
    assert not config.with_name("config.pre-digitalisierer-curved-books.json").exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_start_rolls_back_new_output_when_capture_root_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
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
    original_mkdir = Path.mkdir

    def fail_capture_root_mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == capture_root:
            raise OSError("synthetic capture-root mkdir failure")
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", fail_capture_root_mkdir)

    with pytest.raises(OSError, match="synthetic capture-root mkdir failure"):
        backend.start(output_dir)

    assert config.read_bytes() == before
    assert not config.with_name("config.pre-digitalisierer-curved-books.json").exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_start_preset_failure_preserves_existing_output_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    xdotool.chmod(0o700)
    capture_root = tmp_path / "captures"
    output_dir = tmp_path / "session"
    capture_root.mkdir()
    output_dir.mkdir()
    backend = CzurCaptureBackend(
        capture_root=capture_root,
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )
    monkeypatch.setattr(backend, "_visible_windows", lambda: [])
    original_snapshot = backend._config_snapshot
    calls = 0
    foreign = b'{"setting":{"scan_preview_capture_type":"foreign"}}\n'

    def snapshot_then_foreign_change(
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        nonlocal calls
        snapshot = original_snapshot()
        calls += 1
        if calls == 1:
            config.write_bytes(foreign)
        return snapshot

    monkeypatch.setattr(backend, "_config_snapshot", snapshot_then_foreign_change)

    with pytest.raises(
        CzurAdapterError,
        match="changed before Curved Books preset could be applied",
    ):
        backend.start(output_dir)

    assert config.read_bytes() == foreign
    assert output_dir.is_dir()
    assert capture_root.is_dir()
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
    original_snapshot = backend._snapshot_regular_file

    def fail_config_read(
        path: Path,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        if path == config:
            raise OSError("synthetic config read failure")
        return original_snapshot(path)

    monkeypatch.setattr(backend, "_snapshot_regular_file", fail_config_read)

    assert backend.status().ready is False

def test_status_rejects_xdotool_display_failure_before_start_side_effects(
    tmp_path: Path,
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
    xdotool.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"Error: Can't open display: (null)\" >&2\n"
        "printf '%s\\n' \"Failed creating new xdo instance\" >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
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

    status = backend.status()

    assert status.ready is False
    assert status.connected is False
    assert "Can't open display" in status.detail
    with pytest.raises(
        CzurAdapterError,
        match="xdotool window search failed: Error: Can't open display",
    ):
        backend.start(output_dir)

    assert config.read_bytes() == before
    assert not backup.exists()
    assert not output_dir.exists()
    assert not capture_root.exists()
    assert backend._session_output is None


def test_status_accepts_xdotool_search_no_match_without_stderr(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"setting": {}}', encoding="utf-8")
    launcher = tmp_path / "czur-scanner"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    xdotool = tmp_path / "xdotool"
    xdotool.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    xdotool.chmod(0o700)
    backend = CzurCaptureBackend(
        capture_root=tmp_path / "captures",
        config_path=config,
        launcher=launcher,
        xdotool=str(xdotool),
    )

    assert backend._visible_windows() == []
    status = backend.status()
    assert status.connected is False
    assert status.ready is True


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


def test_backup_fsync_failure_removes_exact_new_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_fsync = os.fsync
    failed = False

    def fail_new_backup_fsync(descriptor: int) -> None:
        nonlocal failed
        try:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            target = None
        if (
            not failed
            and target is not None
            and target.name == "backup"
            and target.parent.name.startswith(
                ".config.json.digitalisierer.backup-stage."
            )
        ):
            failed = True
            raise OSError("synthetic backup fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr("digitalisierer.czur.os.fsync", fail_new_backup_fsync)

    with pytest.raises(OSError, match="synthetic backup fsync failure"):
        backend._create_backup_if_absent(backup, before)

    assert failed is True
    assert not os.path.lexists(backup)


def test_backup_fsync_failure_needs_no_post_failure_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_fsync = os.fsync
    original_mkdtemp = tempfile.mkdtemp
    allocations = 0
    fsync_failed = False

    def allow_only_initial_stage_allocation(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        nonlocal allocations
        allocations += 1
        if allocations > 1:
            raise OSError(28, "No space left on device")
        return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

    def fail_staged_backup_fsync(descriptor: int) -> None:
        nonlocal fsync_failed
        try:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            target = None
        if (
            not fsync_failed
            and target is not None
            and target.name == "backup"
            and target.parent.name.startswith(
                ".config.json.digitalisierer.backup-stage."
            )
        ):
            fsync_failed = True
            raise OSError("synthetic backup fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(
        "digitalisierer.czur.tempfile.mkdtemp",
        allow_only_initial_stage_allocation,
    )
    monkeypatch.setattr("digitalisierer.czur.os.fsync", fail_staged_backup_fsync)

    with pytest.raises(OSError, match="synthetic backup fsync failure"):
        backend._create_backup_if_absent(backup, before)

    assert allocations == 1
    assert fsync_failed is True
    assert not os.path.lexists(backup)
    assert not list(
        tmp_path.glob(".config.json.digitalisierer.backup-stage.*")
    )

def test_backup_content_is_durable_before_canonical_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_fsync = os.fsync
    canonical_visible_during_content_fsync: bool | None = None

    def fail_content_fsync(descriptor: int) -> None:
        nonlocal canonical_visible_during_content_fsync
        try:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            target = None
        if (
            canonical_visible_during_content_fsync is None
            and target is not None
            and target.name == "backup"
            and target.parent.name.startswith(
                ".config.json.digitalisierer.backup-stage."
            )
        ):
            canonical_visible_during_content_fsync = os.path.lexists(backup)
            raise OSError("synthetic backup content fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr("digitalisierer.czur.os.fsync", fail_content_fsync)

    with pytest.raises(OSError, match="synthetic backup content fsync failure"):
        backend._create_backup_if_absent(backup, before)

    assert canonical_visible_during_content_fsync is False
    assert not os.path.lexists(backup)


def test_backup_publication_enospc_leaves_no_canonical_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_link = os.link
    publication_attempted = False

    def fail_backup_publication(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal publication_attempted
        if Path(dst) == backup:
            publication_attempted = True
            assert Path(src).parent.name.startswith(
                ".config.json.digitalisierer.backup-stage."
            )
            raise OSError(28, "No space left on device")
        original_link(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("digitalisierer.czur.os.link", fail_backup_publication)

    with pytest.raises(OSError, match="No space left on device"):
        backend._create_backup_if_absent(backup, before)

    assert publication_attempted is True
    assert not os.path.lexists(backup)
    assert not list(
        tmp_path.glob(".config.json.digitalisierer.backup-stage.*")
    )


@pytest.mark.parametrize("failure_point", ["snapshot", "directory-fsync"])
def test_backup_post_publication_failure_rolls_back_without_new_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import tempfile

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_snapshot = backend._snapshot_regular_file
    original_fsync_directory = backend._fsync_directory
    original_mkdtemp = tempfile.mkdtemp
    failed = False

    def forbid_allocation_after_backup_publication(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        if os.path.lexists(backup):
            raise OSError(28, "No space left on device")
        return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

    def fail_post_publication_snapshot(
        path: Path,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        nonlocal failed
        if (
            failure_point == "snapshot"
            and not failed
            and path == backup
            and os.path.lexists(backup)
        ):
            failed = True
            raise OSError("synthetic post-publication backup snapshot failure")
        return original_snapshot(path)

    def fail_post_publication_directory_fsync(directory: Path) -> None:
        nonlocal failed
        if (
            failure_point == "directory-fsync"
            and not failed
            and directory == backup.parent
            and os.path.lexists(backup)
        ):
            failed = True
            raise OSError("synthetic post-publication backup directory fsync failure")
        original_fsync_directory(directory)

    monkeypatch.setattr(
        "digitalisierer.czur.tempfile.mkdtemp",
        forbid_allocation_after_backup_publication,
    )
    monkeypatch.setattr(
        backend,
        "_snapshot_regular_file",
        fail_post_publication_snapshot,
    )
    monkeypatch.setattr(
        backend,
        "_fsync_directory",
        fail_post_publication_directory_fsync,
    )

    with pytest.raises(OSError, match="synthetic post-publication backup"):
        backend.apply_curved_books_preset()

    assert failed is True
    assert config.read_bytes() == before
    assert not os.path.lexists(backup)
    assert not list(
        tmp_path.glob(".config.json.digitalisierer.backup-stage.*")
    )


def test_backup_helper_does_not_validate_staging_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_snapshot = backend._snapshot_regular_file
    post_link_staging_snapshot = False

    def reject_staging_snapshot_after_publication(
        path: Path,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        nonlocal post_link_staging_snapshot
        if (
            path.name == "backup"
            and path.parent.name.startswith(
                ".config.json.digitalisierer.backup-stage."
            )
            and os.path.lexists(backup)
        ):
            post_link_staging_snapshot = True
            raise OSError("synthetic forbidden post-link staging snapshot")
        return original_snapshot(path)

    monkeypatch.setattr(
        backend,
        "_snapshot_regular_file",
        reject_staging_snapshot_after_publication,
    )

    backend.apply_curved_books_preset()

    assert post_link_staging_snapshot is False
    assert backup.read_bytes() == before
    assert not list(
        tmp_path.glob(".config.json.digitalisierer.backup-stage.*")
    )


def test_preexisting_stale_backup_gets_durable_transaction_recovery_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"setting": {"scan_preview_capture_type": "single"}}) + "\n",
        encoding="utf-8",
    )
    config.chmod(0o640)
    before = config.read_bytes()
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    stale_backup = b'{"setting": {"stale": true}}\n'
    backup.write_bytes(stale_backup)
    backend = CzurCaptureBackend(config_path=config)
    original_rename = os.rename
    crashed = False

    def crash_after_claim(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
    ) -> None:
        nonlocal crashed
        destination = Path(dst)
        if (
            not crashed
            and Path(src) == config
            and destination.name == "preimage"
            and destination.parent.name.startswith(
                ".config.json.digitalisierer.claim."
            )
        ):
            original_rename(src, dst)
            crashed = True
            raise SystemExit("synthetic crash after config claim")
        original_rename(src, dst)

    monkeypatch.setattr("digitalisierer.czur.os.rename", crash_after_claim)

    with pytest.raises(SystemExit, match="synthetic crash after config claim"):
        backend.apply_curved_books_preset()

    assert crashed is True
    assert backup.read_bytes() == stale_backup
    claims = list(tmp_path.glob(".config.json.digitalisierer.claim.*/preimage"))
    recoveries = list(tmp_path.glob(".config.json.digitalisierer.claim.*/recovery"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == before
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == before
    assert not config.exists()


def test_final_preset_sync_failure_restores_canonical_config(
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
    before = config.read_bytes()
    before_mode = config.stat().st_mode & 0o7777
    backup = config.with_name("config.pre-digitalisierer-curved-books.json")
    backend = CzurCaptureBackend(config_path=config)
    original_snapshot = backend._snapshot_regular_file
    original_fsync_directory = backend._fsync_directory
    claimed_snapshots = 0
    final_preimage_verified = False
    failed = False

    def mark_final_preimage_verification(
        path: Path,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        nonlocal claimed_snapshots, final_preimage_verified
        result = original_snapshot(path)
        if (
            path.name == "preimage"
            and path.parent.name.startswith(".config.json.digitalisierer.claim.")
        ):
            claimed_snapshots += 1
            if claimed_snapshots == 2:
                final_preimage_verified = True
        return result

    def fail_commit_sync(directory: Path) -> None:
        nonlocal failed
        if (
            not failed
            and final_preimage_verified
            and directory == config.parent
        ):
            failed = True
            raise OSError("synthetic final publication sync failure")
        original_fsync_directory(directory)

    monkeypatch.setattr(
        backend,
        "_snapshot_regular_file",
        mark_final_preimage_verification,
    )
    monkeypatch.setattr(backend, "_fsync_directory", fail_commit_sync)

    with pytest.raises(OSError, match="synthetic final publication sync failure"):
        backend.apply_curved_books_preset()

    assert failed is True
    assert claimed_snapshots == 2
    assert config.is_file()
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o7777 == before_mode
    assert not backup.exists()
