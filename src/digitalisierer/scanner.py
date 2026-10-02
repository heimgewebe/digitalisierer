from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import statistics
import tempfile
from typing import Any, Iterator, Protocol

from .domain import MediaAsset, MediaKind, ProcessingSession, QualityFinding, SessionAsset
from .library import session_root
from .ports import OCRBackend


SCAN_SESSION_LAYOUT = "digitalisierer.scan-session.v1"
SCAN_EXPORT_LAYOUT = "digitalisierer.scan-export.v1"
SESSION_FILE = "session.json"
REVIEW_FILE = "review.json"
REVIEW_LOCK_FILE = ".review.lock"
FINDINGS_FILE = "findings.json"
OBSERVATION_COMMIT_FILE = ".observation-metadata-commit.json"
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
MAX_SOURCE_IMAGE_BYTES = 512 * 1024 * 1024


class ScannerWorkflowError(RuntimeError):
    """Raised when a scanner session cannot be ingested or finalized safely."""


class ScannerReviewConflict(ScannerWorkflowError):
    """Raised when a review form no longer describes the current decision."""


class ImageDependencyError(ScannerWorkflowError):
    """Raised when Pillow/img2pdf are unavailable for scanner work."""


class ImageToPdf(Protocol):
    name: str

    def version(self) -> str:
        ...

    def __call__(self, images: list[Path], output: Path) -> None:
        ...


@dataclass(frozen=True, slots=True)
class ScanSessionPaths:
    root: Path
    sources: Path
    thumbnails: Path
    exports: Path
    session_file: Path
    review_file: Path
    findings_file: Path


@dataclass(frozen=True, slots=True)
class ScanObservation:
    session_root: Path
    imported_asset_ids: tuple[str, ...]
    skipped_asset_ids: tuple[str, ...]
    findings: tuple[QualityFinding, ...]


@dataclass(frozen=True, slots=True)
class ScanExport:
    session_root: Path
    export_dir: Path
    output_hashes: dict[str, str]


def scan_session_paths(
    project_id: str,
    session_id: str,
    library_root: Path | None = None,
) -> ScanSessionPaths:
    root = session_root(project_id, session_id, library_root)
    return ScanSessionPaths(
        root=root,
        sources=root / "sources",
        thumbnails=root / "thumbnails",
        exports=root / "exports",
        session_file=root / SESSION_FILE,
        review_file=root / REVIEW_FILE,
        findings_file=root / FINDINGS_FILE,
    )


def _validate_session_identity(paths: ScanSessionPaths, session: dict[str, Any]) -> None:
    sessions_dir = paths.root.parent
    project_dir = sessions_dir.parent
    projects_dir = project_dir.parent
    if sessions_dir.name != "sessions" or projects_dir.name != "projects":
        raise ScannerWorkflowError("scan session path does not use the canonical layout")
    if (
        session.get("schema_version") != 1
        or session.get("kind") != "digitalisierer.scan-session"
        or session.get("layout") != SCAN_SESSION_LAYOUT
        or session.get("project_id") != project_dir.name
        or session.get("session_id") != paths.root.name
    ):
        raise ScannerWorkflowError(
            "scan session identity/layout does not match the session path"
        )


def _validate_review_payload(review: dict[str, Any]) -> None:
    if (
        review.get("schema_version") != 1
        or review.get("kind") != "digitalisierer.scan-review"
    ):
        raise ScannerWorkflowError("scan review metadata contract is incompatible")
    if not isinstance(review.get("items"), dict):
        raise ScannerWorkflowError("scan review items must be an object")


def _validate_findings_payload(findings: dict[str, Any]) -> None:
    if (
        findings.get("schema_version") != 1
        or findings.get("kind") != "digitalisierer.scan-findings"
    ):
        raise ScannerWorkflowError("scan findings metadata contract is incompatible")
    raw_findings = findings.get("findings")
    if not isinstance(raw_findings, list):
        raise ScannerWorkflowError("scan findings must be a list")
    for finding in raw_findings:
        if not isinstance(finding, dict):
            raise ScannerWorkflowError("scan finding entry must be an object")
        kind = finding.get("kind")
        message = finding.get("message")
        asset_ids = finding.get("asset_ids")
        evidence = finding.get("evidence")
        confidence = finding.get("confidence")
        if (
            not isinstance(kind, str)
            or not kind.strip()
            or not isinstance(message, str)
            or not message.strip()
            or not isinstance(asset_ids, list)
            or any(
                not isinstance(asset_id, str) or not asset_id.strip()
                for asset_id in asset_ids
            )
            or len(set(asset_ids)) != len(asset_ids)
            or not isinstance(evidence, list)
            or any(not isinstance(item, str) for item in evidence)
            or (
                confidence is not None
                and (
                    isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not 0.0 <= confidence <= 1.0
                    or not math.isfinite(confidence)
                )
            )
        ):
            raise ScannerWorkflowError("scan finding entry has invalid shape")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _validate_scan_asset_record(
    record: dict[str, Any],
) -> tuple[str, str, str]:
    asset_id = record.get("asset_id")
    source_name = record.get("source_name")
    capture_source = record.get("capture_source")
    sha256 = record.get("sha256")
    byte_count = record.get("bytes")
    preserved = record.get("preserved_path")
    thumbnail = record.get("thumbnail_path")
    thumbnail_sha256 = record.get("thumbnail_sha256")
    image = record.get("image")

    if not isinstance(asset_id, str) or not asset_id:
        raise ScannerWorkflowError("scan asset record has an invalid asset_id")
    if (
        not isinstance(source_name, str)
        or not source_name
        or Path(source_name).name != source_name
        or Path(source_name).suffix.lower() not in IMAGE_SUFFIXES
    ):
        raise ScannerWorkflowError(
            f"scan asset source_name is invalid for {asset_id}"
        )
    if not isinstance(capture_source, str) or not capture_source:
        raise ScannerWorkflowError(
            f"scan asset capture_source is invalid for {asset_id}"
        )
    if not _is_sha256(sha256):
        raise ScannerWorkflowError(
            f"scan asset source digest is invalid for {asset_id}"
        )
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count <= 0
    ):
        raise ScannerWorkflowError(
            f"scan asset byte count is invalid for {asset_id}"
        )
    if not isinstance(preserved, str) or not preserved:
        raise ScannerWorkflowError(
            f"scan asset preserved path is invalid for {asset_id}"
        )
    if thumbnail != f"thumbnails/{asset_id}.jpg":
        raise ScannerWorkflowError(
            f"scan thumbnail path is not canonical for {asset_id}"
        )
    if not _is_sha256(thumbnail_sha256):
        raise ScannerWorkflowError(
            f"scan thumbnail digest is invalid for {asset_id}"
        )
    if not isinstance(image, dict):
        raise ScannerWorkflowError(
            f"scan asset image metadata is invalid for {asset_id}"
        )

    width = image.get("width")
    height = image.get("height")
    dpi = image.get("dpi")
    mean = image.get("mean")
    stddev = image.get("stddev")
    dark_ratio = image.get("dark_ratio")
    average_hash = image.get("average_hash")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or not isinstance(dpi, list)
        or len(dpi) != 2
        or any(not _is_finite_number(value) or float(value) < 0.0 for value in dpi)
        or not _is_finite_number(mean)
        or not _is_finite_number(stddev)
        or not _is_finite_number(dark_ratio)
        or not isinstance(average_hash, str)
        or len(average_hash) != 256
        or any(character not in "0123456789abcdef" for character in average_hash)
    ):
        raise ScannerWorkflowError(
            f"scan asset image metadata is invalid for {asset_id}"
        )
    assert isinstance(mean, (int, float)) and not isinstance(mean, bool)
    assert isinstance(stddev, (int, float)) and not isinstance(stddev, bool)
    assert isinstance(dark_ratio, (int, float)) and not isinstance(dark_ratio, bool)
    if (
        not 0.0 <= float(mean) <= 255.0
        or float(stddev) < 0.0
        or not 0.0 <= float(dark_ratio) <= 1.0
    ):
        raise ScannerWorkflowError(
            f"scan asset image metadata is invalid for {asset_id}"
        )

    assert isinstance(sha256, str)
    assert isinstance(preserved, str)
    return asset_id, sha256, preserved


def _json_text(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _review_update_lock(paths: ScanSessionPaths) -> Iterator[None]:
    if not paths.root.is_dir() or not paths.session_file.is_file():
        raise ScannerWorkflowError(
            f"scanner session is not initialized: {paths.root}"
        )
    root = _validate_session_root(paths)
    try:
        root_descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise ScannerWorkflowError("scan session root is invalid") from exc
    try:
        descriptor = os.open(
            REVIEW_LOCK_FILE,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if os.path.lexists(root / OBSERVATION_COMMIT_FILE):
                _recover_observation_metadata_commit(paths)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    finally:
        os.close(root_descriptor)


def _stable_file_bytes(path: Path) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except FileNotFoundError as exc:
        raise ScannerWorkflowError(f"scanner session file is missing: {path}") from exc
    except OSError as exc:
        raise ScannerWorkflowError(
            f"scanner session file must be a non-symlink regular file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ScannerWorkflowError(
                f"scanner session file must be a non-symlink regular file: {path}"
            )
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            raw = handle.read()
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise ScannerWorkflowError(
                f"scanner session file changed while reading: {path}"
            )
        try:
            current = path.lstat()
        except OSError as exc:
            raise ScannerWorkflowError(
                f"scanner session file changed while reading: {path}"
            ) from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or _stat_identity(current) != _stat_identity(after)
        ):
            raise ScannerWorkflowError(
                f"scanner session file changed while reading: {path}"
            )
        return raw
    finally:
        os.close(descriptor)


def _load_json(path: Path) -> dict[str, Any]:
    raw = _stable_file_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScannerWorkflowError(f"scanner session JSON is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ScannerWorkflowError(f"scanner session JSON must be an object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


@contextmanager
def _stable_source_descriptor(
    path: Path,
    *,
    expected_stat: os.stat_result | None = None,
) -> Iterator[tuple[int, os.stat_result]]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as exc:
        raise ScannerWorkflowError(
            f"scan source must be a non-symlink regular file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ScannerWorkflowError(
                f"scan source must be a non-symlink regular file: {path}"
            )
        if before.st_size <= 0 or before.st_size > MAX_SOURCE_IMAGE_BYTES:
            raise ScannerWorkflowError(
                f"scan source size is outside the supported boundary: {path}"
            )
        if (
            expected_stat is not None
            and _stat_identity(before) != _stat_identity(expected_stat)
        ):
            raise ScannerWorkflowError(f"scan source changed while reading: {path}")
        try:
            current = path.lstat()
        except OSError as exc:
            raise ScannerWorkflowError(
                f"scan source changed while reading: {path}"
            ) from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
            or _stat_identity(current) != _stat_identity(before)
        ):
            raise ScannerWorkflowError(f"scan source changed while reading: {path}")

        yield descriptor, before

        after = os.fstat(descriptor)
        try:
            current = path.lstat()
        except OSError as exc:
            raise ScannerWorkflowError(
                f"scan source changed while reading: {path}"
            ) from exc
        if (
            _stat_identity(after) != _stat_identity(before)
            or not stat.S_ISREG(current.st_mode)
            or current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or _stat_identity(current) != _stat_identity(after)
        ):
            raise ScannerWorkflowError(f"scan source changed while reading: {path}")
    finally:
        os.close(descriptor)


def _stable_hash(path: Path) -> tuple[str, os.stat_result]:
    with _stable_source_descriptor(path) as (descriptor, source_stat):
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), source_stat


