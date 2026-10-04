from __future__ import annotations

import os
from pathlib import Path
import re


DEFAULT_LIBRARY_DIRNAME = "Digitalisierer"
DEFAULT_INBOX_PROJECT_ID = "inbox"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


def default_library_root() -> Path:
    configured = os.environ.get("DIGITALISIERER_LIBRARY_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / DEFAULT_LIBRARY_DIRNAME


def validate_identifier(value: str, *, label: str) -> str:
    if value != value.strip() or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(
            f"{label} must match {_IDENTIFIER.pattern!r} and contain no path separators"
        )
    if value in {".", ".."}:
        raise ValueError(f"{label} must not be a relative path marker")
    return value


def session_root(
    project_id: str,
    session_id: str,
    library_root: Path | None = None,
) -> Path:
    project = validate_identifier(project_id, label="project_id")
    session = validate_identifier(session_id, label="session_id")
    root = (library_root or default_library_root()).expanduser()
    return root / "projects" / project / "sessions" / session
