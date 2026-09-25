from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
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
    kind: str
    message: str
    asset_ids: tuple[str, ...] = ()
    confidence: float | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("quality finding kind must not be empty")
        if not self.message.strip():
            raise ValueError("quality finding message must not be empty")
        if any(not asset_id.strip() for asset_id in self.asset_ids):
            raise ValueError("quality finding asset ids must not be empty")
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("quality finding asset ids must be unique")
        if self.confidence is not None and (
            isinstance(self.confidence, bool)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("quality finding confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    kind: str
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    text: str
    start: float | None = None
    end: float | None = None
    speaker: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.start is not None and (
            isinstance(self.start, bool)
            or not math.isfinite(self.start)
            or self.start < 0
        ):
            raise ValueError("transcript segment start must be a finite non-negative number")
        if self.end is not None and (
            isinstance(self.end, bool)
            or not math.isfinite(self.end)
            or self.end < 0
        ):
            raise ValueError("transcript segment end must be a finite non-negative number")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("transcript segment end must not precede start")
        if self.speaker is not None and not self.speaker.strip():
            raise ValueError("transcript segment speaker must not be empty")
        if self.confidence is not None and (
            isinstance(self.confidence, bool)
            or not math.isfinite(self.confidence)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("transcript segment confidence must be finite and between 0 and 1")


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str | None = None
    segments: tuple[TranscriptSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    transcript: Transcript
    provider: str
    engine: str
    model: str
    model_revision: str | None = None
    backend_version: str | None = None
    cloud_used: bool = False


@dataclass(slots=True)
class ProcessingSession:
    session_id: str
    root: Path
    stage: SessionStage = SessionStage.INGEST
    items: list[SessionAsset] = field(default_factory=list)
    findings: list[QualityFinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def _items_by_id(self) -> dict[str, SessionAsset]:
        item_by_id: dict[str, SessionAsset] = {}
        for item in self.items:
            asset_id = item.asset.asset_id
            if asset_id in item_by_id:
                raise ValueError(f"duplicate asset_id in session: {asset_id}")
            item_by_id[asset_id] = item
        return item_by_id

    @staticmethod
    def _effective_sequence(
        item: SessionAsset,
        item_by_id: dict[str, SessionAsset],
    ) -> int | None:
        current = item
        seen: set[str] = set()
        while True:
            asset_id = current.asset.asset_id
            if asset_id in seen:
                raise ValueError(f"replacement cycle in session: {asset_id}")
            seen.add(asset_id)

            if current.sequence is not None:
                return current.sequence
            if current.replacement_for is None:
                return None
            next_item = item_by_id.get(current.replacement_for)
            if next_item is None:
                raise ValueError(
                    "replacement_for must reference an asset_id in the same session: "
                    f"{current.replacement_for}"
                )
            current = next_item

    @staticmethod
    def _validate_replacement_graph(
        item_by_id: dict[str, SessionAsset],
    ) -> None:
        for item in item_by_id.values():
            target_id = item.replacement_for
            if target_id is None:
                continue
            asset_id = item.asset.asset_id
            if target_id == asset_id:
                raise ValueError(f"asset cannot replace itself: {asset_id}")
            if target_id not in item_by_id:
                raise ValueError(
                    "replacement_for must reference an asset_id in the same session: "
                    f"{target_id}"
                )

        for item in item_by_id.values():
            current = item
            seen: set[str] = set()
            while current.replacement_for is not None:
                asset_id = current.asset.asset_id
                if asset_id in seen:
                    raise ValueError(f"replacement cycle in session: {asset_id}")
                seen.add(asset_id)
                current = item_by_id[current.replacement_for]

    def validate(self) -> None:
        """Validate review-state invariants before processing or export."""
        item_by_id = self._items_by_id()
        self._validate_replacement_graph(item_by_id)
        included_replacements: dict[str, str] = {}

        for item in self.items:
            target_id = item.replacement_for
            if target_id is None:
                continue

            asset_id = item.asset.asset_id
            target = item_by_id[target_id]

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

        sequence_owner: dict[int, str] = {}
        for item in self.items:
            if not item.included:
                continue
            sequence = self._effective_sequence(item, item_by_id)
            if sequence is None:
                continue
            previous = sequence_owner.get(sequence)
            if previous is not None:
                raise ValueError(
                    f"duplicate effective sequence {sequence}: "
                    f"{previous}, {item.asset.asset_id}"
                )
            sequence_owner[sequence] = item.asset.asset_id

    def active_items(self) -> list[SessionAsset]:
        """Return included session items in reviewed/export order."""
        return self.ordered_items()

    def active_assets(self) -> list[MediaAsset]:
        """Return included assets in reviewed/export order."""
        return self.ordered_assets()

    def ordered_items(self) -> list[SessionAsset]:
        self.validate()
        item_by_id = self._items_by_id()

        def sort_key(item: SessionAsset) -> tuple[bool, int, str]:
            sequence = self._effective_sequence(item, item_by_id)
            return (
                sequence is None,
                sequence if sequence is not None else 0,
                item.asset.asset_id,
            )

        return sorted(
            (item for item in self.items if item.included),
            key=sort_key,
        )

    def ordered_assets(self) -> list[MediaAsset]:
        return [item.asset for item in self.ordered_items()]


@dataclass(slots=True)
class DigitizationProject:
    project_id: str
    root: Path
    sessions: list[ProcessingSession] = field(default_factory=list)