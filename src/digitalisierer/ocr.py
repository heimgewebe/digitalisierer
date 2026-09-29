from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import shutil
import subprocess


class OcrAdapterError(RuntimeError):
    """Raised when the local OCR adapter cannot produce its declared outputs."""


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


class OcrmypdfBackend:
    name = "ocrmypdf"

    def __init__(
        self,
        *,
        executable: str | None = None,
        jobs: int = 4,
        runner: Runner = _run,
    ) -> None:
        resolved = executable or shutil.which("ocrmypdf")
        if not resolved:
            raise OcrAdapterError("ocrmypdf executable is not available")
        if jobs < 1:
            raise ValueError("OCR jobs must be at least 1")
        self.executable = resolved
        self.jobs = jobs
        self._runner = runner

    def searchable_pdf(
        self,
        master_pdf: Path,
        output_pdf: Path,
        sidecar_txt: Path,
        *,
        language: str,
    ) -> None:
        if not master_pdf.is_file():
            raise OcrAdapterError(f"master PDF is missing: {master_pdf}")
        if not language.strip():
            raise ValueError("OCR language must not be empty")
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        sidecar_txt.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            self.executable,
            "--language",
            language,
            "--output-type",
            "pdf",
            "--optimize",
            "0",
            "--jobs",
            str(self.jobs),
            "--sidecar",
            str(sidecar_txt),
            str(master_pdf),
            str(output_pdf),
        ]
        completed = self._runner(argv)
        if (
            completed.returncode != 0
            or not output_pdf.is_file()
            or not sidecar_txt.is_file()
        ):
            detail = (completed.stdout or "").strip()
            if len(detail) > 2000:
                detail = detail[-2000:]
            suffix = f": {detail}" if detail else ""
            raise OcrAdapterError(
                f"ocrmypdf failed with exit code {completed.returncode}{suffix}"
            )

    def version(self) -> str:
        completed = self._runner([self.executable, "--version"])
        if completed.returncode != 0:
            detail = (completed.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise OcrAdapterError(
                f"ocrmypdf version probe failed with exit code "
                f"{completed.returncode}{suffix}"
            )
        lines = [
            line.strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip()
        ]
        if not lines:
            raise OcrAdapterError("ocrmypdf version probe returned no version")
        return lines[-1]