def _natural_key(path: Path) -> tuple[list[int | str], str]:
    normalized = [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]
    return normalized, path.name

def image_files(folder: Path) -> list[Path]:
    root = folder.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ScannerWorkflowError(f"scan source folder is not a directory: {root}")
    return sorted(
        [
            path
            for path in root.iterdir()
            if not path.is_symlink()
            and path.is_file()
            and path.suffix.lower() in IMAGE_SUFFIXES
        ],
        key=_natural_key,
    )


def _validate_session_root(paths: ScanSessionPaths) -> Path:
    if paths.root.is_symlink():
        raise ScannerWorkflowError("scan session root must not be a symlink")
    try:
        root = paths.root.resolve(strict=True)
        expected_root = paths.root.parent.resolve(strict=True) / paths.root.name
    except (OSError, RuntimeError) as exc:
        raise ScannerWorkflowError("scan session root is invalid") from exc
    if root != expected_root or not root.is_dir():
        raise ScannerWorkflowError("scan session root is not canonical")
    return root


def _validate_session_storage_directory(
    paths: ScanSessionPaths,
    directory: Path,
    expected_name: str,
) -> None:
    root = _validate_session_root(paths)
    if directory.name != expected_name or directory.is_symlink():
        raise ScannerWorkflowError(
            f"scanner {expected_name} directory must be a canonical direct child"
        )
    try:
        resolved = directory.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScannerWorkflowError(
            f"scanner {expected_name} directory is invalid"
        ) from exc
    if (
        not resolved.is_dir()
        or resolved.parent != root
        or resolved.name != expected_name
    ):
        raise ScannerWorkflowError(
            f"scanner {expected_name} directory must be a canonical direct child"
        )


def create_or_resume_scan_session(
    project_id: str,
    session_id: str,
    library_root: Path | None = None,
    *,
    repairable_capture_sources: frozenset[str] = frozenset(),
) -> ScanSessionPaths:
    paths = scan_session_paths(project_id, session_id, library_root)
    paths.root.parent.mkdir(parents=True, exist_ok=True)
    if paths.root.is_symlink():
        raise ScannerWorkflowError("scan session root must not be a symlink")
    try:
        paths.root.mkdir(mode=0o700)
    except FileExistsError:
        if not paths.root.is_dir():
            raise ScannerWorkflowError("scan session root is not a directory")
        if not paths.session_file.exists() and any(paths.root.iterdir()):
            raise ScannerWorkflowError(
                "scan session root is already occupied by non-scanner content"
            )
    _validate_session_root(paths)

    if paths.session_file.exists():
        session = _load_json(paths.session_file)
        _validate_session_identity(paths, session)
    else:
        session = {
            "schema_version": 1,
            "kind": "digitalisierer.scan-session",
            "layout": SCAN_SESSION_LAYOUT,
            "project_id": project_id,
            "session_id": session_id,
            "assets": [],
            "capture_observations": [],
        }
        _atomic_write_text(paths.session_file, _json_text(session))

    raw_assets = session.get("assets")
    if not isinstance(raw_assets, list):
        raise ScannerWorkflowError("scan session assets must be a list")
    if raw_assets:
        missing_metadata = [
            path.name
            for path in (paths.review_file, paths.findings_file)
            if not path.exists()
        ]
        if missing_metadata:
            raise ScannerWorkflowError(
                "scan metadata is missing for populated session: "
                + ", ".join(missing_metadata)
            )

    for metadata_path, validator in (
        (paths.review_file, _validate_review_payload),
        (paths.findings_file, _validate_findings_payload),
    ):
        if metadata_path.exists():
            validator(_load_json(metadata_path))

    for directory, expected_name in (
        (paths.sources, "sources"),
        (paths.thumbnails, "thumbnails"),
        (paths.exports, "exports"),
    ):
        if directory.is_symlink():
            raise ScannerWorkflowError(
                f"scanner {expected_name} directory must be a canonical direct child"
            )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _validate_session_storage_directory(paths, directory, expected_name)

    if not paths.review_file.exists():
        _atomic_write_text(
            paths.review_file,
            _json_text(
                {
                    "schema_version": 1,
                    "kind": "digitalisierer.scan-review",
                    "items": {},
                }
            ),
        )
    if not paths.findings_file.exists():
        _atomic_write_text(
            paths.findings_file,
            _json_text(
                {
                    "schema_version": 1,
                    "kind": "digitalisierer.scan-findings",
                    "findings": [],
                }
            ),
        )

    # A resumed session is ready only when its individually valid metadata also
    # forms one coherent scanner state. Only an actual crash marker needs the
    # session lock; ordinary resume remains side-effect free here.
    if os.path.lexists(_observation_commit_path(paths)):
        with _review_update_lock(paths):
            pass

    session = _load_json(paths.session_file)
    _validate_session_identity(paths, session)
    review = _load_json(paths.review_file)
    _validate_review_payload(review)
    findings = _load_json(paths.findings_file)
    _validate_findings_payload(findings)
    processing = _processing_session_from_payload(
        paths,
        session,
        review,
        repairable_capture_sources=repairable_capture_sources,
    )
    _validate_findings_asset_ids(processing, findings)
    return paths


def _pillow_modules() -> tuple[Any, Any]:
    try:
        from PIL import Image, ImageStat
    except ImportError as exc:
        raise ImageDependencyError(
            "scanner image support requires Pillow; install Digitalisierer scanner dependencies"
        ) from exc
    return Image, ImageStat


def _pixel_values(image: Any) -> list[int]:
    flattened = getattr(image, "get_flattened_data", None)
    if callable(flattened):
        return [int(value) for value in flattened()]
    return [int(value) for value in image.getdata()]


def _average_hash(image: Any) -> str:
    small = image.convert("L").resize((32, 32))
    values = _pixel_values(small)
    mean = sum(values) / max(1, len(values))
    bits = "".join("1" if value > mean else "0" for value in values)
    return f"{int(bits, 2):0256x}"


@contextmanager
def _pillow_image_source(source: Path | int) -> Iterator[Any]:
    if isinstance(source, int):
        os.lseek(source, 0, os.SEEK_SET)
        with os.fdopen(os.dup(source), "rb") as handle:
            yield handle
        return
    yield source


def _inspect_image(path: Path | int) -> dict[str, object]:
    Image, ImageStat = _pillow_modules()
    with _pillow_image_source(path) as image_source:
        with Image.open(image_source) as image:
            image.load()
            width, height = image.size
            dpi_raw = image.info.get("dpi") or (0.0, 0.0)
            work = image.convert("L")
            work.thumbnail((256, 256))
            stat = ImageStat.Stat(work)
            values = _pixel_values(work)
            dark_ratio = sum(value < 210 for value in values) / max(1, len(values))
            average_hash = _average_hash(image)
    dpi_x = float(dpi_raw[0]) if len(dpi_raw) >= 1 else 0.0
    dpi_y = float(dpi_raw[1]) if len(dpi_raw) >= 2 else 0.0
    return {
        "width": int(width),
        "height": int(height),
        "dpi": [round(dpi_x, 3), round(dpi_y, 3)],
        "mean": round(float(stat.mean[0]), 3),
        "stddev": round(float(stat.stddev[0]), 3),
        "dark_ratio": round(float(dark_ratio), 6),
        "average_hash": average_hash,
    }


def _write_thumbnail(
    source: Path | int,
    target: Path,
) -> _CreatedArtifactState | None:
    Image, _ = _pillow_modules()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with _pillow_image_source(source) as image_source:
            with Image.open(image_source) as image:
                image.load()
                thumbnail = image.convert("RGB")
                thumbnail.thumbnail((720, 960))
                with temporary.open("xb") as handle:
                    thumbnail.save(handle, format="JPEG", quality=82, optimize=True)
                    handle.flush()
                    os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        expected_sha256 = _sha256_file(temporary)
        expected_identity = _stat_identity(
            os.stat(temporary, follow_symlinks=False)
        )
        try:
            _rename_noreplace(temporary, target)
        except FileExistsError:
            existing_sha256, _ = _regular_file_snapshot(
                target,
                purpose="scan thumbnail",
            )
            if not secrets.compare_digest(existing_sha256, expected_sha256):
                raise ScannerWorkflowError(
                    f"scan thumbnail collision for {target.name}"
                )
            temporary.unlink()
            return None
        expected_state = _CreatedArtifactState(
            path=target,
            sha256=expected_sha256,
            identity=expected_identity,
        )
        try:
            published_sha256, published_identity = _regular_file_snapshot(
                target,
                purpose="published scan thumbnail",
            )
            if (
                published_identity != expected_identity
                or not secrets.compare_digest(published_sha256, expected_sha256)
            ):
                raise ScannerWorkflowError(
                    f"scan thumbnail changed during publication: {target.name}"
                )
        except Exception as exc:
            if not _remove_created_artifact(expected_state):
                raise ScannerWorkflowError(
                    f"failed to roll back published scan thumbnail: {target.name}"
                ) from exc
            raise
        return _CreatedArtifactState(
            path=target,
            sha256=published_sha256,
            identity=published_identity,
        )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _existing_regular_file_hash(path: Path) -> str | None:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ScannerWorkflowError(
            f"preserved source target must be a non-symlink regular file: {path.name}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ScannerWorkflowError(
                f"preserved source target must be a non-symlink regular file: {path.name}"
            )
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(fd)
        if _stat_identity(before) != _stat_identity(after):
            raise ScannerWorkflowError(
                f"preserved source target changed while hashing: {path.name}"
            )
        current = path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or _stat_identity(current) != _stat_identity(after)
        ):
            raise ScannerWorkflowError(
                f"preserved source target changed while hashing: {path.name}"
            )
        return digest.hexdigest()
    except OSError as exc:
        raise ScannerWorkflowError(
            f"preserved source target changed while hashing: {path.name}"
        ) from exc
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class _CreatedArtifactState:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ThumbnailRepairState:
    thumbnail: Path
    preimage_claim: Path | None
    preimage_sha256: str | None
    preimage_identity: tuple[int, int, int, int] | None
    published_sha256: str
    published_identity: tuple[int, int, int, int]


def _regular_file_snapshot(
    path: Path,
    *,
    purpose: str,
) -> tuple[str, tuple[int, int, int, int]]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as exc:
        raise ScannerWorkflowError(
            f"{purpose} must be a non-symlink regular file: {path.name}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ScannerWorkflowError(
                f"{purpose} must be a non-symlink regular file: {path.name}"
            )
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(descriptor)
        try:
            current = path.lstat()
        except OSError as exc:
            raise ScannerWorkflowError(
                f"{purpose} changed while reading: {path.name}"
            ) from exc
        if (
            _stat_identity(before) != _stat_identity(after)
            or not stat.S_ISREG(current.st_mode)
            or current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or _stat_identity(current) != _stat_identity(after)
        ):
            raise ScannerWorkflowError(
                f"{purpose} changed while reading: {path.name}"
            )
        return digest.hexdigest(), _stat_identity(after)
    finally:
        os.close(descriptor)


