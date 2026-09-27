from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .ports import CaptureStatus


class CzurAdapterError(RuntimeError):
    """Raised when the official CZUR application cannot be prepared safely."""


CURVED_BOOKS_HOTKEY = "alt+2"
CURVED_BOOKS_SETTINGS: dict[str, object] = {
    "scan_preview_capture_type": "mul_page",
    "scan_preview_parameter_setting_mul_flatten": True,
    "scan_preview_parameter_setting_mul_remove_finger": True,
    "scan_preview_parameter_setting_mul_auto_split_page": True,
    "scan_preview_parameter_setting_mul_auto_split_page_type": "both",
    "scan_preview_parameter_setting_mul_order": "positive",
    "global_power_frequency": 50,
    "global_show_start_type_window": False,
}


def _natural_key(path: Path) -> list[int | str]:
    import re

    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


class CzurCaptureBackend:
    name = "czur-official-linux"

    def __init__(
        self,
        *,
        capture_root: Path | None = None,
        config_path: Path | None = None,
        launcher: Path | None = None,
        xdotool: str | None = None,
    ) -> None:
        home = Path.home()
        self.capture_root = (capture_root or home / "CZURScannerDoc").expanduser()
        self.config_path = (
            config_path or home / ".czur" / "ScannerInfo" / "config.json"
        ).expanduser()
        self.launcher = (launcher or home / ".local" / "bin" / "czur-scanner").expanduser()
        self.xdotool = xdotool or shutil.which("xdotool") or "xdotool"
        self._session_output: Path | None = None

    def _load_config_payload(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            raise CzurAdapterError(f"CZUR config is missing: {self.config_path}")
        try:
            raw = self.config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CzurAdapterError(
                f"CZUR config cannot be read as UTF-8: {self.config_path}"
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CzurAdapterError("CZUR config is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CzurAdapterError("CZUR config root must be an object")
        setting = payload.get("setting")
        if setting is not None and not isinstance(setting, dict):
            raise CzurAdapterError("CZUR config setting must be an object")
        return payload

    def status(self) -> CaptureStatus:
        executable_ready = self.launcher.is_file() and os.access(
            self.launcher, os.X_OK
        )
        try:
            self._load_config_payload()
        except CzurAdapterError:
            config_ready = False
        else:
            config_ready = True
        xdotool_ready = shutil.which(self.xdotool) is not None or Path(
            self.xdotool
        ).is_file()
        windows = self._visible_windows() if xdotool_ready else []
        return CaptureStatus(
            connected=bool(windows),
            ready=executable_ready and config_ready and xdotool_ready,
            detail=(
                "official CZUR app + Curved Books preset + capture-folder observation"
            ),
        )

    def apply_curved_books_preset(self) -> dict[str, object]:
        payload = self._load_config_payload()
        setting = payload.setdefault("setting", {})
        if not isinstance(setting, dict):
            raise CzurAdapterError("CZUR config setting must be an object")
        before: dict[str, object] = {}
        for key, value in CURVED_BOOKS_SETTINGS.items():
            before[key] = setting.get(key)
            setting[key] = value

        backup = self.config_path.with_name(
            "config.pre-digitalisierer-curved-books.json"
        )
        if not backup.exists():
            shutil.copy2(self.config_path, backup)

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".config.json.digitalisierer.",
            dir=str(self.config_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.config_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return before

    def _visible_windows(self) -> list[str]:
        completed = subprocess.run(
            [self.xdotool, "search", "--onlyvisible", "--class", "CzurScanner"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            return []
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    def _focus(self, window_id: str) -> None:
        completed = subprocess.run(
            [self.xdotool, "windowactivate", window_id],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise CzurAdapterError(
                f"cannot focus CZUR window {window_id}: "
                f"{(completed.stderr or '').strip()}"
            )

    def _activate_curved_books_mode(self, window_id: str) -> None:
        completed = subprocess.run(
            [
                self.xdotool,
                "key",
                "--window",
                window_id,
                "--clearmodifiers",
                CURVED_BOOKS_HOTKEY,
            ],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise CzurAdapterError(
                f"cannot activate CZUR Curved Books mode in window {window_id}: "
                f"{(completed.stderr or '').strip()}"
            )

    def start(self, output_dir: Path) -> None:
        self._session_output = output_dir.expanduser()
        self._session_output.mkdir(parents=True, exist_ok=True)
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.apply_curved_books_preset()

        windows = self._visible_windows()
        if windows:
            self._focus(windows[-1])
            self._activate_curved_books_mode(windows[-1])
            return

        if not self.launcher.is_file() or not os.access(self.launcher, os.X_OK):
            raise CzurAdapterError(f"CZUR launcher is not executable: {self.launcher}")
        subprocess.Popen(
            [str(self.launcher)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            windows = self._visible_windows()
            if windows:
                self._focus(windows[-1])
                self._activate_curved_books_mode(windows[-1])
                return
            time.sleep(0.25)
        raise CzurAdapterError("CZUR application did not expose a visible window")

    def capture(self) -> None:
        capture_key = os.environ.get("DIGITALISIERER_CZUR_CAPTURE_KEY")
        if not capture_key:
            raise CzurAdapterError(
                "no capture key is configured; use CZUR auto-photo/manual capture "
                "and let Digitalisierer observe the capture folder"
            )
        windows = self._visible_windows()
        if not windows:
            raise CzurAdapterError("CZUR application has no visible window")
        self._focus(windows[-1])
        completed = subprocess.run(
            [self.xdotool, "key", "--clearmodifiers", capture_key],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise CzurAdapterError(
                f"CZUR capture key failed: {(completed.stderr or '').strip()}"
            )

    def stop(self) -> None:
        self._session_output = None

    def newest_scan_folder(self) -> Path:
        if not self.capture_root.is_dir():
            raise CzurAdapterError(
                f"CZUR capture root is missing: {self.capture_root}"
            )
        candidates: list[tuple[int, Path]] = []
        directories = [self.capture_root]
        directories.extend(
            directory
            for directory in self.capture_root.iterdir()
            if directory.is_dir() and not directory.name.startswith("_")
        )
        for directory in directories:
            images = [
                path
                for path in directory.iterdir()
                if path.is_file()
                and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            ]
            if not images:
                continue
            newest = max(path.stat().st_mtime_ns for path in images)
            candidates.append((newest, directory))
        if not candidates:
            raise CzurAdapterError(
                f"no CZUR scan folder with images at or below {self.capture_root}"
            )
        return max(candidates, key=lambda item: item[0])[1].resolve()

    def image_files(self, folder: Path) -> list[Path]:
        return sorted(
            [
                path
                for path in folder.iterdir()
                if path.is_file()
                and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            ],
            key=_natural_key,
        )