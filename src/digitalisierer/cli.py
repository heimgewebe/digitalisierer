from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
from typing import TypedDict

from . import __version__


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
    ready: bool
    checks: dict[str, ToolCheck]
    detail: str


def _which(name: str) -> ToolCheck:
    path = shutil.which(name)
    return {"found": path is not None, "path": path}


def _czur_app_path() -> Path:
    return Path.home() / ".local/opt/czur-scanner/CzurScanner"


def _capabilities() -> dict[str, CapabilityStatus]:
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

    capabilities["transcription"] = {
        "ready": False,
        "checks": {},
        "detail": "Transcription adapter is not integrated yet.",
    }
    return capabilities


def doctor(required: tuple[str, ...] = DEFAULT_REQUIRED_CAPABILITIES) -> int:
    capabilities = _capabilities()
    unknown = sorted(set(required).difference(capabilities))
    if unknown:
        raise ValueError(f"unknown required capabilities: {', '.join(unknown)}")

    ready = all(capabilities[name]["ready"] for name in required)
    result = {
        "digitalisierer": __version__,
        "ready": ready,
        "required_capabilities": list(required),
        "capabilities": capabilities,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
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

    args = parser.parse_args(argv)
    if args.command == "doctor":
        required = (
            tuple(args.required_capabilities)
            if args.required_capabilities
            else DEFAULT_REQUIRED_CAPABILITIES
        )
        return doctor(required)
    return 2
