from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, TypedDict, cast

from . import __version__
from .czur import CzurAdapterError, CzurCaptureBackend
from .heim_pc_asr import AsrAdapterError, HeimgewebeAsrBackend
from .ocr import OcrAdapterError, OcrmypdfBackend
from .ports import TranscriptionBackend
from .review import serve_review
from .scanner import (
    ScannerWorkflowError,
    create_or_resume_scan_session,
    finalize_scan_session,
    load_processing_session,
    observe_scan_folder,
    scan_session_paths,
    update_review_item,
)
from .transcription import (
    TranscriptionWorkflowError,
    transcribe_and_export,
    transcribe_to_library,
)


CAPABILITY_TOOLS: dict[str, tuple[str, ...]] = {
    "media": ("ffmpeg", "ffprobe"),
    "ocr": ("ocrmypdf", "tesseract"),
    "capture-czur": (),
}
CAPABILITY_NAMES = (*CAPABILITY_TOOLS, "transcription")
DEFAULT_REQUIRED_CAPABILITIES = ("media", "ocr")


class ToolCheck(TypedDict):
    found: bool
    path: str | None


class CapabilityStatus(TypedDict):
    ready: bool | None
    checks: dict[str, ToolCheck]
    detail: str


def _which(name: str) -> ToolCheck:
    path = shutil.which(name)
    return {"found": path is not None, "path": path}


def _czur_capture_capability() -> CapabilityStatus:
    backend = CzurCaptureBackend()
    status = backend.status()
    return {
        "ready": status.ready,
        "checks": {
            "xdotool": _which(backend.xdotool),
            "czur-launcher": {
                "found": backend.launcher.is_file()
                and os.access(backend.launcher, os.X_OK),
                "path": str(backend.launcher),
            },
            "czur-config": {
                "found": backend.config_path.is_file(),
                "path": str(backend.config_path),
            },
        },
        "detail": status.detail,
    }


def _transcription_backend() -> TranscriptionBackend:
    return HeimgewebeAsrBackend()


def _transcription_capability() -> CapabilityStatus:
    status = HeimgewebeAsrBackend().status()
    return {
        "ready": status.ready,
        "checks": {
            "heimgewebe-asr": {
                "found": status.ready,
                "path": status.entrypoint,
            }
        },
        "detail": status.detail,
    }


def _capabilities(required: tuple[str, ...]) -> dict[str, CapabilityStatus]:
    capabilities: dict[str, CapabilityStatus] = {}

    for capability, tools in CAPABILITY_TOOLS.items():
        if capability == "capture-czur":
            capabilities[capability] = _czur_capture_capability()
            continue

        checks: dict[str, ToolCheck] = {name: _which(name) for name in tools}
        capabilities[capability] = {
            "ready": all(item["found"] for item in checks.values()),
            "checks": checks,
            "detail": "",
        }

    capabilities["transcription"] = (
        _transcription_capability()
        if "transcription" in required
        else {
            "ready": None,
            "checks": {},
            "detail": "not checked; use --require transcription for a live ASR readiness probe",
        }
    )
    return capabilities


