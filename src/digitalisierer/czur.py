from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import stat
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
        try:
            raw_bytes, _ = self._snapshot_regular_file(self.config_path)
        except FileNotFoundError as exc:
            raise CzurAdapterError(
                f"CZUR config is missing: {self.config_path}"
            ) from exc
        except OSError as exc:
            try:
                current = self.config_path.lstat()
            except FileNotFoundError as missing_exc:
                raise CzurAdapterError(
                    f"CZUR config is missing: {self.config_path}"
                ) from missing_exc
            except OSError as inspect_exc:
                raise CzurAdapterError(
                    f"CZUR config cannot be inspected: {self.config_path}"
                ) from inspect_exc
            if not stat.S_ISREG(current.st_mode):
                raise CzurAdapterError(
                    f"CZUR config is not a regular file: {self.config_path}"
                ) from exc
            raise CzurAdapterError(
                f"CZUR config cannot be read safely: {self.config_path}"
            ) from exc
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeError as exc:
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
        backup = self.config_path.with_name(
            "config.pre-digitalisierer-curved-books.json"
        )
        before, _, _, _, _, _ = self._apply_curved_books_preset_transaction(
            backup
        )
        return before

    @staticmethod
    def _config_stat_identity(
        value: os.stat_result,
    ) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            stat.S_IMODE(value.st_mode),
        )

    @staticmethod
    def _same_claimed_preimage(
        expected_content: bytes,
        expected_identity: tuple[int, int, int, int, int, int],
        actual_content: bytes,
        actual_identity: tuple[int, int, int, int, int, int],
    ) -> bool:
        return (
            actual_content == expected_content
            and actual_identity[0] == expected_identity[0]
            and actual_identity[1] == expected_identity[1]
            and actual_identity[2] == expected_identity[2]
            and actual_identity[3] == expected_identity[3]
            and actual_identity[5] == expected_identity[5]
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _snapshot_regular_file(
        self,
        path: Path,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise OSError(f"not a regular file: {path}")
            with os.fdopen(os.dup(fd), "rb") as handle:
                content = handle.read()
            after = os.fstat(fd)
            current = path.lstat()
            before_identity = self._config_stat_identity(before)
            after_identity = self._config_stat_identity(after)
            if (
                before_identity != after_identity
                or after_identity != self._config_stat_identity(current)
                or not stat.S_ISREG(current.st_mode)
            ):
                raise OSError(f"file changed while being snapshotted: {path}")
            return content, after_identity
        finally:
            os.close(fd)

    def _config_snapshot(
        self,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        return self._snapshot_regular_file(self.config_path)

    def _create_backup_if_absent(
        self,
        backup: Path,
        config_before: bytes,
    ) -> tuple[int, int, int, int, int, int] | None:
        if os.path.lexists(backup):
            return None
        descriptor = -1
        try:
            descriptor = os.open(
                backup,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            return None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(config_before)
                handle.flush()
                os.fsync(handle.fileno())
            backup_content, backup_identity = self._snapshot_regular_file(backup)
            if backup_content != config_before:
                raise CzurAdapterError(
                    "CZUR backup changed while Curved Books preset was being prepared"
                )
            self._fsync_directory(backup.parent)
            return backup_identity
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _restore_claimed_path(self, claimed: Path, destination: Path) -> bool:
        try:
            claimed_before = claimed.lstat()
            os.link(
                claimed,
                destination,
                follow_symlinks=False,
            )
            claimed_after = claimed.lstat()
            current = destination.lstat()
            if (
                claimed_after.st_dev != current.st_dev
                or claimed_after.st_ino != current.st_ino
                or stat.S_IFMT(claimed_after.st_mode) != stat.S_IFMT(current.st_mode)
                or claimed_before.st_dev != claimed_after.st_dev
                or claimed_before.st_ino != claimed_after.st_ino
            ):
                return False
            claimed.unlink()
            self._fsync_directory(destination.parent)
            return True
        except (FileExistsError, FileNotFoundError, OSError):
            return False

    def _remove_owned_backup(
        self,
        backup: Path,
        config_before: bytes,
        backup_identity: tuple[int, int, int, int, int, int],
    ) -> bool:
        cleanup_dir = Path(
            tempfile.mkdtemp(
                prefix=".config.json.digitalisierer.backup-claim.",
                dir=str(backup.parent),
            )
        )
        claimed_backup = cleanup_dir / "backup"
        try:
            try:
                os.rename(backup, claimed_backup)
            except (FileNotFoundError, OSError):
                return False
            try:
                backup_content, current_identity = self._snapshot_regular_file(
                    claimed_backup
                )
            except OSError:
                self._restore_claimed_path(claimed_backup, backup)
                return False
            if not self._same_claimed_preimage(
                config_before,
                backup_identity,
                backup_content,
                current_identity,
            ):
                self._restore_claimed_path(claimed_backup, backup)
                return False
            claimed_backup.unlink()
            self._fsync_directory(backup.parent)
            return True
        finally:
            try:
                cleanup_dir.rmdir()
            except OSError:
                pass

    def _restore_claimed_config(self, claimed: Path) -> bool:
        return self._restore_claimed_path(claimed, self.config_path)

    def _rollback_published_claim(
        self,
        *,
        claimed_preimage: Path,
        preset_content: bytes,
        published_identity: tuple[int, int, int, int, int, int],
    ) -> bool:
        rollback_dir = Path(
            tempfile.mkdtemp(
                prefix=".config.json.digitalisierer.rollback-claim.",
                dir=str(self.config_path.parent),
            )
        )
        published_claim = rollback_dir / "published"
        try:
            try:
                os.rename(self.config_path, published_claim)
            except FileNotFoundError:
                return False
            try:
                current_content, current_identity = self._snapshot_regular_file(
                    published_claim
                )
            except OSError:
                self._restore_claimed_config(published_claim)
                return False
            if not self._same_claimed_preimage(
                preset_content,
                published_identity,
                current_content,
                current_identity,
            ):
                self._restore_claimed_config(published_claim)
                return False
            if not self._restore_claimed_config(claimed_preimage):
                return False
            try:
                published_after, published_after_identity = (
                    self._snapshot_regular_file(published_claim)
                )
                if self._same_claimed_preimage(
                    preset_content,
                    published_identity,
                    published_after,
                    published_after_identity,
                ):
                    published_claim.unlink()
                    self._fsync_directory(self.config_path.parent)
            except OSError:
                pass
            return True
        finally:
            try:
                rollback_dir.rmdir()
            except OSError:
                pass

    def _apply_curved_books_preset_transaction(
        self,
        backup: Path,
    ) -> tuple[
        dict[str, object],
        bytes,
        int,
        bytes,
        tuple[int, int, int, int, int, int],
        tuple[int, int, int, int, int, int] | None,
    ]:
        self._require_config_write_ready()
        try:
            config_before, config_before_identity = self._config_snapshot()
            raw = config_before.decode("utf-8")
        except UnicodeError as exc:
            raise CzurAdapterError(
                f"CZUR config cannot be read as UTF-8: {self.config_path}"
            ) from exc
        except OSError as exc:
            raise CzurAdapterError(
                f"CZUR config cannot be snapshotted before preset: {self.config_path}"
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CzurAdapterError("CZUR config is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CzurAdapterError("CZUR config root must be an object")
        setting = payload.setdefault("setting", {})
        if not isinstance(setting, dict):
            raise CzurAdapterError("CZUR config setting must be an object")

        before: dict[str, object] = {}
        for key, value in CURVED_BOOKS_SETTINGS.items():
            before[key] = setting.get(key)
            setting[key] = value
        preset_content = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

        current, current_identity = self._config_snapshot()
        if current != config_before or current_identity != config_before_identity:
            raise CzurAdapterError(
                "CZUR config changed before Curved Books preset could be applied"
            )

        fd, temporary_name = tempfile.mkstemp(
            prefix=".config.json.digitalisierer.",
            dir=str(self.config_path.parent),
        )
        temporary = Path(temporary_name)
        guard_dir = Path(
            tempfile.mkdtemp(
                prefix=".config.json.digitalisierer.claim.",
                dir=str(self.config_path.parent),
            )
        )
        claimed_preimage = guard_dir / "preimage"
        claimed = False
        published = False
        published_identity: tuple[int, int, int, int, int, int] | None = None
        backup_identity: tuple[int, int, int, int, int, int] | None = None
        rollback_complete = False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(preset_content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)

            current, current_identity = self._config_snapshot()
            if (
                current != config_before
                or current_identity != config_before_identity
            ):
                raise CzurAdapterError(
                    "CZUR config changed before Curved Books preset could be applied"
                )

            os.rename(self.config_path, claimed_preimage)
            claimed = True
            claimed_content, claimed_identity = self._snapshot_regular_file(
                claimed_preimage
            )
            if not self._same_claimed_preimage(
                config_before,
                config_before_identity,
                claimed_content,
                claimed_identity,
            ):
                raise CzurAdapterError(
                    "CZUR config changed while Curved Books preset was being claimed"
                )

            backup_identity = self._create_backup_if_absent(
                backup,
                config_before,
            )

            try:
                os.link(
                    temporary,
                    self.config_path,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise CzurAdapterError(
                    "CZUR config changed immediately before Curved Books preset publication"
                ) from exc
            published = True
            published_identity = self._config_stat_identity(
                os.stat(temporary, follow_symlinks=False)
            )
            self._fsync_directory(self.config_path.parent)

            config_after, config_after_identity = self._config_snapshot()
            if (
                config_after != preset_content
                or config_after_identity != published_identity
            ):
                raise CzurAdapterError(
                    "CZUR config changed while Curved Books preset was being applied"
                )

            temporary.unlink()
            self._fsync_directory(self.config_path.parent)
            config_after, config_after_identity = self._config_snapshot()
            if not self._same_claimed_preimage(
                preset_content,
                published_identity,
                config_after,
                config_after_identity,
            ):
                raise CzurAdapterError(
                    "CZUR config changed while Curved Books preset was being finalized"
                )

            claimed_after, claimed_after_identity = self._snapshot_regular_file(
                claimed_preimage
            )
            if (
                claimed_after != claimed_content
                or claimed_after_identity != claimed_identity
            ):
                raise CzurAdapterError(
                    "CZUR config preimage changed while Curved Books preset was being applied"
                )

            claimed_preimage.unlink()
            claimed = False
            try:
                guard_dir.rmdir()
            except OSError:
                pass
            self._fsync_directory(self.config_path.parent)
            return (
                before,
                config_before,
                config_before_identity[-1],
                config_after,
                config_after_identity,
                backup_identity,
            )
        except Exception as exc:
            if published and published_identity is not None:
                rollback_complete = self._rollback_published_claim(
                    claimed_preimage=claimed_preimage,
                    preset_content=preset_content,
                    published_identity=published_identity,
                )
                if rollback_complete:
                    claimed = False
            elif claimed:
                rollback_complete = self._restore_claimed_config(claimed_preimage)
                if rollback_complete:
                    claimed = False

            backup_cleanup_failed = False
            if rollback_complete and backup_identity is not None:
                backup_cleanup_failed = not self._remove_owned_backup(
                    backup,
                    config_before,
                    backup_identity,
                )
            if backup_cleanup_failed:
                raise CzurAdapterError(
                    f"{exc}; CZUR backup rollback refused"
                ) from exc
            raise
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            if not claimed:
                try:
                    guard_dir.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _rollback_created_directories(
        output_dir: Path,
        output_existed: bool,
        capture_root: Path,
        capture_root_existed: bool,
    ) -> list[str]:
        errors: list[str] = []
        for path, existed, label in (
            (output_dir, output_existed, "session output"),
            (capture_root, capture_root_existed, "capture root"),
        ):
            if existed:
                continue
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{label} rollback failed: {exc}")
        return errors

    def _restore_config_snapshot(
        self,
        *,
        expected_content: bytes,
        expected_identity: tuple[int, int, int, int, int, int],
        restore_content: bytes,
        restore_mode: int,
    ) -> tuple[bool, Path | None]:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".config.json.digitalisierer.rollback-preimage.",
            dir=str(self.config_path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(restore_content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, restore_mode)
            self._fsync_directory(self.config_path.parent)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

        restored = self._rollback_published_claim(
            claimed_preimage=temporary,
            preset_content=expected_content,
            published_identity=expected_identity,
        )
        if restored:
            return True, None
        return False, temporary

    def _rollback_failed_prelaunch(
        self,
        *,
        output_dir: Path,
        output_existed: bool,
        capture_root_existed: bool,
        backup: Path,
        backup_existed: bool,
        backup_identity: tuple[int, int, int, int, int, int] | None,
        config_before: bytes,
        config_before_mode: int,
        config_after: bytes,
        config_after_identity: tuple[int, int, int, int, int, int],
    ) -> list[str]:
        errors: list[str] = []
        config_restored = False
        try:
            config_restored, preserved_preimage = self._restore_config_snapshot(
                expected_content=config_after,
                expected_identity=config_after_identity,
                restore_content=config_before,
                restore_mode=config_before_mode,
            )
            if not config_restored:
                detail = "CZUR config changed after preset; rollback refused"
                if preserved_preimage is not None:
                    detail += f"; original preimage preserved at {preserved_preimage}"
                errors.append(detail)
        except OSError as exc:
            errors.append(f"CZUR config rollback failed: {exc}")

        if not backup_existed and backup_identity is not None:
            if config_restored:
                if not self._remove_owned_backup(
                    backup,
                    config_before,
                    backup_identity,
                ):
                    errors.append("CZUR backup rollback refused")
            elif os.path.lexists(backup):
                errors.append("CZUR backup rollback refused")

        errors.extend(
            self._rollback_created_directories(
                output_dir,
                output_existed,
                self.capture_root,
                capture_root_existed,
            )
        )
        self._session_output = None
        return errors

    @staticmethod
    def _process_group_alive(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _wait_for_process_group_exit(
        cls,
        process: subprocess.Popen[bytes],
        process_group_id: int,
        *,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            process.poll()
            if not cls._process_group_alive(process_group_id):
                return True
            time.sleep(0.05)
        process.poll()
        return not cls._process_group_alive(process_group_id)

    @classmethod
    def _stop_launched_process(cls, process: subprocess.Popen[bytes]) -> list[str]:
        process_group_id = process.pid
        if not cls._process_group_alive(process_group_id):
            process.poll()
            return []
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            process.poll()
            return []
        except OSError as exc:
            return [f"launched CZUR process-group termination failed: {exc}"]
        if cls._wait_for_process_group_exit(process, process_group_id, timeout=2.0):
            process.poll()
            return []
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            process.poll()
            return []
        except OSError as exc:
            return [f"launched CZUR process-group kill failed: {exc}"]
        if not cls._wait_for_process_group_exit(process, process_group_id, timeout=2.0):
            return ["launched CZUR process group did not exit after kill"]
        process.poll()
        return []

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
        backup_existed = os.path.lexists(backup)

        self._session_output = session_output
        self._session_output.mkdir(parents=True, exist_ok=True)
        self.capture_root.mkdir(parents=True, exist_ok=True)
        try:
            (
                _,
                config_before,
                config_before_mode,
                config_after,
                config_after_identity,
                backup_identity,
            ) = self._apply_curved_books_preset_transaction(backup)
        except Exception as exc:
            rollback_errors = self._rollback_created_directories(
                session_output,
                output_existed,
                self.capture_root,
                capture_root_existed,
            )
            self._session_output = None
            if rollback_errors:
                raise CzurAdapterError(
                    f"{exc}; rollback incomplete: " + "; ".join(rollback_errors)
                ) from exc
            raise

        if windows:
            try:
                self._focus(windows[-1])
                self._activate_curved_books_mode(windows[-1])
            except Exception as exc:
                rollback_errors = self._rollback_failed_prelaunch(
                    output_dir=session_output,
                    output_existed=output_existed,
                    capture_root_existed=capture_root_existed,
                    backup=backup,
                    backup_existed=backup_existed,
                    backup_identity=backup_identity,
                    config_before=config_before,
                    config_before_mode=config_before_mode,
                    config_after=config_after,
                    config_after_identity=config_after_identity,
                )
                suffix = (
                    "; rollback incomplete: " + "; ".join(rollback_errors)
                    if rollback_errors
                    else ""
                )
                raise CzurAdapterError(f"{exc}{suffix}") from exc
            return

        if not self._launcher_ready():
            rollback_errors = self._rollback_failed_prelaunch(
                output_dir=session_output,
                output_existed=output_existed,
                capture_root_existed=capture_root_existed,
                backup=backup,
                backup_existed=backup_existed,
                backup_identity=backup_identity,
                config_before=config_before,
                config_before_mode=config_before_mode,
                config_after=config_after,
                config_after_identity=config_after_identity,
            )
            suffix = (
                "; rollback incomplete: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise CzurAdapterError(
                f"CZUR launcher is not executable: {self.launcher}{suffix}"
            )
        try:
            launched_process = subprocess.Popen(
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
                backup_identity=backup_identity,
                config_before=config_before,
                config_before_mode=config_before_mode,
                config_after=config_after,
                config_after_identity=config_after_identity,
            )
            suffix = (
                "; rollback incomplete: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise CzurAdapterError(
                f"cannot execute CZUR launcher: {self.launcher}{suffix}"
            ) from exc

        try:
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                windows = self._visible_windows()
                if windows:
                    self._focus(windows[-1])
                    self._activate_curved_books_mode(windows[-1])
                    return
                if launched_process.poll() is not None:
                    raise CzurAdapterError(
                        "CZUR application exited before exposing a visible window"
                    )
                time.sleep(0.25)
            raise CzurAdapterError(
                "CZUR application did not expose a visible window"
            )
        except Exception as exc:
            process_errors = self._stop_launched_process(launched_process)
            if process_errors:
                raise CzurAdapterError(
                    f"{exc}; rollback incomplete: " + "; ".join(process_errors)
                ) from exc
            rollback_errors = self._rollback_failed_prelaunch(
                output_dir=session_output,
                output_existed=output_existed,
                capture_root_existed=capture_root_existed,
                backup=backup,
                backup_existed=backup_existed,
                backup_identity=backup_identity,
                config_before=config_before,
                config_before_mode=config_before_mode,
                config_after=config_after,
                config_after_identity=config_after_identity,
            )
            suffix = (
                "; rollback incomplete: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise CzurAdapterError(f"{exc}{suffix}") from exc


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