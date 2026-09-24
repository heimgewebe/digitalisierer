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

    def active_items(self) -> list[SessionAsset]:
        return [item for item in self.items if item.included]

    def active_assets(self) -> list[MediaAsset]:
        return [item.asset for item in self.active_items()]

    def ordered_items(self) -> list[SessionAsset]:
        return sorted(
            self.active_items(),
            key=lambda item: (
                item.sequence is None,
                item.sequence if item.sequence is not None else 0,
                item.asset.asset_id,
            ),
        )


@dataclass(slots=True)
class DigitizationProject:
    project_id: str
    root: Path
    sessions: list[ProcessingSession] = field(default_factory=list)
