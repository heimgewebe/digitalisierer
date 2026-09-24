from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from . import __version__


CAPABILITY_TOOLS = {
    "media": ("ffmpeg", "ffprobe"),
    "ocr": ("ocrmypdf", "tesseract"),
    "capture-czur": ("xdotool", "v4l2-ctl"),
}
CZUR_APP = Path.home() / ".local/opt/czur-scanner/CzurScanner"


def _which(name: str) -> dict[str, object]:
    path = shutil.which(name)
    return {"found": bool(path), "path": path}


def doctor() -> int:
    capabilities: dict[str, dict[str, object]] = {}

    for capability, tools in CAPABILITY_TOOLS.items():
        checks = {name: _which(name) for name in tools}
        capabilities[capability] = {
            "ready": all(item["found"] for item in checks.values()),
            "checks": checks,
        }

    czur = capabilities["capture-czur"]
    czur_checks = dict(czur["checks"])
    czur_checks["czur-app"] = {
        "found": CZUR_APP.is_file(),
        "path": str(CZUR_APP),
    }
    czur["checks"] = czur_checks
    czur["ready"] = all(item["found"] for item in czur_checks.values())

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