def doctor(required: tuple[str, ...] = DEFAULT_REQUIRED_CAPABILITIES) -> int:
    unknown = sorted(set(required).difference(CAPABILITY_NAMES))
    if unknown:
        raise ValueError(f"unknown required capabilities: {', '.join(unknown)}")
    capabilities = _capabilities(required)

    ready = all(capabilities[name]["ready"] is True for name in required)
    result = {
        "digitalisierer": __version__,
        "ready": ready,
        "required_capabilities": list(required),
        "capabilities": capabilities,
    }
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if ready else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digitalisierer")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser(
        "doctor",
        help="report local digitization capability readiness",
    )
    doctor_parser.add_argument(
        "--require",
        action="append",
        choices=CAPABILITY_NAMES,
        dest="required_capabilities",
        help=(
            "require one capability for a successful exit; repeat for multiple. "
            "Default: media + ocr"
        ),
    )

    transcribe_parser = sub.add_parser(
        "transcribe",
        help="transcribe one local media file through the canonical heimgewebe/asr authority",
    )
    transcribe_parser.add_argument("source", type=Path)
    storage_group = transcribe_parser.add_mutually_exclusive_group()
    storage_group.add_argument(
        "--output-dir",
        type=Path,
        help="explicit bundle directory; must not already exist",
    )
    storage_group.add_argument(
        "--library-root",
        type=Path,
        help=(
            "Digitalisierer library root; default: $DIGITALISIERER_LIBRARY_ROOT "
            "or ~/Digitalisierer"
        ),
    )


    scan_parser = sub.add_parser(
        "scan",
        help="create, review and finalize one scanner session in the canonical library",
    )
    scan_sub = scan_parser.add_subparsers(dest="scan_command", required=True)

    def add_scan_identity(command: argparse.ArgumentParser) -> None:
        command.add_argument("--project", required=True)
        command.add_argument("--session", required=True)
        command.add_argument(
            "--library-root",
            type=Path,
            help=(
                "Digitalisierer library root; default: $DIGITALISIERER_LIBRARY_ROOT "
                "or ~/Digitalisierer"
            ),
        )

    scan_init = scan_sub.add_parser("init", help="create or resume a scan session")
    add_scan_identity(scan_init)

    scan_start = scan_sub.add_parser(
        "start",
        help="apply the Curved Books preset and launch/focus the official CZUR app",
    )
    add_scan_identity(scan_start)

    scan_observe = scan_sub.add_parser(
        "observe",
        help="preserve newly captured CZUR pages and generate thumbnails/findings",
    )
    add_scan_identity(scan_observe)
    scan_observe.add_argument(
        "--source",
        type=Path,
        help="capture folder; default: newest CZUR folder with images",
    )
    scan_observe.add_argument("--start", type=int, default=1)
    scan_observe.add_argument("--limit", type=int)

    scan_review_set = scan_sub.add_parser(
        "review-set",
        help="apply one explicit include/order/replacement review decision",
    )
    add_scan_identity(scan_review_set)
    scan_review_set.add_argument("asset_id")
    inclusion = scan_review_set.add_mutually_exclusive_group()
    inclusion.add_argument("--include", action="store_true")
    inclusion.add_argument("--exclude", action="store_true")
    sequence = scan_review_set.add_mutually_exclusive_group()
    sequence.add_argument("--sequence", type=int)
    sequence.add_argument("--clear-sequence", action="store_true")
    replacement = scan_review_set.add_mutually_exclusive_group()
    replacement.add_argument("--replacement-for")
    replacement.add_argument("--clear-replacement", action="store_true")

    scan_review = scan_sub.add_parser(
        "review",
        help="serve the large local scan review UI on loopback only",
    )
    add_scan_identity(scan_review)
    scan_review.add_argument("--host", default="127.0.0.1")
    scan_review.add_argument("--port", type=int, default=8765)

    scan_finalize = scan_sub.add_parser(
        "finalize",
        help="export reviewed pages to master PDF, OCR PDF, text, report and manifest",
    )
    add_scan_identity(scan_finalize)
    scan_finalize.add_argument("--lang", default=os.environ.get("CZUR_OCR_LANG", "deu"))
    scan_finalize.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="OCR worker count; default: $CZUR_OCR_JOBS or 4",
    )

    args = parser.parse_args(argv)
    if args.command == "doctor":
        required = (
            tuple(args.required_capabilities)
            if args.required_capabilities
            else DEFAULT_REQUIRED_CAPABILITIES
        )
        return doctor(required)
    if args.command == "scan":
        paths = scan_session_paths(
            args.project,
            args.session,
            args.library_root.expanduser() if args.library_root is not None else None,
        )
        try:
            if args.scan_command == "init":
                paths = create_or_resume_scan_session(
                    args.project,
                    args.session,
                    args.library_root.expanduser()
                    if args.library_root is not None
                    else None,
                )
                print(
                    json.dumps(
                        {"session_root": str(paths.root), "state": "ready"},
                        indent=2,
                        ensure_ascii=True,
                    )
                )
                return 0

            if args.scan_command == "start":
                paths = create_or_resume_scan_session(
                    args.project,
                    args.session,
                    args.library_root.expanduser()
                    if args.library_root is not None
                    else None,
                )
                capture_backend = CzurCaptureBackend()
                capture_backend.start(paths.root / "capture-observation")
                status = capture_backend.status()
                print(
                    json.dumps(
                        {
                            "session_root": str(paths.root),
                            "capture_backend": capture_backend.name,
                            "ready": status.ready,
                            "connected": status.connected,
                            "detail": status.detail,
                        },
                        indent=2,
                        ensure_ascii=True,
                    )
                )
                return 0

            if args.scan_command == "observe":
                paths = create_or_resume_scan_session(
                    args.project,
                    args.session,
                    args.library_root.expanduser()
                    if args.library_root is not None
                    else None,
                )
                capture_backend = CzurCaptureBackend()
                source = (
                    args.source.expanduser().resolve(strict=True)
                    if args.source is not None
                    else capture_backend.newest_scan_folder()
                )
                observed = observe_scan_folder(
                    paths,
                    source,
                    start=args.start,
                    limit=args.limit,
                )
                print(
                    json.dumps(
                        {
                            "session_root": str(observed.session_root),
                            "source_folder": str(source),
                            "imported_asset_ids": list(observed.imported_asset_ids),
                            "skipped_asset_ids": list(observed.skipped_asset_ids),
                            "findings": [
                                {
                                    "kind": finding.kind,
                                    "message": finding.message,
                                    "asset_ids": list(finding.asset_ids),
                                    "evidence": list(finding.evidence),
                                }
                                for finding in observed.findings
                            ],
                        },
                        indent=2,
                        ensure_ascii=True,
                    )
                )
                return 0

            if args.scan_command == "review-set":
                kwargs: dict[str, object] = {}
                if args.include:
                    kwargs["included"] = True
                elif args.exclude:
                    kwargs["included"] = False
                if args.sequence is not None:
                    kwargs["sequence"] = args.sequence
                elif args.clear_sequence:
                    kwargs["sequence"] = None
                if args.replacement_for is not None:
                    kwargs["replacement_for"] = args.replacement_for
                elif args.clear_replacement:
                    kwargs["replacement_for"] = None
                if not kwargs:
                    raise ValueError("review-set requires at least one review change")
                processing = update_review_item(
                    paths,
                    args.asset_id,
                    **cast(Any, kwargs),
                )
                print(
                    json.dumps(
                        {
                            "session_root": str(paths.root),
                            "active_order": [
                                asset.asset_id
                                for asset in processing.ordered_assets()
                            ],
                        },
                        indent=2,
                        ensure_ascii=True,
                    )
                )
                return 0

            if args.scan_command == "review":
                load_processing_session(paths)
                serve_review(paths, host=args.host, port=args.port)
                return 0

            if args.scan_command == "finalize":
                jobs = (
                    args.jobs
                    if args.jobs is not None
                    else int(os.environ.get("CZUR_OCR_JOBS", "4"))
                )
                ocr_backend = OcrmypdfBackend(jobs=jobs)
                scan_export = finalize_scan_session(
                    paths,
                    ocr_backend,
                    language=args.lang,
                )
                print(
                    json.dumps(
                        {
                            "session_root": str(scan_export.session_root),
                            "export_dir": str(scan_export.export_dir),
                            "output_hashes": scan_export.output_hashes,
                        },
                        indent=2,
                        ensure_ascii=True,
                    )
                )
                return 0
        except (
            CzurAdapterError,
            OcrAdapterError,
            ScannerWorkflowError,
            ValueError,
            OSError,
        ) as exc:
            print(f"scan failed: {exc}", file=sys.stderr)
            return 1
        return 2

    if args.command == "transcribe":
        source = args.source.expanduser()
        try:
            transcription_backend = _transcription_backend()
            if args.output_dir is not None:
                transcription_export = transcribe_and_export(
                    source,
                    args.output_dir.expanduser(),
                    transcription_backend,
                )
            else:
                transcription_export = transcribe_to_library(
                    source,
                    transcription_backend,
                    (
                        args.library_root.expanduser()
                        if args.library_root is not None
                        else None
                    ),
                )
        except (AsrAdapterError, TranscriptionWorkflowError, OSError) as exc:
            print(f"transcription failed: {exc}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "output_dir": str(transcription_export.output_dir),
                    "artifacts": [
                        {
                            "kind": artifact.kind,
                            "path": str(artifact.path),
                            "sha256": artifact.sha256,
                        }
                        for artifact in transcription_export.artifacts
                    ],
                },
                indent=2,
                ensure_ascii=True,
            )
        )
        return 0
    return 2