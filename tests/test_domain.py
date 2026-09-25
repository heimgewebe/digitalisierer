from pathlib import Path

import pytest

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


def test_active_assets_follow_review_sequence() -> None:
    late = MediaAsset("late", Path("late.jpg"), MediaKind.DOCUMENT_IMAGE)
    early = MediaAsset("early", Path("early.jpg"), MediaKind.DOCUMENT_IMAGE)
    session = ProcessingSession(
        "ordered",
        Path("/tmp/ordered"),
        items=[
            SessionAsset(late, sequence=20),
            SessionAsset(early, sequence=10),
        ],
    )

    assert [asset.asset_id for asset in session.active_assets()] == ["early", "late"]
    assert session.active_assets() == session.ordered_assets()


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


def test_replacement_requires_original_to_be_excluded() -> None:
    original = MediaAsset("original", Path("original.jpg"), MediaKind.DOCUMENT_IMAGE)
    replacement = MediaAsset(
        "replacement",
        Path("replacement.jpg"),
        MediaKind.DOCUMENT_IMAGE,
    )

    with pytest.raises(ValueError, match="requires replaced asset original to be excluded"):
        ProcessingSession(
            "invalid-replacement",
            Path("/tmp/invalid"),
            items=[
                SessionAsset(original, sequence=1),
                SessionAsset(replacement, sequence=1, replacement_for="original"),
            ],
        )


def test_replacement_reference_must_exist_and_asset_ids_are_unique() -> None:
    first = MediaAsset("same", Path("a.jpg"), MediaKind.DOCUMENT_IMAGE)
    second = MediaAsset("same", Path("b.jpg"), MediaKind.DOCUMENT_IMAGE)

    with pytest.raises(ValueError, match="duplicate asset_id"):
        ProcessingSession(
            "duplicate-ids",
            Path("/tmp/duplicate"),
            items=[SessionAsset(first), SessionAsset(second)],
        )

    replacement = MediaAsset("replacement", Path("r.jpg"), MediaKind.DOCUMENT_IMAGE)
    with pytest.raises(ValueError, match="replacement_for must reference"):
        ProcessingSession(
            "missing-target",
            Path("/tmp/missing"),
            items=[SessionAsset(replacement, replacement_for="missing")],
        )


def test_valid_replacement_exports_only_replacement() -> None:
    original = MediaAsset("original", Path("original.jpg"), MediaKind.DOCUMENT_IMAGE)
    replacement = MediaAsset(
        "replacement",
        Path("replacement.jpg"),
        MediaKind.DOCUMENT_IMAGE,
    )
    session = ProcessingSession(
        "replacement",
        Path("/tmp/replacement"),
        items=[
            SessionAsset(original, sequence=1, included=False),
            SessionAsset(
                replacement,
                sequence=1,
                replacement_for="original",
            ),
        ],
    )

    assert [asset.asset_id for asset in session.ordered_assets()] == ["replacement"]


def test_project_groups_sessions_without_owning_engine_state() -> None:
    session = ProcessingSession("one", Path("/tmp/one"))
    project = DigitizationProject("project", Path("/tmp/project"), [session])

    assert project.sessions == [session]
