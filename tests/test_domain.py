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
    first = MediaAsset(
        "page-a",
        Path("page-a.jpg"),
        MediaKind.DOCUMENT_IMAGE,
        sha256="abc",
    )
    excluded = MediaAsset("page-b", Path("page-b.jpg"), MediaKind.DOCUMENT_IMAGE)
    third = MediaAsset("page-c", Path("page-c.jpg"), MediaKind.DOCUMENT_IMAGE)
    session = ProcessingSession(
        session_id="chapter",
        root=Path("/tmp/chapter"),
        items=[
            SessionAsset(first, sequence=1),
            SessionAsset(excluded, sequence=2, included=False),
            SessionAsset(third, sequence=3),
        ],
    )

    assert first.role is AssetRole.SOURCE
    assert first.sha256 == "abc"
    assert [asset.asset_id for asset in session.active_assets()] == ["page-a", "page-c"]
    assert excluded.asset_id == "page-b"


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
