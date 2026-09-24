from pathlib import Path

from digitalisierer.domain import AssetRole, MediaAsset, MediaKind, ProcessingSession


def test_active_assets_preserve_non_excluded_assets() -> None:
    session = ProcessingSession(
        session_id="test",
        root=Path("/tmp/test"),
        assets=[
            MediaAsset("a", Path("1.jpg"), MediaKind.DOCUMENT_IMAGE, sequence=1),
            MediaAsset("b", Path("2.jpg"), MediaKind.DOCUMENT_IMAGE, sequence=2, excluded=True),
            MediaAsset("c", Path("3.jpg"), MediaKind.DOCUMENT_IMAGE, sequence=3),
        ],
    )

    assert [asset.asset_id for asset in session.active_assets()] == ["a", "c"]


def test_ordered_assets_support_sequence_without_changing_sources() -> None:
    session = ProcessingSession(
        session_id="mixed",
        root=Path("/tmp/mixed"),
        assets=[
            MediaAsset("late", Path("b.wav"), MediaKind.AUDIO, sequence=20),
            MediaAsset("early", Path("a.pdf"), MediaKind.PDF, sequence=10),
            MediaAsset("derived", Path("x.txt"), MediaKind.UNKNOWN, role=AssetRole.DERIVED),
        ],
    )

    assert [asset.asset_id for asset in session.ordered_assets()] == [
        "early",
        "late",
        "derived",
    ]
