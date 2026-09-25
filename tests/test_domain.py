from pathlib import Path

import pytest

from digitalisierer.domain import (
    AssetRole,
    DigitizationProject,
    MediaAsset,
    MediaKind,
    ProcessingSession,
    QualityFinding,
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
    assert [item.asset.asset_id for item in session.active_items()] == ["early", "late"]
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
                SessionAsset(replacement, replacement_for="original"),
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


def test_dangling_replacement_chain_fails_with_value_error_independent_of_order() -> None:
    first = MediaAsset("first", Path("first.jpg"), MediaKind.DOCUMENT_IMAGE)
    second = MediaAsset("second", Path("second.jpg"), MediaKind.DOCUMENT_IMAGE)

    with pytest.raises(ValueError, match="replacement_for must reference"):
        ProcessingSession(
            "dangling-chain",
            Path("/tmp/dangling-chain"),
            items=[
                SessionAsset(first, included=False, replacement_for="second"),
                SessionAsset(second, included=False, replacement_for="missing"),
            ],
        )


def test_replacement_without_sequence_inherits_replaced_position() -> None:
    original = MediaAsset("page-01", Path("page-01.jpg"), MediaKind.DOCUMENT_IMAGE)
    page_02 = MediaAsset("page-02", Path("page-02.jpg"), MediaKind.DOCUMENT_IMAGE)
    replacement = MediaAsset(
        "page-01-rescan",
        Path("page-01-rescan.jpg"),
        MediaKind.DOCUMENT_IMAGE,
    )
    session = ProcessingSession(
        "replacement",
        Path("/tmp/replacement"),
        items=[
            SessionAsset(original, sequence=1, included=False),
            SessionAsset(page_02, sequence=2),
            SessionAsset(replacement, replacement_for="page-01"),
        ],
    )

    assert [asset.asset_id for asset in session.ordered_assets()] == [
        "page-01-rescan",
        "page-02",
    ]


def test_duplicate_effective_sequence_is_rejected() -> None:
    original = MediaAsset("original", Path("original.jpg"), MediaKind.DOCUMENT_IMAGE)
    replacement = MediaAsset("replacement", Path("replacement.jpg"), MediaKind.DOCUMENT_IMAGE)
    conflict = MediaAsset("conflict", Path("conflict.jpg"), MediaKind.DOCUMENT_IMAGE)

    with pytest.raises(ValueError, match="duplicate effective sequence 1"):
        ProcessingSession(
            "duplicate-sequence",
            Path("/tmp/duplicate-sequence"),
            items=[
                SessionAsset(original, sequence=1, included=False),
                SessionAsset(replacement, replacement_for="original"),
                SessionAsset(conflict, sequence=1),
            ],
        )


def test_replacement_cycle_is_rejected_even_when_items_are_excluded() -> None:
    first = MediaAsset("first", Path("first.jpg"), MediaKind.DOCUMENT_IMAGE)
    second = MediaAsset("second", Path("second.jpg"), MediaKind.DOCUMENT_IMAGE)

    with pytest.raises(ValueError, match="replacement cycle"):
        ProcessingSession(
            "cycle",
            Path("/tmp/cycle"),
            items=[
                SessionAsset(first, included=False, replacement_for="second"),
                SessionAsset(second, included=False, replacement_for="first"),
            ],
        )


def test_quality_finding_can_be_asset_pair_or_session_wide() -> None:
    duplicate = QualityFinding(
        kind="near-duplicate",
        message="two pages look alike",
        asset_ids=("page-01", "page-02"),
        confidence=0.9,
    )
    gap = QualityFinding(
        kind="sequence-gap",
        message="expected sequence position is missing",
    )

    assert duplicate.asset_ids == ("page-01", "page-02")
    assert gap.asset_ids == ()


def test_quality_finding_rejects_duplicate_asset_references() -> None:
    with pytest.raises(ValueError, match="asset ids must be unique"):
        QualityFinding(
            kind="duplicate",
            message="invalid duplicate references",
            asset_ids=("page-01", "page-01"),
        )


def test_project_groups_sessions_without_owning_engine_state() -> None:
    session = ProcessingSession("one", Path("/tmp/one"))
    project = DigitizationProject("project", Path("/tmp/project"), [session])

    assert project.sessions == [session]