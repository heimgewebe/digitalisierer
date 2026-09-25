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


class ToolCheck(TypedDict):
    found: bool
    path: str | None


class CapabilityStatus(TypedDict):
    ready: bool
    checks: dict[str, ToolCheck]


def _which(name: str) -> ToolCheck:
    path = shutil.which(name)
    return {"found": path is not None, "path": path}


def _czur_app_path() -> Path:
    return Path.home() / ".local/opt/czur-scanner/CzurScanner"


def doctor() -> int:
    capabilities: dict[str, CapabilityStatus] = {}

    for capability, tools in CAPABILITY_TOOLS.items():
        checks: dict[str, ToolCheck] = {name: _which(name) for name in tools}

        if capability == "capture-czur":
            app_path = _czur_app_path()
            checks["czur-app"] = {
                "found": app_path.is_file() and os.access(app_path, os.X_OK),
                "path": str(app_path),
            }

        capabilities[capability] = {
            "ready": all(item["found"] for item in checks.values()),
            "checks": checks,
        }

    result = {
        "digitalisierer": __version__,
        "capabilities": capabilities,
        "optional": {
            "transcription": {
                "ready": False,
                "detail": "No transcription adapter is selected yet.",
            }
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digitalisierer")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="report local digitization capability readiness")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return doctor()
    return 2
