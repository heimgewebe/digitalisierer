from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import TypedDict

from . import __version__
from .heim_pc_asr import AsrAdapterError, HeimgewebeAsrBackend
from .ports import TranscriptionBackend
from .transcription import (
    TranscriptionWorkflowError,
    transcribe_and_export,
    transcribe_to_library,
)


CAPABILITY_TOOLS: dict[str, tuple[str, ...]] = {
    "media": ("ffmpeg", "ffprobe"),
    "ocr": ("ocrmypdf", "tesseract"),
    "capture-czur": ("xdotool", "v4l2-ctl"),
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


def _czur_app_path() -> Path:
    return Path.home() / ".local/opt/czur-scanner/CzurScanner"


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
        checks: dict[str, ToolCheck] = {name: _which(name) for name in tools}
        detail = ""

        if capability == "capture-czur":
            app_path = _czur_app_path()
            checks["czur-app"] = {
                "found": app_path.is_file() and os.access(app_path, os.X_OK),
                "path": str(app_path),
            }
            detail = "CZUR capture adapter prerequisites"

        capabilities[capability] = {
            "ready": all(item["found"] for item in checks.values()),
            "checks": checks,
            "detail": detail,
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

    args = parser.parse_args(argv)
    if args.command == "doctor":
        required = (
            tuple(args.required_capabilities)
            if args.required_capabilities
            else DEFAULT_REQUIRED_CAPABILITIES
        )
        return doctor(required)
    if args.command == "transcribe":
        source = args.source.expanduser()
        try:
            backend = _transcription_backend()
            if args.output_dir is not None:
                exported = transcribe_and_export(
                    source,
                    args.output_dir.expanduser(),
                    backend,
                )
            else:
                exported = transcribe_to_library(
                    source,
                    backend,
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
                    "output_dir": str(exported.output_dir),
                    "artifacts": [
                        {
                            "kind": artifact.kind,
                            "path": str(artifact.path),
                            "sha256": artifact.sha256,
                        }
                        for artifact in exported.artifacts
                    ],
                },
                indent=2,
                ensure_ascii=True,
            )
        )
        return 0
    return 2