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
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
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


def _stable_hash(path: Path) -> tuple[str, os.stat_result]:
    before = path.stat()
    if not path.is_file():
        raise ScannerWorkflowError(f"scan source must be a regular file: {path}")
    if before.st_size <= 0 or before.st_size > MAX_SOURCE_IMAGE_BYTES:
        raise ScannerWorkflowError(
            f"scan source size is outside the supported boundary: {path}"
        )
    digest = _sha256_file(path)
    after = path.stat()
    if _stat_identity(before) != _stat_identity(after):
        raise ScannerWorkflowError(f"scan source changed while hashing: {path}")
    return digest, after


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


def _inspect_image(path: Path) -> dict[str, object]:
    Image, ImageStat = _pillow_modules()
    with Image.open(path) as image:
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


def _write_thumbnail(source: Path, target: Path) -> None:
    Image, _ = _pillow_modules()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with Image.open(source) as image:
            image.load()
            thumbnail = image.convert("RGB")
            thumbnail.thumbnail((720, 960))
            with temporary.open("xb") as handle:
                thumbnail.save(handle, format="JPEG", quality=82, optimize=True)
                handle.flush()
                os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _existing_regular_file_hash(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
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


def _copy_preserved(
    source: Path,
    target: Path,
    *,
    expected_sha256: str,
    expected_stat: os.stat_result,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_hash = _existing_regular_file_hash(target)
    if existing_hash is not None:
        if existing_hash != expected_sha256:
            raise ScannerWorkflowError(
                f"preserved source collision for {target.name}"
            )
        return
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        copied_sha = _sha256_file(temporary)
        after = source.stat()
        if (
            copied_sha != expected_sha256
            or _stat_identity(after) != _stat_identity(expected_stat)
        ):
            raise ScannerWorkflowError(
                f"scan source changed while preserving: {source}"
            )
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing_hash = _existing_regular_file_hash(target)
            if existing_hash is None or existing_hash != expected_sha256:
                raise ScannerWorkflowError(
                    f"preserved source collision for {target.name}"
                )
        temporary.unlink()
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
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        copied_sha = _sha256_file(temporary)
        after = source.stat()
        if (
            copied_sha != expected_sha256
            or _stat_identity(after) != _stat_identity(expected_stat)
        ):
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
    if not preserved.exists() or _sha256_file(preserved) != expected_sha256:
        _replace_preserved(
            source,
            preserved,
            expected_sha256=expected_sha256,
            expected_stat=expected_stat,
        )
    if _sha256_file(preserved) != expected_sha256:
        raise ScannerWorkflowError(f"preserved source hash mismatch for {asset_id}")

    thumbnail = thumbnails_root / f"{asset_id}.jpg"
    if thumbnail.is_symlink() or (thumbnail.exists() and not thumbnail.is_file()):
        raise ScannerWorkflowError(
            f"scan thumbnail must be the canonical regular file for {asset_id}"
        )
    thumbnail_sha256 = record.get("thumbnail_sha256")
    thumbnail_valid = (
        isinstance(thumbnail_sha256, str)
        and len(thumbnail_sha256) == 64
        and all(char in "0123456789abcdef" for char in thumbnail_sha256)
        and thumbnail.exists()
        and secrets.compare_digest(_sha256_file(thumbnail), thumbnail_sha256)
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
        _copy_preserved(
            source,
            target,
            expected_sha256=sha256,
            expected_stat=source_stat,
        )
        image = _inspect_image(target)
        thumbnail_path = paths.root / thumbnail_rel
        _write_thumbnail(target, thumbnail_path)
        thumbnail_sha256 = _sha256_file(thumbnail_path)
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

    def current_capture_signature() -> list[tuple[str, int, int]]:
        current_sources = image_files(source_root)
        current_selected = current_sources[start - 1 :]
        if limit is not None:
            current_selected = current_selected[:limit]
        return [
            (path.name, path.stat().st_size, path.stat().st_mtime_ns)
            for path in current_selected
        ]

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
    thumbnail_rollbacks: list[tuple[Path, Path | None]] = []
    try:
        for record, preserved, thumbnail in pending_thumbnail_repairs:
            backup: Path | None = None
            if thumbnail.exists():
                backup_candidate = thumbnail.with_name(
                    f".{thumbnail.name}.{secrets.token_hex(8)}.rollback"
                )
                os.link(
                    thumbnail,
                    backup_candidate,
                    follow_symlinks=False,
                )
                if backup_candidate.is_symlink() or not backup_candidate.is_file():
                    backup_candidate.unlink(missing_ok=True)
                    raise ScannerWorkflowError(
                        f"scan thumbnail changed while preparing repair: {thumbnail.name}"
                    )
                backup = backup_candidate
            thumbnail_rollbacks.append((thumbnail, backup))
            if backup is not None:
                directory_fd = os.open(
                    thumbnail.parent,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

            _write_thumbnail(preserved, thumbnail)
            record["thumbnail_sha256"] = _sha256_file(thumbnail)

        # Dependent metadata must not become durable before the artifact
        # directory entries it references. New imports publish both preserved
        # sources and thumbnails; repairs can publish a replacement thumbnail.
        if imported:
            _fsync_directory(paths.sources)
        if imported or pending_thumbnail_repairs:
            _fsync_directory(paths.thumbnails)

        # review/findings depend on the prospective session.json asset set.
        # Keep repaired thumbnails and the final session commit in the same
        # rollback boundary so any reported observation failure restores the
        # previous filesystem and metadata snapshot.
        _atomic_write_text(paths.review_file, _json_text(review))
        _atomic_write_text(paths.findings_file, findings_text)
        if current_capture_signature() != before_signature:
            raise ScannerWorkflowError(
                "capture folder changed while Digitalisierer observed it"
            )
        _atomic_write_text(paths.session_file, _json_text(session))
    except Exception:
        rollback_error: Exception | None = None
        for thumbnail, backup in reversed(thumbnail_rollbacks):
            try:
                if backup is None:
                    thumbnail.unlink(missing_ok=True)
                else:
                    os.replace(backup, thumbnail)
                directory_fd = os.open(
                    thumbnail.parent,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception as exc:
                if rollback_error is None:
                    rollback_error = exc
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
        if rollback_error is not None:
            raise ScannerWorkflowError(
                "failed to restore scanner observation after failure"
            ) from rollback_error
        raise
    else:
        for thumbnail, backup in thumbnail_rollbacks:
            if backup is None:
                continue
            try:
                backup.unlink()
                directory_fd = os.open(
                    thumbnail.parent,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The canonical thumbnail and metadata are already committed.
                # Backup cleanup must not turn that commit into a false failure.
                pass
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
        candidate = (paths.root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScannerWorkflowError(
            f"preserved scanner source path is invalid: {relative}"
        ) from exc
    if not sources_root.is_dir() or sources_root.parent != root:
        raise ScannerWorkflowError("scanner sources directory escaped session root")
    try:
        candidate.relative_to(sources_root)
    except ValueError as exc:
        raise ScannerWorkflowError(
            f"preserved scanner source path is outside session sources: {relative}"
        ) from exc
    if (
        suffix not in IMAGE_SUFFIXES
        or relative_path != expected_relative
        or candidate != sources_root / f"{asset_id}{suffix}"
    ):
        raise ScannerWorkflowError(
            f"preserved scanner source path is not canonical for {asset_id}"
        )
    if not candidate.is_file():
        raise ScannerWorkflowError(
            f"preserved scanner source must be a regular file: {relative}"
        )
    return candidate


def _processing_session_from_payload(
    paths: ScanSessionPaths,
    session: dict[str, Any],
    review: dict[str, Any],
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
        asset_id = raw.get("asset_id")
        sha256 = raw.get("sha256")
        preserved = raw.get("preserved_path")
        if (
            not isinstance(asset_id, str)
            or not isinstance(sha256, str)
            or not isinstance(preserved, str)
        ):
            raise ScannerWorkflowError("scan asset identity is incomplete")
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
                    path=_preserved_source_path(paths, asset_id, preserved),
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
]:
    session, _ = _stable_json_snapshot(paths.session_file)
    review, _ = _stable_json_snapshot(paths.review_file)
    findings, _ = _stable_json_snapshot(paths.findings_file)
    _validate_findings_payload(findings)
    processing = _processing_session_from_payload(paths, session, review)
    _validate_findings_asset_ids(processing, findings)
    return session, review, findings, processing


def load_review_state(
    paths: ScanSessionPaths,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    ProcessingSession,
]:
    with _review_update_lock(paths):
        return _load_review_state_unlocked(paths)


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
    _, review, _, _ = _load_review_state_unlocked(paths)
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

    old_text = paths.review_file.read_text(encoding="utf-8")
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
        if _sha256_file(path) != expected:
            raise ScannerWorkflowError(
                f"preserved scanner source hash mismatch: {asset_id}"
            )
        verified.append(
            {
                "asset_id": asset_id,
                "sha256": expected,
                "bytes": path.stat().st_size,
                "path": relative,
            }
        )
    return verified


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
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ScannerWorkflowError(
            f"scanner export artifact cannot be opened safely: {path.name}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ScannerWorkflowError(
                f"scanner export artifact is not a regular file: {path.name}"
            )
        os.fsync(fd)
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
    try:
        master = staging / "master.pdf"
        searchable = staging / "searchable.pdf"
        text_file = staging / "text.txt"
        report = staging / "report.txt"
        manifest_path = staging / "manifest.json"

        pdf_builder([asset.path for asset in active], master)
        if not master.is_file():
            raise ScannerWorkflowError("master PDF builder produced no output")
        ocr_backend.searchable_pdf(
            master,
            searchable,
            text_file,
            language=language,
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

        output_hashes = {
            name: _sha256_file(staging / name)
            for name in ("master.pdf", "searchable.pdf", "text.txt", "report.txt")
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

        for artifact in (master, searchable, text_file, report, manifest_path):
            _fsync_regular_file(artifact)
        _fsync_directory(staging)

        # Re-verify preserved inputs after all processing and before publication.
        after_sources = _verify_preserved_sources(paths, session)
        if after_sources != verified_sources:
            raise ScannerWorkflowError(
                "preserved scanner sources changed while finalizing"
            )
        for name, expected in output_hashes.items():
            if _sha256_file(staging / name) != expected:
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

            _rename_noreplace(staging, final_dir)
            _fsync_directory(paths.exports)
        staging = Path()
        return ScanExport(
            session_root=paths.root,
            export_dir=final_dir,
            output_hashes={
                **output_hashes,
                "manifest.json": _sha256_file(final_dir / "manifest.json"),
            },
        )
    except Exception:
        if staging != Path() and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise