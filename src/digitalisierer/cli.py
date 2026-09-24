from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from . import __version__


CORE_TOOLS = ("ffmpeg", "ffprobe", "ocrmypdf", "tesseract")
CZUR_TOOLS = ("xdotool", "v4l2-ctl")
CZUR_APP = Path.home() / ".local/opt/czur-scanner/CzurScanner"


def _which(name: str) -> dict[str, object]:
    path = shutil.which(name)
    return {"found": bool(path), "path": path}


def doctor() -> int:
    core = {name: _which(name) for name in CORE_TOOLS}
    czur = {name: _which(name) for name in CZUR_TOOLS}
    czur["czur-app"] = {
        "found": CZUR_APP.is_file(),
        "path": str(CZUR_APP),
    }
    result = {
        "digitalisierer": __version__,
        "core_ready": all(item["found"] for item in core.values()),
        "checks": {
            "core": core,
            "capture": {"czur": czur},
        },
        "notes": {
            "transcription": "No transcription engine is mandatory yet; it is a pluggable backend."
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["core_ready"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digitalisierer")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check local digitization prerequisites")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return doctor()
    return 2