@contextmanager
def _verified_preview_source_snapshot(
    source: Path,
    *,
    expected_sha256: str,
) -> Iterator[int]:
    with tempfile.TemporaryDirectory(
        prefix=".digitalisierer-preview-source.",
    ) as directory_name:
        snapshot = Path(directory_name) / f"source{source.suffix or '.bin'}"
        try:
            source_fd = os.open(
                source,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except OSError as exc:
            raise ScannerWorkflowError(
                f"preserved scanner source cannot be snapshotted: {source.name}"
            ) from exc
        snapshot_fd = -1
        snapshot_read_fd = -1
        try:
            try:
                snapshot_fd = os.open(
                    snapshot,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    0o400,
                )
                os.fchmod(snapshot_fd, 0o400)
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ScannerWorkflowError(
                        f"preserved scanner source is not regular: {source.name}"
                    )

                digest = hashlib.sha256()
                with (
                    os.fdopen(os.dup(source_fd), "rb") as source_handle,
                    os.fdopen(os.dup(snapshot_fd), "wb") as snapshot_handle,
                ):
                    for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                        snapshot_handle.write(chunk)
                    snapshot_handle.flush()
                    os.fsync(snapshot_handle.fileno())

                after = os.fstat(source_fd)
                try:
                    current = source.lstat()
                    snapshot_current = snapshot.lstat()
                except OSError as exc:
                    raise ScannerWorkflowError(
                        f"preserved scanner source changed while snapshotting: {source.name}"
                    ) from exc
                snapshot_stat = os.fstat(snapshot_fd)
                if (
                    _stat_identity(before) != _stat_identity(after)
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_dev != after.st_dev
                    or current.st_ino != after.st_ino
                    or _stat_identity(current) != _stat_identity(after)
                    or not stat.S_ISREG(snapshot_current.st_mode)
                    or snapshot_current.st_dev != snapshot_stat.st_dev
                    or snapshot_current.st_ino != snapshot_stat.st_ino
                    or snapshot_stat.st_size != after.st_size
                    or stat.S_IMODE(snapshot_stat.st_mode) != 0o400
                    or not secrets.compare_digest(digest.hexdigest(), expected_sha256)
                ):
                    raise ScannerWorkflowError(
                        f"preserved scanner source changed while snapshotting: {source.name}"
                    )

                snapshot_read_fd = os.open(
                    snapshot,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
                snapshot_read_stat = os.fstat(snapshot_read_fd)
                snapshot_read_current = snapshot.lstat()
                if (
                    not stat.S_ISREG(snapshot_read_stat.st_mode)
                    or snapshot_read_current.st_dev != snapshot_read_stat.st_dev
                    or snapshot_read_current.st_ino != snapshot_read_stat.st_ino
                    or _stat_identity(snapshot_read_current)
                    != _stat_identity(snapshot_read_stat)
                    or _stat_identity(snapshot_read_stat) != _stat_identity(snapshot_stat)
                ):
                    raise ScannerWorkflowError(
                        f"preview snapshot changed before derivation: {source.name}"
                    )
            finally:
                if snapshot_fd >= 0:
                    os.close(snapshot_fd)
                os.close(source_fd)

            yield snapshot_read_fd
        finally:
            if snapshot_read_fd >= 0:
                os.close(snapshot_read_fd)


def _descriptor_sha256(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restore_claimed_file(claimed: Path, destination: Path) -> bool:
    try:
        claimed_before = claimed.lstat()
        os.link(claimed, destination, follow_symlinks=False)
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
        _fsync_directory(destination.parent)
        return True
    except (FileExistsError, FileNotFoundError, OSError):
        return False


def _claim_owned_regular_file(
    path: Path,
    expected_sha256: str,
    expected_identity: tuple[int, int, int, int],
    *,
    marker: str,
) -> Path | None:
    claimed = path.with_name(
        f".{path.name}.{secrets.token_hex(8)}.{marker}"
    )
    try:
        _rename_noreplace(path, claimed)
    except (FileExistsError, FileNotFoundError, OSError, ScannerWorkflowError):
        return None
    try:
        current_sha256, current_identity = _regular_file_snapshot(
            claimed,
            purpose="scanner claimed file",
        )
    except ScannerWorkflowError:
        _restore_claimed_file(claimed, path)
        return None
    if (
        current_identity != expected_identity
        or not secrets.compare_digest(current_sha256, expected_sha256)
    ):
        _restore_claimed_file(claimed, path)
        return None
    return claimed


def _restore_owned_claim(
    claim: Path,
    expected_sha256: str,
    expected_identity: tuple[int, int, int, int],
    destination: Path,
) -> bool:
    owned = _claim_owned_regular_file(
        claim,
        expected_sha256,
        expected_identity,
        marker="restore-claim",
    )
    if owned is None:
        return False
    return _restore_claimed_file(owned, destination)


def _remove_owned_claim(
    claim: Path,
    expected_sha256: str,
    expected_identity: tuple[int, int, int, int],
) -> bool:
    owned = _claim_owned_regular_file(
        claim,
        expected_sha256,
        expected_identity,
        marker="cleanup-claim",
    )
    if owned is None:
        return False
    try:
        current_sha256, current_identity = _regular_file_snapshot(
            owned,
            purpose="scanner cleanup claim",
        )
        if (
            current_identity != expected_identity
            or not secrets.compare_digest(current_sha256, expected_sha256)
        ):
            _restore_claimed_file(owned, claim)
            return False
        owned.unlink()
        _fsync_directory(owned.parent)
        return True
    except (OSError, ScannerWorkflowError):
        _restore_claimed_file(owned, claim)
        return False


def _prepare_thumbnail_repair(
    source: Path,
    thumbnail: Path,
) -> _ThumbnailRepairState:
    preimage_claim: Path | None = None
    preimage_sha256: str | None = None
    preimage_identity: tuple[int, int, int, int] | None = None
    if os.path.lexists(thumbnail):
        preimage_sha256, preimage_identity = _regular_file_snapshot(
            thumbnail,
            purpose="scan thumbnail preimage",
        )
        preimage_claim = _claim_owned_regular_file(
            thumbnail,
            preimage_sha256,
            preimage_identity,
            marker="rollback-preimage",
        )
        if preimage_claim is None:
            raise ScannerWorkflowError(
                f"scan thumbnail changed while preparing repair: {thumbnail.name}"
            )
    try:
        published = _write_thumbnail(source, thumbnail)
        if published is None:
            raise ScannerWorkflowError(
                f"scan thumbnail changed while preparing repair: {thumbnail.name}"
            )
        published_sha256, published_identity = _regular_file_snapshot(
            thumbnail,
            purpose="repaired scan thumbnail",
        )
        if published_identity[:2] != published.identity[:2]:
            raise ScannerWorkflowError(
                f"scan thumbnail changed while preparing repair: {thumbnail.name}"
            )
    except Exception as exc:
        if (
            preimage_claim is not None
            and preimage_sha256 is not None
            and preimage_identity is not None
            and not _restore_owned_claim(
                preimage_claim,
                preimage_sha256,
                preimage_identity,
                thumbnail,
            )
        ):
            raise ScannerWorkflowError(
                "failed to restore scan thumbnail after repair preparation; "
                f"original preimage preserved at {preimage_claim}"
            ) from exc
        raise
    return _ThumbnailRepairState(
        thumbnail=thumbnail,
        preimage_claim=preimage_claim,
        preimage_sha256=preimage_sha256,
        preimage_identity=preimage_identity,
        published_sha256=published_sha256,
        published_identity=published_identity,
    )


def _rollback_thumbnail_repair(state: _ThumbnailRepairState) -> bool:
    published_claim = _claim_owned_regular_file(
        state.thumbnail,
        state.published_sha256,
        state.published_identity,
        marker="rollback-published",
    )
    if published_claim is None:
        return False
    if state.preimage_claim is not None:
        if state.preimage_sha256 is None or state.preimage_identity is None:
            return False
        if not _restore_owned_claim(
            state.preimage_claim,
            state.preimage_sha256,
            state.preimage_identity,
            state.thumbnail,
        ):
            _restore_claimed_file(published_claim, state.thumbnail)
            return False
    _remove_owned_claim(
        published_claim,
        state.published_sha256,
        state.published_identity,
    )
    return True


def _cleanup_thumbnail_repair(state: _ThumbnailRepairState) -> None:
    if (
        state.preimage_claim is None
        or state.preimage_sha256 is None
        or state.preimage_identity is None
    ):
        return
    _remove_owned_claim(
        state.preimage_claim,
        state.preimage_sha256,
        state.preimage_identity,
    )


def _remove_created_artifact(state: _CreatedArtifactState) -> bool:
    return _remove_owned_claim(
        state.path,
        state.sha256,
        state.identity,
    )


def _observation_commit_path(paths: ScanSessionPaths) -> Path:
    return paths.root / OBSERVATION_COMMIT_FILE


def _metadata_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _observation_commit_payload(
    *,
    previous_session_text: str,
    previous_review_text: str,
    previous_findings_text: str,
    next_session_text: str,
    next_review_text: str,
    next_findings_text: str,
) -> dict[str, Any]:
    previous = {
        "session": _metadata_text_sha256(previous_session_text),
        "review": _metadata_text_sha256(previous_review_text),
        "findings": _metadata_text_sha256(previous_findings_text),
    }
    next_state = {
        "session": {
            "sha256": _metadata_text_sha256(next_session_text),
            "text": next_session_text,
        },
        "review": {
            "sha256": _metadata_text_sha256(next_review_text),
            "text": next_review_text,
        },
        "findings": {
            "sha256": _metadata_text_sha256(next_findings_text),
            "text": next_findings_text,
        },
    }
    return {
        "schema_version": 1,
        "kind": "digitalisierer.observation-metadata-commit",
        "previous": previous,
        "next": next_state,
    }


def _clear_observation_commit_marker(
    paths: ScanSessionPaths,
    *,
    expected_sha256: str,
) -> None:
    marker = _observation_commit_path(paths)
    current_sha256, current_identity = _regular_file_snapshot(
        marker,
        purpose="observation metadata commit marker",
    )
    if not secrets.compare_digest(current_sha256, expected_sha256):
        raise ScannerWorkflowError(
            "observation metadata commit marker changed before cleanup"
        )
    claimed = _claim_owned_regular_file(
        marker,
        current_sha256,
        current_identity,
        marker="commit-cleanup",
    )
    if claimed is None:
        raise ScannerWorkflowError(
            "observation metadata commit marker changed before cleanup"
        )
    try:
        claim_sha256, claim_identity = _regular_file_snapshot(
            claimed,
            purpose="claimed observation metadata commit marker",
        )
        if (
            claim_identity != current_identity
            or not secrets.compare_digest(claim_sha256, current_sha256)
        ):
            raise ScannerWorkflowError(
                "observation metadata commit marker changed before cleanup"
            )
        claimed.unlink()
        _fsync_directory(paths.root)
    except Exception:
        if not os.path.lexists(marker):
            _restore_claimed_file(claimed, marker)
        raise


def _recover_observation_metadata_commit(paths: ScanSessionPaths) -> None:
    marker = _observation_commit_path(paths)
    raw_marker = _stable_file_bytes(marker)
    marker_sha256 = hashlib.sha256(raw_marker).hexdigest()
    try:
        payload = json.loads(raw_marker.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScannerWorkflowError(
            "observation metadata commit marker is invalid"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != "digitalisierer.observation-metadata-commit"
        or set(payload) != {"schema_version", "kind", "previous", "next"}
    ):
        raise ScannerWorkflowError(
            "observation metadata commit marker is invalid"
        )
    previous = payload.get("previous")
    next_state = payload.get("next")
    if (
        not isinstance(previous, dict)
        or not isinstance(next_state, dict)
        or set(previous) != {"session", "review", "findings"}
        or set(next_state) != {"session", "review", "findings"}
    ):
        raise ScannerWorkflowError(
            "observation metadata commit marker is invalid"
        )

    paths_by_name = {
        "session": paths.session_file,
        "review": paths.review_file,
        "findings": paths.findings_file,
    }
    next_text: dict[str, str] = {}
    next_sha256: dict[str, str] = {}
    for name in ("session", "review", "findings"):
        previous_sha = previous.get(name)
        entry = next_state.get(name)
        if (
            not _is_sha256(previous_sha)
            or not isinstance(entry, dict)
            or set(entry) != {"sha256", "text"}
            or not _is_sha256(entry.get("sha256"))
            or not isinstance(entry.get("text"), str)
        ):
            raise ScannerWorkflowError(
                "observation metadata commit marker is invalid"
            )
        text_value = entry["text"]
        digest = _metadata_text_sha256(text_value)
        if not secrets.compare_digest(digest, entry["sha256"]):
            raise ScannerWorkflowError(
                "observation metadata commit marker is invalid"
            )
        next_text[name] = text_value
        next_sha256[name] = digest

    try:
        next_session = json.loads(next_text["session"])
        next_review = json.loads(next_text["review"])
        next_findings = json.loads(next_text["findings"])
    except json.JSONDecodeError as exc:
        raise ScannerWorkflowError(
            "observation metadata commit marker is invalid"
        ) from exc
    if (
        not isinstance(next_session, dict)
        or not isinstance(next_review, dict)
        or not isinstance(next_findings, dict)
    ):
        raise ScannerWorkflowError(
            "observation metadata commit marker is invalid"
        )
    _validate_session_identity(paths, next_session)
    _validate_review_payload(next_review)
    _validate_findings_payload(next_findings)
    next_processing = _processing_session_from_payload(
        paths,
        next_session,
        next_review,
    )
    _validate_findings_asset_ids(next_processing, next_findings)

    current_sha256: dict[str, str] = {}
    for name, metadata_path in paths_by_name.items():
        current = hashlib.sha256(_stable_file_bytes(metadata_path)).hexdigest()
        previous_sha = previous[name]
        if (
            not secrets.compare_digest(current, previous_sha)
            and not secrets.compare_digest(current, next_sha256[name])
        ):
            raise ScannerWorkflowError(
                f"scanner {name} metadata diverged during interrupted commit"
            )
        current_sha256[name] = current

    for name, metadata_path in paths_by_name.items():
        if not secrets.compare_digest(
            current_sha256[name],
            next_sha256[name],
        ):
            _atomic_write_text(metadata_path, next_text[name])

    for name, metadata_path in paths_by_name.items():
        current = hashlib.sha256(_stable_file_bytes(metadata_path)).hexdigest()
        if not secrets.compare_digest(current, next_sha256[name]):
            raise ScannerWorkflowError(
                f"scanner {name} metadata recovery did not converge"
            )

    _clear_observation_commit_marker(
        paths,
        expected_sha256=marker_sha256,
    )


def _copy_preserved(
    source: Path,
    target: Path,
    *,
    expected_sha256: str,
    expected_stat: os.stat_result,
) -> _CreatedArtifactState | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_hash = _existing_regular_file_hash(target)
    if existing_hash is not None:
        if existing_hash != expected_sha256:
            raise ScannerWorkflowError(
                f"preserved source collision for {target.name}"
            )
        return None
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with _stable_source_descriptor(
            source,
            expected_stat=expected_stat,
        ) as (source_descriptor, _):
            with (
                os.fdopen(os.dup(source_descriptor), "rb") as source_handle,
                temporary.open("xb") as target_handle,
            ):
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
        copied_sha = _sha256_file(temporary)
        if copied_sha != expected_sha256:
            raise ScannerWorkflowError(
                f"scan source changed while preserving: {source}"
            )
        os.chmod(temporary, 0o600)
        expected_identity = _stat_identity(
            os.stat(temporary, follow_symlinks=False)
        )
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing_hash = _existing_regular_file_hash(target)
            if existing_hash is None or existing_hash != expected_sha256:
                raise ScannerWorkflowError(
                    f"preserved source collision for {target.name}"
                )
            temporary.unlink()
            return None
        expected_state = _CreatedArtifactState(
            path=target,
            sha256=expected_sha256,
            identity=expected_identity,
        )
        try:
            published_sha256, published_identity = _regular_file_snapshot(
                target,
                purpose="published preserved source",
            )
            if (
                published_identity != expected_identity
                or not secrets.compare_digest(published_sha256, expected_sha256)
            ):
                raise ScannerWorkflowError(
                    f"preserved source changed during publication: {target.name}"
                )
        except Exception as exc:
            if not _remove_created_artifact(expected_state):
                raise ScannerWorkflowError(
                    f"failed to roll back published preserved source: {target.name}"
                ) from exc
            raise
        temporary.unlink()
        return _CreatedArtifactState(
            path=target,
            sha256=published_sha256,
            identity=published_identity,
        )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _asset_id(source_name: str, sha256: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(source_name).stem).strip("-._")
    stem = stem or "page"
    return f"{stem[:40]}--{sha256[:12]}"


def _next_collision_asset_id(base_asset_id: str, known_by_id: dict[str, dict[str, Any]]) -> str:
    collision_index = 2
    while True:
        candidate = f"{base_asset_id}--{collision_index}"
        if candidate not in known_by_id:
            return candidate
        collision_index += 1


def _replace_preserved(
    source: Path,
    target: Path,
    *,
    expected_sha256: str,
    expected_stat: os.stat_result,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with _stable_source_descriptor(
            source,
            expected_stat=expected_stat,
        ) as (source_descriptor, _):
            with (
                os.fdopen(os.dup(source_descriptor), "rb") as source_handle,
                temporary.open("xb") as target_handle,
            ):
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
        copied_sha = _sha256_file(temporary)
        if copied_sha != expected_sha256:
            raise ScannerWorkflowError(
                f"scan source changed while repairing preserved copy: {source}"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _repair_recorded_asset(
    paths: ScanSessionPaths,
    source: Path,
    record: dict[str, Any],
    *,
    expected_sha256: str,
    expected_stat: os.stat_result,
) -> tuple[dict[str, Any], Path, Path] | None:
    asset_id = record.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise ScannerWorkflowError("scan asset record has an invalid asset_id")
    if record.get("source_name") != source.name or record.get("capture_source") != str(source):
        raise ScannerWorkflowError(f"recorded scan source identity is invalid for {asset_id}")

    suffix = source.suffix.lower()
    expected_preserved_rel = f"sources/{asset_id}{suffix}"
    expected_thumbnail_rel = f"thumbnails/{asset_id}.jpg"
    if record.get("preserved_path") != expected_preserved_rel:
        raise ScannerWorkflowError(
            f"preserved scanner source path is not canonical for {asset_id}"
        )
    if record.get("thumbnail_path") != expected_thumbnail_rel:
        raise ScannerWorkflowError(
            f"scan thumbnail path is not canonical for {asset_id}"
        )

    try:
        root = paths.root.resolve(strict=True)
        sources_root = paths.sources.resolve(strict=True)
        thumbnails_root = paths.thumbnails.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScannerWorkflowError("scanner storage directories are invalid") from exc
    if (
        paths.sources.is_symlink()
        or paths.thumbnails.is_symlink()
        or not sources_root.is_dir()
        or not thumbnails_root.is_dir()
        or sources_root.parent != root
        or thumbnails_root.parent != root
    ):
        raise ScannerWorkflowError("scanner storage directories escaped session root")

    preserved = sources_root / f"{asset_id}{suffix}"
    if preserved.is_symlink() or (preserved.exists() and not preserved.is_file()):
        raise ScannerWorkflowError(
            f"preserved scanner source must be a regular file: {expected_preserved_rel}"
        )
    try:
        preserved_sha256, _ = _regular_file_snapshot(
            preserved,
            purpose="preserved scanner source",
        )
    except ScannerWorkflowError:
        if os.path.lexists(preserved):
            raise
        preserved_sha256 = None
    if preserved_sha256 != expected_sha256:
        _replace_preserved(
            source,
            preserved,
            expected_sha256=expected_sha256,
            expected_stat=expected_stat,
        )
    preserved_sha256, _ = _regular_file_snapshot(
        preserved,
        purpose="preserved scanner source",
    )
    if not secrets.compare_digest(preserved_sha256, expected_sha256):
        raise ScannerWorkflowError(f"preserved source hash mismatch for {asset_id}")

    thumbnail = thumbnails_root / f"{asset_id}.jpg"
    if thumbnail.is_symlink() or (thumbnail.exists() and not thumbnail.is_file()):
        raise ScannerWorkflowError(
            f"scan thumbnail must be the canonical regular file for {asset_id}"
        )
    thumbnail_sha256 = record.get("thumbnail_sha256")
    thumbnail_valid = False
    if (
        isinstance(thumbnail_sha256, str)
        and len(thumbnail_sha256) == 64
        and all(char in "0123456789abcdef" for char in thumbnail_sha256)
        and thumbnail.exists()
    ):
        current_thumbnail_sha256, _ = _regular_file_snapshot(
            thumbnail,
            purpose="scan thumbnail",
        )
        thumbnail_valid = secrets.compare_digest(
            current_thumbnail_sha256,
            thumbnail_sha256,
        )
    if thumbnail_valid:
        return None
    return record, preserved, thumbnail


def _finding_payload(finding: QualityFinding) -> dict[str, object]:
    return {
        "kind": finding.kind,
        "message": finding.message,
        "asset_ids": list(finding.asset_ids),
        "confidence": finding.confidence,
        "evidence": list(finding.evidence),
    }


def _findings_from_assets(assets: list[dict[str, Any]]) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    areas = [
        int(asset["image"]["width"]) * int(asset["image"]["height"])
        for asset in assets
        if isinstance(asset.get("image"), dict)
    ]
    median_area = statistics.median(areas) if areas else 0.0

    for asset in assets:
        image = asset.get("image")
        asset_id = str(asset.get("asset_id"))
        if not isinstance(image, dict):
            continue
        stddev = float(image["stddev"])
        dark_ratio = float(image["dark_ratio"])
        area = int(image["width"]) * int(image["height"])
        if stddev < 12.0 or dark_ratio < 0.005:
            findings.append(
                QualityFinding(
                    kind="blank-or-near-blank",
                    message="page looks blank or almost blank",
                    asset_ids=(asset_id,),
                    evidence=(
                        f"stddev={stddev:.3f}",
                        f"dark_ratio={dark_ratio:.6f}",
                    ),
                )
            )
        if median_area and area < median_area * 0.55:
            findings.append(
                QualityFinding(
                    kind="small-page",
                    message="page dimensions are unusually small for this session",
                    asset_ids=(asset_id,),
                    evidence=(f"area={area}", f"median_area={median_area:.1f}"),
                )
            )

    near_duplicate_assets = [
        (str(asset["asset_id"]), int(str(asset["image"]["average_hash"]), 16))
        for asset in assets
        if isinstance(asset.get("image"), dict)
    ]
    parents = list(range(len(near_duplicate_assets)))
    similar_pair_counts = [0] * len(near_duplicate_assets)

    def find_root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for first_index, (_, first_hash) in enumerate(near_duplicate_assets):
        for second_index in range(first_index + 1, len(near_duplicate_assets)):
            second_hash = near_duplicate_assets[second_index][1]
            distance = (first_hash ^ second_hash).bit_count()
            if distance > 20:
                continue

            first_root = find_root(first_index)
            second_root = find_root(second_index)
            if first_root == second_root:
                similar_pair_counts[first_root] += 1
                continue
            if first_root > second_root:
                first_root, second_root = second_root, first_root
            parents[second_root] = first_root
            similar_pair_counts[first_root] += similar_pair_counts[second_root] + 1
            similar_pair_counts[second_root] = 0

    near_duplicate_groups: dict[int, list[str]] = {}
    for index, (asset_id, _) in enumerate(near_duplicate_assets):
        root = find_root(index)
        if similar_pair_counts[root]:
            near_duplicate_groups.setdefault(root, []).append(asset_id)

    for root, asset_ids in near_duplicate_groups.items():
        findings.append(
            QualityFinding(
                kind="near-duplicate",
                message="pages form a near-duplicate visual-hash similarity group",
                asset_ids=tuple(asset_ids),
                evidence=(
                    "average_hash_distance_threshold=20",
                    f"member_count={len(asset_ids)}",
                    f"similar_pair_count={similar_pair_counts[root]}",
                ),
            )
        )
    return findings


def _observe_scan_folder_unlocked(
    paths: ScanSessionPaths,
    folder: Path,
    *,
    start: int = 1,
    limit: int | None = None,
) -> ScanObservation:
    if start < 1:
        raise ValueError("start must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    source_root = folder.expanduser().resolve(strict=True)
    sources = image_files(source_root)
    selected = sources[start - 1 :]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ScannerWorkflowError("selected capture range contains no images")

    before_signature = [
        (path.name, path.stat().st_size, path.stat().st_mtime_ns)
        for path in selected
    ]
    session = _load_json(paths.session_file)
    _validate_session_identity(paths, session)
    raw_assets = session.get("assets")
    if not isinstance(raw_assets, list):
        raise ScannerWorkflowError("scan session assets must be a list")
    assets: list[dict[str, Any]] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            raise ScannerWorkflowError("scan asset record must be an object")
        _validate_scan_asset_record(item)
        assets.append(dict(item))
    known_by_id = {
        str(item.get("asset_id")): item
        for item in assets
        if isinstance(item.get("asset_id"), str)
    }
    known_by_capture_source: dict[str, dict[str, Any]] = {}
    for item in assets:
        capture_source = item.get("capture_source")
        if not isinstance(capture_source, str):
            continue
        if capture_source in known_by_capture_source:
            raise ScannerWorkflowError(
                f"scan session records capture source more than once: {capture_source}"
            )
        known_by_capture_source[capture_source] = item

    known_capture_paths = [
        Path(capture_source)
        for capture_source in known_by_capture_source
        if Path(capture_source).parent == source_root
    ]
    unseen_selected = [
        source for source in selected if str(source) not in known_by_capture_source
    ]
    if known_capture_paths and unseen_selected:
        ordered_capture_sources = sorted(
            set(sources).union(known_capture_paths),
            key=_natural_key,
        )
        positions = {
            str(source): index
            for index, source in enumerate(ordered_capture_sources, start=1)
        }
        latest_known_position = max(
            positions[str(source)] for source in known_capture_paths
        )
        if any(
            positions[str(source)] < latest_known_position
            for source in unseen_selected
        ):
            raise ScannerWorkflowError(
                "out-of-order capture backfill is not supported; "
                "observe ranges from this capture folder in natural source order"
            )

    imported: list[str] = []
    skipped: list[str] = []
    pending_thumbnail_repairs: list[tuple[dict[str, Any], Path, Path]] = []
    created_artifacts: list[_CreatedArtifactState] = []

    review = _load_json(paths.review_file)
    _validate_review_payload(review)
    try:
        previous_session_text = _stable_file_bytes(paths.session_file).decode("utf-8")
        previous_review_text = _stable_file_bytes(paths.review_file).decode("utf-8")
        previous_findings_text = _stable_file_bytes(paths.findings_file).decode("utf-8")
    except UnicodeError as exc:
        raise ScannerWorkflowError(
            "scan metadata cannot be snapshotted before observation"
        ) from exc
    review_items = review.get("items")
    if not isinstance(review_items, dict):
        raise ScannerWorkflowError("scan review items must be an object")
    missing_review_ids = sorted(
        asset_id
        for asset_id in known_by_id
        if not isinstance(review_items.get(asset_id), dict)
    )
    if missing_review_ids:
        raise ScannerWorkflowError(
            "scan review is missing a decision for existing asset(s): "
            + ", ".join(missing_review_ids)
        )

    existing_sequences: list[int] = []
    for decision in review_items.values():
        if not isinstance(decision, dict):
            continue
        sequence = decision.get("sequence")
        if sequence is None:
            continue
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ScannerWorkflowError("scan review contains an invalid sequence")
        existing_sequences.append(sequence)
    next_sequence = max([start - 1, *existing_sequences]) + 1

    try:
        for source in selected:
            sha256, source_stat = _stable_hash(source)
            source_reference = str(source)
            existing_occurrence = known_by_capture_source.get(source_reference)
            if existing_occurrence is not None:
                asset_id = existing_occurrence.get("asset_id")
                if not isinstance(asset_id, str) or not asset_id:
                    raise ScannerWorkflowError("scan asset record has an invalid asset_id")
                if existing_occurrence.get("sha256") != sha256:
                    raise ScannerWorkflowError(
                        f"capture source changed since observation for {asset_id}"
                    )
                pending_repair = _repair_recorded_asset(
                    paths,
                    source,
                    existing_occurrence,
                    expected_sha256=sha256,
                    expected_stat=source_stat,
                )
                if pending_repair is not None:
                    pending_thumbnail_repairs.append(pending_repair)
                skipped.append(asset_id)
                continue

            base_asset_id = _asset_id(source.name, sha256)
            asset_id = base_asset_id
            existing = known_by_id.get(base_asset_id)
            if existing is not None:
                if existing.get("sha256") != sha256:
                    raise ScannerWorkflowError(
                        f"asset identity collision for {base_asset_id}"
                    )
                asset_id = _next_collision_asset_id(base_asset_id, known_by_id)

            suffix = source.suffix.lower()
            preserved_rel = f"sources/{asset_id}{suffix}"
            thumbnail_rel = f"thumbnails/{asset_id}.jpg"
            target = paths.root / preserved_rel
            attempt_created: list[_CreatedArtifactState] = []
            try:
                source_created = _copy_preserved(
                    source,
                    target,
                    expected_sha256=sha256,
                    expected_stat=source_stat,
                )
                if source_created is not None:
                    attempt_created.append(source_created)
                with _verified_preview_source_snapshot(
                    target,
                    expected_sha256=sha256,
                ) as preview_source:
                    image = _inspect_image(preview_source)
                    thumbnail_path = paths.root / thumbnail_rel
                    thumbnail_created = _write_thumbnail(
                        preview_source,
                        thumbnail_path,
                    )
                if thumbnail_created is not None:
                    attempt_created.append(thumbnail_created)
                thumbnail_sha256, _ = _regular_file_snapshot(
                    thumbnail_path,
                    purpose="scan thumbnail",
                )
                record: dict[str, Any] = {
                    "asset_id": asset_id,
                    "source_name": source.name,
                    "capture_source": str(source),
                    "sha256": sha256,
                    "bytes": source_stat.st_size,
                    "preserved_path": preserved_rel,
                    "thumbnail_path": thumbnail_rel,
                    "thumbnail_sha256": thumbnail_sha256,
                    "image": image,
                }
            except Exception as exc:
                cleanup_failed = False
                for state in reversed(attempt_created):
                    if not _remove_created_artifact(state):
                        cleanup_failed = True
                if cleanup_failed:
                    raise ScannerWorkflowError(
                        "failed to roll back newly created scanner artifacts"
                    ) from exc
                raise
            created_artifacts.extend(attempt_created)
            assets.append(record)
            known_by_id[asset_id] = record
            known_by_capture_source[source_reference] = record
            review_items[asset_id] = {
                "sequence": next_sequence,
                "included": True,
                "replacement_for": None,
            }
            next_sequence += 1
            imported.append(asset_id)

        findings = _findings_from_assets(assets)
        session["assets"] = assets
        observations = session.setdefault("capture_observations", [])
        if not isinstance(observations, list):
            raise ScannerWorkflowError("capture observations must be a list")
        observations.append(
            {
                "source_folder": str(source_root),
                "selected_start": start,
                "selected_count": len(selected),
                "selected_names": [path.name for path in selected],
                "imported_asset_ids": imported,
                "skipped_asset_ids": skipped,
            }
        )
        review["items"] = review_items

    except Exception as exc:
        cleanup_failed = False
        for state in reversed(created_artifacts):
            if not _remove_created_artifact(state):
                cleanup_failed = True
        if cleanup_failed:
            raise ScannerWorkflowError(
                "failed to roll back scanner observation preparation"
            ) from exc
        raise

    def current_capture_signature() -> list[tuple[str, int, int]]:
        current_sources = image_files(source_root)
        current_selected = current_sources[start - 1 :]
        if limit is not None:
            current_selected = current_selected[:limit]
        return [
            (path.name, path.stat().st_size, path.stat().st_mtime_ns)
            for path in current_selected
        ]

    thumbnail_rollbacks: list[_ThumbnailRepairState] = []
    metadata_mutated = False
    commit_marker_sha256: str | None = None
    try:
        # Avoid publishing dependent metadata when capture membership or identity
        # has already changed while processing.
        if current_capture_signature() != before_signature:
            raise ScannerWorkflowError(
                "capture folder changed while Digitalisierer observed it"
            )

        findings_text = _json_text(
            {
                "schema_version": 1,
                "kind": "digitalisierer.scan-findings",
                "findings": [_finding_payload(item) for item in findings],
            }
        )
        for record, preserved, thumbnail in pending_thumbnail_repairs:
            rollback_state = _prepare_thumbnail_repair(
                preserved,
                thumbnail,
            )
            thumbnail_rollbacks.append(rollback_state)
            record["thumbnail_sha256"] = rollback_state.published_sha256

        # Dependent metadata must not become durable before the artifact
        # directory entries it references. New imports publish both preserved
        # sources and thumbnails; repairs can publish a replacement thumbnail.
        if imported:
            _fsync_directory(paths.sources)
        if imported or pending_thumbnail_repairs:
            _fsync_directory(paths.thumbnails)

        # review/findings depend on the prospective session.json asset set.
        # Persist a recoverable previous/next marker before the first metadata
        # write. Normal exceptions still roll back; a process/power loss leaves
        # enough exact state to roll forward idempotently on the next locked read.
        next_review_text = _json_text(review)
        next_session_text = _json_text(session)
        commit_payload = _observation_commit_payload(
            previous_session_text=previous_session_text,
            previous_review_text=previous_review_text,
            previous_findings_text=previous_findings_text,
            next_session_text=next_session_text,
            next_review_text=next_review_text,
            next_findings_text=findings_text,
        )
        commit_text = _json_text(commit_payload)
        commit_path = _observation_commit_path(paths)
        if os.path.lexists(commit_path):
            raise ScannerWorkflowError(
                "observation metadata commit marker already exists"
            )
        _atomic_write_text(commit_path, commit_text)
        commit_marker_sha256 = _metadata_text_sha256(commit_text)

        metadata_mutated = True
        _atomic_write_text(paths.review_file, next_review_text)
        _atomic_write_text(paths.findings_file, findings_text)
        if current_capture_signature() != before_signature:
            raise ScannerWorkflowError(
                "capture folder changed while Digitalisierer observed it"
            )
        _atomic_write_text(paths.session_file, next_session_text)
    except Exception:
        rollback_error: Exception | None = None

        # Keep every artifact compatible with the forward recovery marker until
        # metadata has converged back to the verified preimage and that marker
        # is durably gone. A crash after artifact rollback begins can therefore
        # never select the forward metadata state on resume.
        if metadata_mutated:
            for metadata_path, previous_text in (
                (paths.session_file, previous_session_text),
                (paths.review_file, previous_review_text),
                (paths.findings_file, previous_findings_text),
            ):
                try:
                    _atomic_write_text(metadata_path, previous_text)
                except Exception as exc:
                    if rollback_error is None:
                        rollback_error = exc
        if rollback_error is None and commit_marker_sha256 is not None:
            try:
                _clear_observation_commit_marker(
                    paths,
                    expected_sha256=commit_marker_sha256,
                )
            except Exception as exc:
                rollback_error = exc

        if rollback_error is None:
            for rollback_state in reversed(thumbnail_rollbacks):
                try:
                    if not _rollback_thumbnail_repair(rollback_state):
                        raise ScannerWorkflowError(
                            "scan thumbnail changed while rolling back repair"
                        )
                except Exception as exc:
                    if rollback_error is None:
                        rollback_error = exc
            for created_state in reversed(created_artifacts):
                try:
                    if not _remove_created_artifact(created_state):
                        raise ScannerWorkflowError(
                            "new scanner artifact changed before rollback"
                        )
                except Exception as exc:
                    if rollback_error is None:
                        rollback_error = exc

        if rollback_error is not None:
            raise ScannerWorkflowError(
                "failed to restore scanner observation after failure"
            ) from rollback_error
        raise
    else:
        if commit_marker_sha256 is not None:
            _clear_observation_commit_marker(
                paths,
                expected_sha256=commit_marker_sha256,
            )
        for rollback_state in thumbnail_rollbacks:
            _cleanup_thumbnail_repair(rollback_state)
    return ScanObservation(
        session_root=paths.root,
        imported_asset_ids=tuple(imported),
        skipped_asset_ids=tuple(skipped),
        findings=tuple(findings),
    )


def observe_scan_folder(
    paths: ScanSessionPaths,
    folder: Path,
    *,
    start: int = 1,
    limit: int | None = None,
) -> ScanObservation:
    with _review_update_lock(paths):
        return _observe_scan_folder_unlocked(
            paths,
            folder,
            start=start,
            limit=limit,
        )


def _stable_json_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    raw = _stable_file_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScannerWorkflowError(f"scanner session JSON is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ScannerWorkflowError(f"scanner session JSON must be an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _preserved_source_path(
    paths: ScanSessionPaths,
    asset_id: str,
    relative: str,
    *,
    require_exists: bool = True,
) -> Path:
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or len(relative_path.parts) != 2
        or relative_path.parts[0] != "sources"
        or ".." in relative_path.parts
    ):
        raise ScannerWorkflowError(
            f"preserved scanner source path is outside session sources: {relative}"
        )
    suffix = relative_path.suffix.lower()
    expected_relative = Path("sources") / f"{asset_id}{suffix}"
    try:
        root = paths.root.resolve(strict=True)
        sources_root = paths.sources.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScannerWorkflowError(
            f"preserved scanner source path is invalid: {relative}"
        ) from exc
    if (
        paths.sources.is_symlink()
        or not sources_root.is_dir()
        or sources_root.parent != root
    ):
        raise ScannerWorkflowError("scanner sources directory escaped session root")

    candidate = sources_root / f"{asset_id}{suffix}"
    lexical_candidate = paths.root / relative_path
    if os.path.lexists(lexical_candidate):
        try:
            current = lexical_candidate.lstat()
            resolved = lexical_candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ScannerWorkflowError(
                f"preserved scanner source path is invalid: {relative}"
            ) from exc
        try:
            resolved.relative_to(sources_root)
        except ValueError as exc:
            raise ScannerWorkflowError(
                f"preserved scanner source path is outside session sources: {relative}"
            ) from exc
        if (
            suffix not in IMAGE_SUFFIXES
            or relative_path != expected_relative
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or resolved != candidate
        ):
            raise ScannerWorkflowError(
                f"preserved scanner source path is not canonical for {asset_id}"
            )
        return candidate

    if suffix not in IMAGE_SUFFIXES or relative_path != expected_relative:
        raise ScannerWorkflowError(
            f"preserved scanner source path is not canonical for {asset_id}"
        )
    if require_exists:
        raise ScannerWorkflowError(
            f"preserved scanner source must be a regular file: {relative}"
        )
    return candidate


def _processing_session_from_payload(
    paths: ScanSessionPaths,
    session: dict[str, Any],
    review: dict[str, Any],
    *,
    repairable_capture_sources: frozenset[str] = frozenset(),
) -> ProcessingSession:
    _validate_session_identity(paths, session)
    _validate_session_storage_directory(paths, paths.sources, "sources")
    _validate_review_payload(review)
    raw_assets = session.get("assets")
    raw_review = review.get("items")
    if not isinstance(raw_assets, list) or not isinstance(raw_review, dict):
        raise ScannerWorkflowError("scan session/review state has invalid shape")

    items: list[SessionAsset] = []
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise ScannerWorkflowError("scan asset record must be an object")
        asset_id, sha256, preserved = _validate_scan_asset_record(raw)
        capture_source = raw["capture_source"]
        assert isinstance(capture_source, str)
        capture_path = Path(capture_source)
        source_is_repairable = (
            capture_source in repairable_capture_sources
            and not capture_path.is_symlink()
            and capture_path.is_file()
        )
        decision = raw_review.get(asset_id)
        if not isinstance(decision, dict):
            raise ScannerWorkflowError(f"review state missing for {asset_id}")
        sequence = decision.get("sequence")
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
        ):
            raise ScannerWorkflowError(f"invalid sequence for {asset_id}")
        included = decision.get("included")
        if not isinstance(included, bool):
            raise ScannerWorkflowError(f"invalid inclusion state for {asset_id}")
        replacement_for = decision.get("replacement_for")
        if replacement_for is not None and not isinstance(replacement_for, str):
            raise ScannerWorkflowError(f"invalid replacement target for {asset_id}")
        items.append(
            SessionAsset(
                MediaAsset(
                    asset_id=asset_id,
                    path=_preserved_source_path(
                        paths,
                        asset_id,
                        preserved,
                        require_exists=not source_is_repairable,
                    ),
                    kind=MediaKind.DOCUMENT_IMAGE,
                    sha256=sha256,
                ),
                sequence=sequence,
                included=included,
                replacement_for=replacement_for,
            )
        )

    asset_ids = {item.asset.asset_id for item in items}
    orphan_review_ids = set(raw_review).difference(asset_ids)
    if orphan_review_ids:
        raise ScannerWorkflowError(
            "review state contains assets missing from scan session: "
            + ", ".join(sorted(orphan_review_ids))
        )
    try:
        return ProcessingSession(
            session_id=str(session.get("session_id")),
            root=paths.root,
            items=items,
        )
    except ValueError as exc:
        raise ScannerWorkflowError(f"invalid scanner review state: {exc}") from exc


def _validate_findings_asset_ids(
    processing: ProcessingSession,
    findings: dict[str, Any],
) -> None:
    known_asset_ids = {item.asset.asset_id for item in processing.items}
    raw_findings = findings.get("findings")
    if not isinstance(raw_findings, list):
        raise ScannerWorkflowError("scan findings must be a list")
    for finding in raw_findings:
        if not isinstance(finding, dict):
            raise ScannerWorkflowError("scan finding entry must be an object")
        asset_ids = finding.get("asset_ids")
        if not isinstance(asset_ids, list):
            raise ScannerWorkflowError("scan finding entry has invalid shape")
        for asset_id in asset_ids:
            if asset_id not in known_asset_ids:
                raise ScannerWorkflowError(
                    f"scan finding references unknown asset: {asset_id}"
                )


def _load_review_state_unlocked(
    paths: ScanSessionPaths,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    ProcessingSession,
    str,
]:
    session, _ = _stable_json_snapshot(paths.session_file)
    review_raw = _stable_file_bytes(paths.review_file)
    try:
        review_text = review_raw.decode("utf-8")
        review = json.loads(review_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScannerWorkflowError(
            f"scanner session JSON is invalid: {paths.review_file}"
        ) from exc
    if not isinstance(review, dict):
        raise ScannerWorkflowError(
            f"scanner session JSON must be an object: {paths.review_file}"
        )
    findings, _ = _stable_json_snapshot(paths.findings_file)
    _validate_findings_payload(findings)
    processing = _processing_session_from_payload(paths, session, review)
    _validate_findings_asset_ids(processing, findings)
    return session, review, findings, processing, review_text


def load_review_state(
    paths: ScanSessionPaths,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    ProcessingSession,
]:
    with _review_update_lock(paths):
        session, review, findings, processing, _ = _load_review_state_unlocked(paths)
        return session, review, findings, processing


def load_processing_session(paths: ScanSessionPaths) -> ProcessingSession:
    return load_review_state(paths)[3]


_UNSET = object()


def review_item_snapshot(asset_id: str, decision: dict[str, Any]) -> str:
    """Bind a form to its complete asset decision, not unrelated session edits."""
    payload = json.dumps(
        {"asset_id": asset_id, "decision": decision},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _update_review_item_unlocked(
    paths: ScanSessionPaths,
    asset_id: str,
    *,
    included: bool | None = None,
    sequence: int | None | object = _UNSET,
    replacement_for: str | None | object = _UNSET,
    expected_snapshot: str | None = None,
) -> ProcessingSession:
    session, review, findings, _, old_text = _load_review_state_unlocked(paths)
    items = review.get("items")
    if not isinstance(items, dict) or not isinstance(items.get(asset_id), dict):
        raise ScannerWorkflowError(f"unknown scanner asset: {asset_id}")
    if expected_snapshot is not None and not secrets.compare_digest(
        expected_snapshot, review_item_snapshot(asset_id, items[asset_id])
    ):
        raise ScannerReviewConflict(
            "review decision changed since this form was rendered; reload and retry"
        )
    updated = json.loads(json.dumps(review))
    updated_items = updated["items"]
    decision = updated_items[asset_id]
    if included is not None:
        decision["included"] = included
    if sequence is not _UNSET:
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
        ):
            raise ValueError("sequence must be a positive integer or null")
        decision["sequence"] = sequence
    if replacement_for is not _UNSET:
        if replacement_for is not None and not isinstance(replacement_for, str):
            raise ValueError("replacement_for must be a string or null")
        decision["replacement_for"] = replacement_for

    old_bytes = old_text.encode("utf-8")
    try:
        current_review_bytes = _stable_file_bytes(paths.review_file)
    except ScannerWorkflowError as exc:
        try:
            current_review_stat = paths.review_file.lstat()
        except FileNotFoundError:
            current_review_stat = None
        if current_review_stat is not None and stat.S_ISREG(
            current_review_stat.st_mode
        ):
            raise ScannerReviewConflict(
                "review state changed while this update was prepared; reload and retry"
            ) from exc
        _atomic_write_text(paths.review_file, old_text)
    else:
        if not secrets.compare_digest(current_review_bytes, old_bytes):
            raise ScannerReviewConflict(
                "review state changed while this update was prepared; reload and retry"
            )

    processing = _processing_session_from_payload(paths, session, updated)
    _validate_findings_asset_ids(processing, findings)

    _atomic_write_text(paths.review_file, _json_text(updated))
    try:
        processing = _load_review_state_unlocked(paths)[3]
    except Exception:
        _atomic_write_text(paths.review_file, old_text)
        raise
    return processing


def update_review_item(
    paths: ScanSessionPaths,
    asset_id: str,
    *,
    included: bool | None = None,
    sequence: int | None | object = _UNSET,
    replacement_for: str | None | object = _UNSET,
    expected_snapshot: str | None = None,
) -> ProcessingSession:
    with _review_update_lock(paths):
        return _update_review_item_unlocked(
            paths,
            asset_id,
            included=included,
            sequence=sequence,
            replacement_for=replacement_for,
            expected_snapshot=expected_snapshot,
        )


def _verify_preserved_sources(
    paths: ScanSessionPaths,
    session: dict[str, Any],
) -> list[dict[str, object]]:
    raw_assets = session.get("assets")
    if not isinstance(raw_assets, list):
        raise ScannerWorkflowError("scan session assets must be a list")
    verified: list[dict[str, object]] = []
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise ScannerWorkflowError("scan asset record must be an object")
        asset_id = raw.get("asset_id")
        relative = raw.get("preserved_path")
        expected = raw.get("sha256")
        if (
            not isinstance(asset_id, str)
            or not isinstance(relative, str)
            or not isinstance(expected, str)
        ):
            raise ScannerWorkflowError("scan asset identity is incomplete")
        path = _preserved_source_path(paths, asset_id, relative)
        digest, identity = _regular_file_snapshot(
            path,
            purpose="preserved scanner source",
        )
        if not secrets.compare_digest(digest, expected):
            raise ScannerWorkflowError(
                f"preserved scanner source hash mismatch: {asset_id}"
            )
        verified.append(
            {
                "asset_id": asset_id,
                "sha256": expected,
                "bytes": identity[2],
                "path": relative,
            }
        )
    return verified


@contextmanager
def _verified_export_source_snapshots(
    active: list[MediaAsset],
    staging: Path,
) -> Iterator[list[Path]]:
    bound_paths: list[Path] = []
    with tempfile.TemporaryDirectory(
        prefix=".verified-export-sources.",
        dir=staging,
    ) as snapshot_dir_name:
        snapshot_dir = Path(snapshot_dir_name)
        for index, asset in enumerate(active):
            expected_sha256 = asset.sha256
            if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
                raise ScannerWorkflowError(
                    f"scanner source digest is invalid for export: {asset.asset_id}"
                )
            try:
                source_fd = os.open(
                    asset.path,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
            except OSError as exc:
                raise ScannerWorkflowError(
                    f"preserved scanner source cannot be snapshotted: {asset.asset_id}"
                ) from exc

            suffix = asset.path.suffix or ".bin"
            snapshot_path = snapshot_dir / f"{index:06d}{suffix}"
            snapshot_fd = -1
            try:
                snapshot_fd = os.open(
                    snapshot_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    0o400,
                )
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ScannerWorkflowError(
                        f"preserved scanner source is not regular: {asset.asset_id}"
                    )

                digest = hashlib.sha256()
                with (
                    os.fdopen(os.dup(source_fd), "rb") as source_handle,
                    os.fdopen(os.dup(snapshot_fd), "wb") as snapshot_handle,
                ):
                    for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                        snapshot_handle.write(chunk)
                    snapshot_handle.flush()
                    os.fsync(snapshot_handle.fileno())

                after = os.fstat(source_fd)
                try:
                    current = asset.path.lstat()
                    snapshot_current = snapshot_path.lstat()
                except OSError as exc:
                    raise ScannerWorkflowError(
                        f"preserved scanner source changed while snapshotting: {asset.asset_id}"
                    ) from exc
                snapshot_stat = os.fstat(snapshot_fd)
                if (
                    _stat_identity(before) != _stat_identity(after)
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_dev != after.st_dev
                    or current.st_ino != after.st_ino
                    or _stat_identity(current) != _stat_identity(after)
                    or not stat.S_ISREG(snapshot_stat.st_mode)
                    or not stat.S_ISREG(snapshot_current.st_mode)
                    or snapshot_stat.st_dev != snapshot_current.st_dev
                    or snapshot_stat.st_ino != snapshot_current.st_ino
                    or snapshot_stat.st_size != after.st_size
                    or stat.S_IMODE(snapshot_stat.st_mode) != 0o400
                    or not secrets.compare_digest(
                        digest.hexdigest(),
                        expected_sha256,
                    )
                ):
                    raise ScannerWorkflowError(
                        f"preserved scanner source changed while snapshotting: {asset.asset_id}"
                    )
                bound_paths.append(snapshot_path)
            finally:
                if snapshot_fd >= 0:
                    os.close(snapshot_fd)
                os.close(source_fd)

        yield bound_paths


class _Img2PdfBuilder:
    name = "img2pdf"

    def version(self) -> str:
        try:
            return importlib.metadata.version("img2pdf")
        except importlib.metadata.PackageNotFoundError as exc:
            raise ImageDependencyError(
                "scanner PDF export requires img2pdf; install Digitalisierer scanner dependencies"
            ) from exc

    def __call__(self, images: list[Path], output: Path) -> None:
        try:
            import img2pdf  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImageDependencyError(
                "scanner PDF export requires img2pdf; install Digitalisierer scanner dependencies"
            ) from exc
        with output.open("xb") as handle:
            img2pdf.convert([str(path) for path in images], outputstream=handle)
            handle.flush()
            os.fsync(handle.fileno())


_default_pdf_builder: ImageToPdf = _Img2PdfBuilder()


def _pdf_builder_provenance(pdf_builder: ImageToPdf) -> dict[str, str]:
    name = getattr(pdf_builder, "name", None)
    version_method = getattr(pdf_builder, "version", None)
    if not isinstance(name, str) or not name or not callable(version_method):
        raise ScannerWorkflowError(
            "PDF builder must expose stable name/version provenance"
        )
    version = version_method()
    if not isinstance(version, str) or not version:
        raise ScannerWorkflowError(
            "PDF builder must expose stable name/version provenance"
        )
    return {"adapter": name, "version": version}


def _ocr_provenance(ocr_backend: OCRBackend, language: str) -> dict[str, str]:
    name = getattr(ocr_backend, "name", None)
    version_method = getattr(ocr_backend, "version", None)
    if not isinstance(name, str) or not name or not callable(version_method):
        raise ScannerWorkflowError(
            "OCR backend must expose stable name/version provenance"
        )
    version = version_method()
    if not isinstance(version, str) or not version:
        raise ScannerWorkflowError(
            "OCR backend must expose stable name/version provenance"
        )
    return {"adapter": name, "language": language, "version": version}


def _canonical_json_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


_RENAME_NOREPLACE = 1


def _fsync_regular_file(path: Path) -> None:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as exc:
        raise ScannerWorkflowError(
            f"scanner export artifact cannot be opened safely: {path.name}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ScannerWorkflowError(
                f"scanner export artifact is not a regular file: {path.name}"
            )
        os.fsync(fd)
        after = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as exc:
            raise ScannerWorkflowError(
                f"scanner export artifact changed while syncing: {path.name}"
            ) from exc
        if (
            _stat_identity(before) != _stat_identity(after)
            or not stat.S_ISREG(current.st_mode)
            or current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or _stat_identity(current) != _stat_identity(after)
        ):
            raise ScannerWorkflowError(
                f"scanner export artifact changed while syncing: {path.name}"
            )
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise ScannerWorkflowError(
            f"scanner export directory cannot be opened safely: {path}"
        ) from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_noreplace(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        if target.exists():
            raise FileExistsError(target)
        raise ScannerWorkflowError(
            "atomic no-replace directory publication requires Linux renameat2"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == 17:
        raise FileExistsError(target)
    raise OSError(error, os.strerror(error), target)


def _publish_verified_staging(
    staging: Path,
    final_dir: Path,
    expected: dict[str, tuple[str, tuple[int, int, int, int]]],
) -> None:
    opened: list[
        tuple[str, int, str, tuple[int, int, int, int]]
    ] = []
    published = False
    published_directory_identity: tuple[int, int] | None = None
    try:
        for name in sorted(expected):
            expected_sha256, expected_identity = expected[name]
            path = staging / name
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK,
                )
            except OSError as exc:
                raise ScannerWorkflowError(
                    f"scanner output changed during publication: {name}"
                ) from exc
            opened.append((name, descriptor, expected_sha256, expected_identity))
            before = os.fstat(descriptor)
            try:
                current = path.lstat()
            except OSError as exc:
                raise ScannerWorkflowError(
                    f"scanner output changed during publication: {name}"
                ) from exc
            digest = _descriptor_sha256(descriptor)
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or _stat_identity(before) != _stat_identity(after)
                or _stat_identity(after) != expected_identity
                or not stat.S_ISREG(current.st_mode)
                or current.st_dev != after.st_dev
                or current.st_ino != after.st_ino
                or _stat_identity(current) != _stat_identity(after)
                or not secrets.compare_digest(digest, expected_sha256)
            ):
                raise ScannerWorkflowError(
                    f"scanner output changed during publication: {name}"
                )

        try:
            staging_stat = staging.lstat()
        except OSError as exc:
            raise ScannerWorkflowError(
                "scanner export staging directory changed before publication"
            ) from exc
        if not stat.S_ISDIR(staging_stat.st_mode):
            raise ScannerWorkflowError(
                "scanner export staging directory changed before publication"
            )
        published_directory_identity = (staging_stat.st_dev, staging_stat.st_ino)

        _rename_noreplace(staging, final_dir)
        published = True
        try:
            published_stat = final_dir.lstat()
        except OSError as exc:
            raise ScannerWorkflowError(
                "published scanner export directory changed during publication"
            ) from exc
        if (
            not stat.S_ISDIR(published_stat.st_mode)
            or (published_stat.st_dev, published_stat.st_ino)
            != published_directory_identity
        ):
            raise ScannerWorkflowError(
                "published scanner export directory changed during publication"
            )

        for name, descriptor, expected_sha256, expected_identity in opened:
            final_path = final_dir / name
            after = os.fstat(descriptor)
            try:
                current = final_path.lstat()
            except OSError as exc:
                raise ScannerWorkflowError(
                    f"scanner output changed during publication: {name}"
                ) from exc
            digest = _descriptor_sha256(descriptor)
            final_stat = os.fstat(descriptor)
            if (
                _stat_identity(after) != _stat_identity(final_stat)
                or _stat_identity(final_stat) != expected_identity
                or not stat.S_ISREG(current.st_mode)
                or current.st_dev != final_stat.st_dev
                or current.st_ino != final_stat.st_ino
                or _stat_identity(current) != _stat_identity(final_stat)
                or not secrets.compare_digest(digest, expected_sha256)
            ):
                raise ScannerWorkflowError(
                    f"scanner output changed during publication: {name}"
                )

        # The directory-entry publication is not durable until its parent has
        # been synced. Keep that durability step inside this rollback boundary.
        _fsync_directory(final_dir.parent)
    except Exception as exc:
        if published:
            try:
                if staging.exists() or published_directory_identity is None:
                    raise ScannerWorkflowError(
                        "published scanner export cannot be rolled back safely"
                    )
                current_final = final_dir.lstat()
                if (
                    not stat.S_ISDIR(current_final.st_mode)
                    or (current_final.st_dev, current_final.st_ino)
                    != published_directory_identity
                ):
                    raise ScannerWorkflowError(
                        "published scanner export directory changed before rollback"
                    )
                os.rename(final_dir, staging)
                rolled_back = staging.lstat()
                if (
                    not stat.S_ISDIR(rolled_back.st_mode)
                    or (rolled_back.st_dev, rolled_back.st_ino)
                    != published_directory_identity
                ):
                    try:
                        _rename_noreplace(staging, final_dir)
                        _fsync_directory(staging.parent)
                    except Exception:
                        pass
                    raise ScannerWorkflowError(
                        "published scanner export directory changed during rollback"
                    )
                _fsync_directory(staging.parent)
            except Exception as rollback_exc:
                raise ScannerWorkflowError(
                    "failed to roll back scanner export after publication verification"
                ) from rollback_exc
        raise exc
    finally:
        for _name, descriptor, _sha256, _identity in opened:
            os.close(descriptor)


def finalize_scan_session(
    paths: ScanSessionPaths,
    ocr_backend: OCRBackend,
    *,
    language: str = "deu",
    pdf_builder: ImageToPdf = _default_pdf_builder,
) -> ScanExport:
    with _review_update_lock(paths):
        session, session_file_sha = _stable_json_snapshot(paths.session_file)
        _validate_session_identity(paths, session)
        review, review_file_sha = _stable_json_snapshot(paths.review_file)
        findings_payload, findings_file_sha = _stable_json_snapshot(
            paths.findings_file
        )
        _validate_findings_payload(findings_payload)
        processing = _processing_session_from_payload(paths, session, review)
        _validate_findings_asset_ids(processing, findings_payload)
    active = processing.ordered_assets()
    if not active:
        raise ScannerWorkflowError("scanner session has no included pages")

    verified_sources = _verify_preserved_sources(paths, session)
    review_sha = _canonical_json_digest(review)
    findings_sha = _canonical_json_digest(findings_payload)
    pdf_builder_provenance = _pdf_builder_provenance(pdf_builder)
    ocr_provenance = _ocr_provenance(ocr_backend, language)
    export_identity = hashlib.sha256(
        _json_text(
            {
                "layout": SCAN_EXPORT_LAYOUT,
                "review_sha256": review_sha,
                "findings_sha256": findings_sha,
                "pdf_builder": pdf_builder_provenance,
                "ocr": ocr_provenance,
                "active": [
                    {"asset_id": asset.asset_id, "sha256": asset.sha256}
                    for asset in active
                ],
            }
        ).encode("utf-8")
    ).hexdigest()
    export_id = f"export--{export_identity[:12]}"
    _validate_session_storage_directory(paths, paths.exports, "exports")
    final_dir = paths.exports / export_id
    if final_dir.exists():
        raise ScannerWorkflowError(f"scan export already exists: {final_dir}")
    staging = paths.exports / f".{export_id}.staging-{secrets.token_hex(8)}"
    staging.mkdir(mode=0o700)
    staging_stat = staging.lstat()
    staging_directory_identity = (staging_stat.st_dev, staging_stat.st_ino)
    try:
        master = staging / "master.pdf"
        searchable = staging / "searchable.pdf"
        text_file = staging / "text.txt"
        report = staging / "report.txt"
        manifest_path = staging / "manifest.json"

        with _verified_export_source_snapshots(active, staging) as export_sources:
            pdf_builder(export_sources, master)
        master_sha256, master_identity = _regular_file_snapshot(
            master,
            purpose="master PDF output",
        )
        ocr_backend.searchable_pdf(
            master,
            searchable,
            text_file,
            language=language,
        )
        searchable_sha256, searchable_identity = _regular_file_snapshot(
            searchable,
            purpose="searchable PDF output",
        )
        text_sha256, text_identity = _regular_file_snapshot(
            text_file,
            purpose="OCR text output",
        )

        raw_findings = findings_payload.get("findings")
        findings_count = len(raw_findings) if isinstance(raw_findings, list) else 0
        _atomic_write_text(
            report,
            "\n".join(
                [
                    "Digitalisierer Scanner Finalize Report",
                    "====================================",
                    f"Session: {paths.root}",
                    f"Included pages: {len(active)}",
                    f"Recorded source assets: {len(verified_sources)}",
                    f"Quality findings: {findings_count}",
                    f"OCR language: {language}",
                    "Preserved sources modified: NO",
                    "",
                    f"Master: {master.name}",
                    f"Searchable PDF: {searchable.name}",
                    f"Text: {text_file.name}",
                    f"Manifest: {manifest_path.name}",
                    "",
                ]
            ),
        )

        report_sha256, report_identity = _regular_file_snapshot(
            report,
            purpose="scanner report output",
        )
        output_hashes = {
            "master.pdf": master_sha256,
            "searchable.pdf": searchable_sha256,
            "text.txt": text_sha256,
            "report.txt": report_sha256,
        }
        artifact_states: dict[
            str,
            tuple[str, tuple[int, int, int, int]],
        ] = {
            "master.pdf": (master_sha256, master_identity),
            "searchable.pdf": (searchable_sha256, searchable_identity),
            "text.txt": (text_sha256, text_identity),
            "report.txt": (report_sha256, report_identity),
        }
        manifest: dict[str, object] = {
            "schema_version": 1,
            "kind": "digitalisierer.scan-manifest",
            "layout": SCAN_EXPORT_LAYOUT,
            "session_layout": SCAN_SESSION_LAYOUT,
            "project_id": session.get("project_id"),
            "session_id": session.get("session_id"),
            "export_id": export_id,
            "review_sha256": review_sha,
            "review": review,
            "findings_sha256": findings_sha,
            "pdf_builder": pdf_builder_provenance,
            "ocr": ocr_provenance,
            "sources": verified_sources,
            "active_order": [
                {"asset_id": asset.asset_id, "sha256": asset.sha256}
                for asset in active
            ],
            "findings": findings_payload,
            "output_hashes": output_hashes,
        }
        _atomic_write_text(manifest_path, _json_text(manifest))
        manifest_sha256, manifest_identity = _regular_file_snapshot(
            manifest_path,
            purpose="scanner manifest output",
        )
        artifact_states["manifest.json"] = (
            manifest_sha256,
            manifest_identity,
        )

        for artifact in (master, searchable, text_file, report, manifest_path):
            _fsync_regular_file(artifact)
        _fsync_directory(staging)

        # Re-verify preserved inputs after all processing and before publication.
        after_sources = _verify_preserved_sources(paths, session)
        if after_sources != verified_sources:
            raise ScannerWorkflowError(
                "preserved scanner sources changed while finalizing"
            )
        for name, (expected_sha256, expected_identity) in artifact_states.items():
            current_sha256, current_identity = _regular_file_snapshot(
                staging / name,
                purpose="scanner staged output",
            )
            if (
                current_identity != expected_identity
                or not secrets.compare_digest(current_sha256, expected_sha256)
            ):
                raise ScannerWorkflowError(
                    f"scanner output changed before publication: {name}"
                )

        # Commit the export against one serialized metadata boundary. A review or
        # observation update that began earlier is visible to the hash check; one
        # that begins later waits until the export directory has been published.
        with _review_update_lock(paths):
            _, current_session_sha = _stable_json_snapshot(paths.session_file)
            _, current_review_sha = _stable_json_snapshot(paths.review_file)
            _, current_findings_sha = _stable_json_snapshot(paths.findings_file)
            if (
                current_session_sha != session_file_sha
                or current_review_sha != review_file_sha
                or current_findings_sha != findings_file_sha
            ):
                raise ScannerWorkflowError(
                    "scanner session metadata changed while finalizing"
                )

            _publish_verified_staging(
                staging,
                final_dir,
                artifact_states,
            )
        staging = Path()
        return ScanExport(
            session_root=paths.root,
            export_dir=final_dir,
            output_hashes={
                **output_hashes,
                "manifest.json": manifest_sha256,
            },
        )
    except Exception:
        if staging != Path() and os.path.lexists(staging):
            try:
                current_staging = staging.lstat()
            except OSError:
                pass
            else:
                if (
                    stat.S_ISDIR(current_staging.st_mode)
                    and (current_staging.st_dev, current_staging.st_ino)
                    == staging_directory_identity
                ):
                    shutil.rmtree(staging, ignore_errors=True)
        raise