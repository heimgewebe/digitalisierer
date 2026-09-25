from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MediaKind(str, Enum):
    DOCUMENT_IMAGE = "document-image"
    IMAGE = "image"
    PDF = "pdf"
    AUDIO = "audio"
    VIDEO = "video"
    TEXT = "text"
    UNKNOWN = "unknown"


class AssetRole(str, Enum):
    SOURCE = "source"
    DERIVED = "derived"
    EXPORT = "export"


class SessionStage(str, Enum):
    INGEST = "ingest"
    PROCESS = "process"
    REVIEW = "review"
    FINALIZED = "finalized"


@dataclass(frozen=True, slots=True)
class MediaAsset:
    asset_id: str
    path: Path
    kind: MediaKind
    role: AssetRole = AssetRole.SOURCE
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SessionAsset:
    asset: MediaAsset
    sequence: int | None = None
    included: bool = True
    # References MediaAsset.asset_id of the source/revision replaced in this session.
    replacement_for: str | None = None


@dataclass(frozen=True, slots=True)
class QualityFinding:
    asset_id: str
    kind: str
    message: str
    confidence: float | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    kind: str
    path: Path
    sha256: str


@dataclass(slots=True)
class ProcessingSession:
    session_id: str
    root: Path
    stage: SessionStage = SessionStage.INGEST
    items: list[SessionAsset] = field(default_factory=list)
    findings: list[QualityFinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate review-state invariants before processing or export."""
        item_by_id: dict[str, SessionAsset] = {}
        for item in self.items:
            asset_id = item.asset.asset_id
            if asset_id in item_by_id:
                raise ValueError(f"duplicate asset_id in session: {asset_id}")
            item_by_id[asset_id] = item

        included_replacements: dict[str, str] = {}
        for item in self.items:
            target_id = item.replacement_for
            if target_id is None:
                continue

            asset_id = item.asset.asset_id
            if target_id == asset_id:
                raise ValueError(f"asset cannot replace itself: {asset_id}")

            target = item_by_id.get(target_id)
            if target is None:
                raise ValueError(
                    f"replacement_for must reference an asset_id in the same session: {target_id}"
                )

            if not item.included:
                continue

            if target.included:
                raise ValueError(
                    f"included replacement {asset_id} requires replaced asset {target_id} "
                    "to be excluded"
                )

            previous = included_replacements.get(target_id)
            if previous is not None:
                raise ValueError(
                    f"multiple included replacements for {target_id}: {previous}, {asset_id}"
                )
            included_replacements[target_id] = asset_id

    def active_items(self) -> list[SessionAsset]:
        self.validate()
        return [item for item in self.items if item.included]

    def active_assets(self) -> list[MediaAsset]:
        """Return included assets in reviewed/export order."""
        return self.ordered_assets()

    def ordered_items(self) -> list[SessionAsset]:
        return sorted(
            self.active_items(),
            key=lambda item: (
                item.sequence is None,
                item.sequence if item.sequence is not None else 0,
                item.asset.asset_id,
            ),
        )

    def ordered_assets(self) -> list[MediaAsset]:
        return [item.asset for item in self.ordered_items()]


@dataclass(slots=True)
class DigitizationProject:
    project_id: str
    root: Path
    sessions: list[ProcessingSession] = field(default_factory=list)
