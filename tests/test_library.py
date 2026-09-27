from pathlib import Path

import pytest

from digitalisierer.library import default_library_root, session_root


def test_default_library_root_uses_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIGITALISIERER_LIBRARY_ROOT", str(tmp_path / "library"))
    assert default_library_root() == tmp_path / "library"


def test_session_root_uses_canonical_project_session_hierarchy(tmp_path: Path) -> None:
    assert session_root("book", "chapter-01", tmp_path) == (
        tmp_path / "projects" / "book" / "sessions" / "chapter-01"
    )


@pytest.mark.parametrize("value", ["../escape", "a/b", "", ".hidden", " space "])
def test_session_root_rejects_unsafe_identifiers(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        session_root(value, "session", tmp_path)
