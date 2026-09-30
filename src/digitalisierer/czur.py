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
        if "setting" in payload and not isinstance(payload["setting"], dict):
            raise CzurAdapterError("CZUR config setting must be an object")
        return payload

    def _config_write_ready(self) -> bool:
        parent = self.config_path.parent
        return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)

    def _require_config_write_ready(self) -> None:
        if not self._config_write_ready():
            raise CzurAdapterError(
                f"CZUR config directory is not writable: {self.config_path.parent}"
            )

    def _capture_root_ready(self) -> bool:
        candidate = self.capture_root
        current = candidate
        while not os.path.lexists(current) and current != current.parent:
            current = current.parent
        if not current.exists() or not current.is_dir():
            return False
        return os.access(current, os.W_OK | os.X_OK)

    def _require_capture_root_ready(self) -> None:
        if not self._capture_root_ready():
            raise CzurAdapterError(
                f"CZUR capture root cannot be created or used: {self.capture_root}"
            )

    def _launcher_ready(self) -> bool:
        return self.launcher.is_file() and os.access(self.launcher, os.X_OK)

    def _xdotool_ready(self) -> bool:
        xdotool_path = Path(self.xdotool)
        return shutil.which(self.xdotool) is not None or (
            xdotool_path.is_file() and os.access(xdotool_path, os.X_OK)
        )

    def _start_preflight(self) -> list[str]:
        if not self._xdotool_ready():
            raise CzurAdapterError(f"xdotool is not executable: {self.xdotool}")
        windows = self._visible_windows()
        if not windows and not self._launcher_ready():
            raise CzurAdapterError(f"CZUR launcher is not executable: {self.launcher}")
        self._load_config_payload()
        self._require_config_write_ready()
        self._require_capture_root_ready()
        return windows

    def status(self) -> CaptureStatus:
        executable_ready = self._launcher_ready()
        try:
            self._load_config_payload()
        except CzurAdapterError:
            config_ready = False
        else:
            config_ready = self._config_write_ready()
        xdotool_ready = self._xdotool_ready()
        if xdotool_ready:
            try:
                windows = self._visible_windows()
            except CzurAdapterError:
                xdotool_ready = False
                windows = []
        else:
            windows = []
        connected = bool(windows)
        capture_root_ready = self._capture_root_ready()
        return CaptureStatus(
            connected=connected,
            ready=(
                config_ready
                and capture_root_ready
                and xdotool_ready
                and (connected or executable_ready)
            ),
            detail=(
                "official CZUR app + Curved Books preset + capture-folder observation"
            ),
        )

    def apply_curved_books_preset(self) -> dict[str, object]:
        payload = self._load_config_payload()
        self._require_config_write_ready()
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

    def _atomic_restore_config(self, content: bytes, mode: int) -> None:
        fd, temporary = tempfile.mkstemp(
            prefix=".config.json.digitalisierer.rollback.",
            dir=str(self.config_path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, self.config_path)
            directory_fd = os.open(
                self.config_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _rollback_failed_prelaunch(
        self,
        *,
        output_dir: Path,
        output_existed: bool,
        capture_root_existed: bool,
        backup: Path,
        backup_existed: bool,
        config_before: bytes,
        config_before_mode: int,
        config_after: bytes,
    ) -> list[str]:
        errors: list[str] = []
        config_restored = False
        try:
            current = self.config_path.read_bytes()
            if current != config_after:
                errors.append("CZUR config changed after preset; rollback refused")
            else:
                self._atomic_restore_config(config_before, config_before_mode)
                config_restored = True
        except OSError as exc:
            errors.append(f"CZUR config rollback failed: {exc}")

        if not backup_existed and backup.exists():
            try:
                if config_restored and backup.is_file() and backup.read_bytes() == config_before:
                    backup.unlink()
                else:
                    errors.append("CZUR backup rollback refused")
            except OSError as exc:
                errors.append(f"CZUR backup rollback failed: {exc}")

        for path, existed, label in (
            (output_dir, output_existed, "session output"),
            (self.capture_root, capture_root_existed, "capture root"),
        ):
            if existed:
                continue
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{label} rollback failed: {exc}")
        self._session_output = None
        return errors

    def _visible_windows(self) -> list[str]:
        try:
            completed = subprocess.run(
                [self.xdotool, "search", "--onlyvisible", "--class", "CzurScanner"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            raise CzurAdapterError(
                f"cannot execute xdotool: {self.xdotool}"
            ) from exc
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
        windows = self._start_preflight()

        session_output = output_dir.expanduser()
        output_existed = session_output.exists()
        capture_root_existed = self.capture_root.exists()
        backup = self.config_path.with_name(
            "config.pre-digitalisierer-curved-books.json"
        )
        backup_existed = backup.exists()
        try:
            config_before = self.config_path.read_bytes()
            config_before_mode = self.config_path.stat().st_mode & 0o7777
        except OSError as exc:
            raise CzurAdapterError(
                f"CZUR config cannot be snapshotted before launch: {self.config_path}"
            ) from exc

        self._session_output = session_output
        self._session_output.mkdir(parents=True, exist_ok=True)
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.apply_curved_books_preset()
        try:
            config_after = self.config_path.read_bytes()
        except OSError as exc:
            self._session_output = None
            raise CzurAdapterError(
                f"CZUR config cannot be verified after preset: {self.config_path}"
            ) from exc

        if windows:
            self._focus(windows[-1])
            self._activate_curved_books_mode(windows[-1])
            return

        if not self._launcher_ready():
            raise CzurAdapterError(f"CZUR launcher is not executable: {self.launcher}")
        try:
            subprocess.Popen(
                [str(self.launcher)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            rollback_errors = self._rollback_failed_prelaunch(
                output_dir=session_output,
                output_existed=output_existed,
                capture_root_existed=capture_root_existed,
                backup=backup,
                backup_existed=backup_existed,
                config_before=config_before,
                config_before_mode=config_before_mode,
                config_after=config_after,
            )
            suffix = (
                "; rollback incomplete: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise CzurAdapterError(
                f"cannot execute CZUR launcher: {self.launcher}{suffix}"
            ) from exc
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
        try:
            capture_root = self.capture_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CzurAdapterError(
                f"CZUR capture root is invalid: {self.capture_root}"
            ) from exc
        if not capture_root.is_dir():
            raise CzurAdapterError(
                f"CZUR capture root is missing: {self.capture_root}"
            )

        candidates: list[tuple[int, Path]] = []
        directories = [capture_root]
        for directory in self.capture_root.iterdir():
            if directory.name.startswith("_") or directory.is_symlink():
                continue
            try:
                resolved = directory.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_dir() or resolved.parent != capture_root:
                continue
            directories.append(resolved)

        for directory in directories:
            images = [
                path
                for path in directory.iterdir()
                if not path.is_symlink()
                and path.is_file()
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
        return max(candidates, key=lambda item: item[0])[1]

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