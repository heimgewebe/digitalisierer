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
    sequence: int | None = None
    excluded: bool = False
    replacement_for: str | None = None


@dataclass(frozen=True, slots=True)
class QualityFinding:
    asset_id: str
    kind: str
    message: str
    confidence: float | None = None


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
    assets: list[MediaAsset] = field(default_factory=list)
    findings: list[QualityFinding] = field(default_factory=list)

    def active_assets(self) -> list[MediaAsset]:
        return [asset for asset in self.assets if not asset.excluded]

    def ordered_assets(self) -> list[MediaAsset]:
        return sorted(
            self.active_assets(),
            key=lambda asset: (
                asset.sequence is None,
                asset.sequence if asset.sequence is not None else 0,
                asset.asset_id,
            ),
        )
