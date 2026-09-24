from pathlib import Path

from digitalisierer.domain import (
    AssetRole,
    DigitizationProject,
    MediaAsset,
    MediaKind,
    ProcessingSession,
    SessionAsset,
)


def test_review_state_does_not_change_source_asset_identity() -> None:
    source = MediaAsset(
        "page-a",
        Path("page-a.jpg"),
        MediaKind.DOCUMENT_IMAGE,
        sha256="abc",
    )
    session = ProcessingSession(
        session_id="chapter",
        root=Path("/tmp/chapter"),
        items=[
            SessionAsset(source, sequence=1, included=False),
        ],
    )

    assert source.role is AssetRole.SOURCE
    assert source.sha256 == "abc"
    assert session.active_assets() == []


def test_ordered_items_keep_review_metadata_outside_assets() -> None:
    late = MediaAsset("late", Path("b.wav"), MediaKind.AUDIO)
    early = MediaAsset("early", Path("a.pdf"), MediaKind.PDF)
    derived = MediaAsset(
        "derived",
        Path("x.txt"),
        MediaKind.TEXT,
        role=AssetRole.DERIVED,
    )
    session = ProcessingSession(
        session_id="mixed",
        root=Path("/tmp/mixed"),
        items=[
            SessionAsset(late, sequence=20),
            SessionAsset(early, sequence=10),
            SessionAsset(derived),
        ],
    )

    assert [item.asset.asset_id for item in session.ordered_items()] == [
        "early",
        "late",
        "derived",
    ]


def test_project_groups_sessions_without_owning_engine_state() -> None:
    session = ProcessingSession("one", Path("/tmp/one"))
    project = DigitizationProject("project", Path("/tmp/project"), [session])

    assert project.sessions == [session]
