from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
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
        asset_ids = finding.get("asset_ids")
        evidence = finding.get("evidence")
        confidence = finding.get("confidence")
        if (
            not isinstance(finding.get("kind"), str)
            or not isinstance(finding.get("message"), str)
            or not isinstance(asset_ids, list)
            or any(not isinstance(asset_id, str) for asset_id in asset_ids)
            or not isinstance(evidence, list)
            or any(not isinstance(item, str) for item in evidence)
            or (
                confidence is not None
                and (
                    isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
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
    lock_path = paths.root / REVIEW_LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
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


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScannerWorkflowError(f"scanner session file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
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


def _natural_key(path: Path) -> list[int | str]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def image_files(folder: Path) -> list[Path]:
    root = folder.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ScannerWorkflowError(f"scan source folder is not a directory: {root}")
    return sorted(
        [
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ],
        key=_natural_key,
    )


def create_or_resume_scan_session(
    project_id: str,
    session_id: str,
    library_root: Path | None = None,
) -> ScanSessionPaths:
    paths = scan_session_paths(project_id, session_id, library_root)
    paths.root.parent.mkdir(parents=True, exist_ok=True)
    try:
        paths.root.mkdir(mode=0o700)
    except FileExistsError:
        if not paths.root.is_dir():
            raise ScannerWorkflowError("scan session root is not a directory")
        if not paths.session_file.exists() and any(paths.root.iterdir()):
            raise ScannerWorkflowError(
                "scan session root is already occupied by non-scanner content"
            )

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

    for directory in (paths.sources, paths.thumbnails, paths.exports):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

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


def _copy_preserved(
    source: Path,
    target: Path,
    *,
    expected_sha256: str,
    expected_stat: os.stat_result,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _sha256_file(target) != expected_sha256:
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
            if _sha256_file(target) != expected_sha256:
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

    for index, first in enumerate(assets):
        first_image = first.get("image")
        if not isinstance(first_image, dict):
            continue
        first_hash = str(first_image["average_hash"])
        for second in assets[index + 1 :]:
            second_image = second.get("image")
            if not isinstance(second_image, dict):
                continue
            second_hash = str(second_image["average_hash"])
            distance = (
                int(first_hash, 16) ^ int(second_hash, 16)
            ).bit_count()
            if distance <= 20:
                findings.append(
                    QualityFinding(
                        kind="near-duplicate",
                        message="two pages have very similar visual hashes",
                        asset_ids=(
                            str(first["asset_id"]),
                            str(second["asset_id"]),
                        ),
                        evidence=(f"average_hash_distance={distance}",),
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
    imported: list[str] = []
    skipped: list[str] = []

    review = _load_json(paths.review_file)
    _validate_review_payload(review)
    review_items = review.get("items")
    if not isinstance(review_items, dict):
        raise ScannerWorkflowError("scan review items must be an object")

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
        asset_id = _asset_id(source.name, sha256)
        suffix = source.suffix.lower()
        preserved_rel = f"sources/{asset_id}{suffix}"
        thumbnail_rel = f"thumbnails/{asset_id}.jpg"
        existing = known_by_id.get(asset_id)
        if existing is not None:
            if existing.get("sha256") != sha256:
                raise ScannerWorkflowError(
                    f"asset identity collision for {asset_id}"
                )
            if not isinstance(review_items.get(asset_id), dict):
                review_items[asset_id] = {
                    "sequence": next_sequence,
                    "included": True,
                    "replacement_for": None,
                }
                next_sequence += 1
            skipped.append(asset_id)
            continue

        target = paths.root / preserved_rel
        _copy_preserved(
            source,
            target,
            expected_sha256=sha256,
            expected_stat=source_stat,
        )
        image = _inspect_image(target)
        _write_thumbnail(target, paths.root / thumbnail_rel)
        record: dict[str, Any] = {
            "asset_id": asset_id,
            "source_name": source.name,
            "capture_source": str(source),
            "sha256": sha256,
            "bytes": source_stat.st_size,
            "preserved_path": preserved_rel,
            "thumbnail_path": thumbnail_rel,
            "image": image,
        }
        assets.append(record)
        known_by_id[asset_id] = record
        review_items[asset_id] = {
            "sequence": next_sequence,
            "included": True,
            "replacement_for": None,
        }
        next_sequence += 1
        imported.append(asset_id)

    after_signature = [
        (path.name, path.stat().st_size, path.stat().st_mtime_ns)
        for path in selected
    ]
    if after_signature != before_signature:
        raise ScannerWorkflowError(
            "capture folder changed while Digitalisierer observed it"
        )

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
    # Publish dependent metadata before session.json, which is the commit point.
    # If either earlier write succeeds and a later write fails, the session remains
    # on the previous asset set; orphan review state then blocks processing/finalize
    # until a retry reconstructs the complete observation.
    _atomic_write_text(paths.review_file, _json_text(review))
    _atomic_write_text(
        paths.findings_file,
        _json_text(
            {
                "schema_version": 1,
                "kind": "digitalisierer.scan-findings",
                "findings": [_finding_payload(item) for item in findings],
            }
        ),
    )
    _atomic_write_text(paths.session_file, _json_text(session))
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
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except FileNotFoundError as exc:
        raise ScannerWorkflowError(f"scanner session file is missing: {path}") from exc
    if _stat_identity(before) != _stat_identity(after):
        raise ScannerWorkflowError(f"scanner session file changed while reading: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScannerWorkflowError(f"scanner session JSON is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ScannerWorkflowError(f"scanner session JSON must be an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _preserved_source_path(paths: ScanSessionPaths, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts:
        raise ScannerWorkflowError(
            f"preserved scanner source path is outside session sources: {relative}"
        )
    if relative_path.parts[0] != "sources" or ".." in relative_path.parts:
        raise ScannerWorkflowError(
            f"preserved scanner source path is outside session sources: {relative}"
        )
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
                    path=_preserved_source_path(paths, preserved),
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


def _update_review_item_unlocked(
    paths: ScanSessionPaths,
    asset_id: str,
    *,
    included: bool | None = None,
    sequence: int | None | object = _UNSET,
    replacement_for: str | None | object = _UNSET,
) -> ProcessingSession:
    _, review, _, _ = _load_review_state_unlocked(paths)
    items = review.get("items")
    if not isinstance(items, dict) or not isinstance(items.get(asset_id), dict):
        raise ScannerWorkflowError(f"unknown scanner asset: {asset_id}")
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
) -> ProcessingSession:
    with _review_update_lock(paths):
        return _update_review_item_unlocked(
            paths,
            asset_id,
            included=included,
            sequence=sequence,
            replacement_for=replacement_for,
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
        path = _preserved_source_path(paths, relative)
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
    session, session_file_sha = _stable_json_snapshot(paths.session_file)
    _validate_session_identity(paths, session)
    review, review_file_sha = _stable_json_snapshot(paths.review_file)
    findings_payload, findings_file_sha = _stable_json_snapshot(paths.findings_file)
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
        report.write_text(
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
            encoding="utf-8",
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
        manifest_path.write_text(_json_text(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)

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