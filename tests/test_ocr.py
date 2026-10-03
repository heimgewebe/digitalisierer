from pathlib import Path
import subprocess

import pytest

from digitalisierer.ocr import OcrAdapterError, OcrmypdfBackend


def test_ocr_backend_builds_bounded_command_and_requires_outputs(tmp_path: Path) -> None:
    master = tmp_path / "master.pdf"
    master.write_bytes(b"pdf")
    output = tmp_path / "searchable.pdf"
    sidecar = tmp_path / "text.txt"
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, stdout="16.0\n")
        output.write_bytes(b"searchable")
        sidecar.write_text("text", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="ok")

    backend = OcrmypdfBackend(executable="/usr/bin/ocrmypdf", jobs=3, runner=runner)
    backend.searchable_pdf(master, output, sidecar, language="deu")

    assert calls[0] == [
        "/usr/bin/ocrmypdf",
        "--language",
        "deu",
        "--output-type",
        "pdf",
        "--optimize",
        "0",
        "--jobs",
        "3",
        "--sidecar",
        str(sidecar),
        str(master),
        str(output),
    ]


def test_ocr_backend_fails_if_command_succeeds_without_outputs(tmp_path: Path) -> None:
    master = tmp_path / "master.pdf"
    master.write_bytes(b"pdf")

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="no output")

    backend = OcrmypdfBackend(executable="/usr/bin/ocrmypdf", runner=runner)
    with pytest.raises(OcrAdapterError, match="failed"):
        backend.searchable_pdf(
            master,
            tmp_path / "missing.pdf",
            tmp_path / "missing.txt",
            language="deu",
        )

def test_ocr_backend_version_uses_version_line_after_warnings() -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="warning from dependency\n13.4.0+dfsg\n",
        )

    backend = OcrmypdfBackend(executable="/usr/bin/ocrmypdf", runner=runner)

    assert backend.version() == "13.4.0+dfsg"

@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (1, "probe failed", "version probe failed"),
        (0, "", "version probe returned no version"),
    ],
)
def test_ocr_backend_version_rejects_missing_provenance(
    returncode: int,
    stdout: str,
    message: str,
) -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

    backend = OcrmypdfBackend(executable="/usr/bin/ocrmypdf", runner=runner)

    with pytest.raises(OcrAdapterError, match=message):
        backend.version()



def test_ocr_backend_streams_master_chunks_via_stdin(tmp_path: Path) -> None:
    output = tmp_path / "searchable.pdf"
    sidecar = tmp_path / "text.txt"
    calls: list[list[str]] = []
    consumed: list[bytes] = []

    def stream_runner(
        argv: list[str],
        chunks: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        consumed.extend(chunks)  # type: ignore[arg-type]
        output.write_bytes(b"searchable")
        sidecar.write_text("text", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="ok")

    backend = OcrmypdfBackend(
        executable="/usr/bin/ocrmypdf",
        jobs=2,
        stream_runner=stream_runner,
    )
    backend.searchable_pdf_stream(
        iter((b"%PDF-part-1", b"-part-2")),
        output,
        sidecar,
        language="deu",
    )

    assert consumed == [b"%PDF-part-1", b"-part-2"]
    assert calls == [[
        "/usr/bin/ocrmypdf",
        "--language",
        "deu",
        "--output-type",
        "pdf",
        "--optimize",
        "0",
        "--jobs",
        "2",
        "--sidecar",
        str(sidecar),
        "-",
        str(output),
    ]]
