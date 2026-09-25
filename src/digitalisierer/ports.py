from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .domain import MediaAsset, ProcessingSession, QualityFinding


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    connected: bool
    ready: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptArtifact:
    text_path: Path
    json_path: Path | None = None
    srt_path: Path | None = None
    vtt_path: Path | None = None


class CaptureBackend(Protocol):
    name: str

    def status(self) -> CaptureStatus:
        ...

    def start(self, output_dir: Path) -> None:
        ...

    def capture(self) -> None:
        ...

    def stop(self) -> None:
        ...


class OCRBackend(Protocol):
    name: str

    def searchable_pdf(
        self,
        master_pdf: Path,
        output_pdf: Path,
        sidecar_txt: Path,
        *,
        language: str,
    ) -> None:
        ...


class TranscriptionBackend(Protocol):
    name: str

    def transcribe(
        self,
        source: Path,
        output_dir: Path,
        *,
        language: str | None = None,
    ) -> TranscriptArtifact:
        ...


class MediaProbeBackend(Protocol):
    name: str

    def inspect(self, source: Path) -> dict[str, object]:
        ...


class AssetQualityAnalyzer(Protocol):
    """Analyzer for findings that can be established from one asset."""

    name: str

    def analyze_asset(self, asset: MediaAsset) -> list[QualityFinding]:
        ...


class SessionQualityAnalyzer(Protocol):
    """Analyzer for findings that require complete session context."""

    name: str

    def analyze_session(self, session: ProcessingSession) -> list[QualityFinding]:
        ...
