import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from digitalisierer.scanner import (
    ScannerWorkflowError,
    create_or_resume_scan_session,
    finalize_scan_session,
    load_processing_session,
    observe_scan_folder,
    update_review_item,
)


def _image(path: Path, value: int, *, size: tuple[int, int] = (120, 160)) -> None:
    image = Image.new("L", size, color=value)
    image.save(path, format="JPEG")


def test_observe_preserves_sources_and_generates_review_and_findings(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 255)
    _image(capture / "image00002.jpg", 20)

    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)

    assert len(observed.imported_asset_ids) == 2
    assert any(f.kind == "blank-or-near-blank" for f in observed.findings)
    processing = load_processing_session(paths)
    assert len(processing.ordered_assets()) == 2

    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    for asset in session["assets"]:
        original = capture / asset["source_name"]
        preserved = paths.root / asset["preserved_path"]
        assert preserved.read_bytes() == original.read_bytes()
        assert hashlib.sha256(preserved.read_bytes()).hexdigest() == asset["sha256"]
        assert (paths.root / asset["thumbnail_path"]).is_file()


def test_observe_resume_skips_already_preserved_assets(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")

    first = observe_scan_folder(paths, capture)
    second = observe_scan_folder(paths, capture)

    assert len(first.imported_asset_ids) == 1
    assert second.imported_asset_ids == ()
    assert second.skipped_asset_ids == first.imported_asset_ids


def test_review_state_controls_export_order_without_mutating_sources(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 60)
    _image(capture / "image00002.jpg", 160)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, second = observed.imported_asset_ids

    update_review_item(paths, first, sequence=20)
    processing = update_review_item(paths, second, sequence=10)

    assert [asset.asset_id for asset in processing.ordered_assets()] == [second, first]
    for asset in processing.ordered_assets():
        assert asset.path.is_file()


def test_review_replacement_invariant_is_enforced(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 40)
    _image(capture / "image00002.jpg", 180)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, second = observed.imported_asset_ids

    with pytest.raises(ScannerWorkflowError, match="requires replaced asset"):
        update_review_item(paths, second, replacement_for=first)

    # Failed review writes are rolled back.
    processing = load_processing_session(paths)
    assert len(processing.ordered_assets()) == 2


class _FakeOcr:
    name = "fake-ocr"

    def searchable_pdf(
        self,
        master_pdf: Path,
        output_pdf: Path,
        sidecar_txt: Path,
        *,
        language: str,
    ) -> None:
        assert language == "deu"
        output_pdf.write_bytes(master_pdf.read_bytes() + b"-ocr")
        sidecar_txt.write_text("recognized", encoding="utf-8")

    def version(self) -> str:
        return "test"


def _fake_pdf(images: list[Path], output: Path) -> None:
    output.write_bytes(
        b"PDF:" + b"|".join(hashlib.sha256(path.read_bytes()).digest() for path in images)
    )


def test_finalize_is_review_bound_hash_bound_and_no_replace(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 30)
    _image(capture / "image00002.jpg", 200)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, second = observed.imported_asset_ids
    update_review_item(paths, first, sequence=20)
    update_review_item(paths, second, sequence=10)

    exported = finalize_scan_session(
        paths,
        _FakeOcr(),
        pdf_builder=_fake_pdf,
    )
    manifest = json.loads(
        (exported.export_dir / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["layout"] == "digitalisierer.scan-export.v1"
    assert [item["asset_id"] for item in manifest["active_order"]] == [second, first]
    for name, expected in manifest["output_hashes"].items():
        actual = hashlib.sha256((exported.export_dir / name).read_bytes()).hexdigest()
        assert actual == expected
    assert exported.output_hashes["manifest.json"] == hashlib.sha256(
        (exported.export_dir / "manifest.json").read_bytes()
    ).hexdigest()

    with pytest.raises(ScannerWorkflowError, match="already exists"):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)


def test_finalize_rejects_modified_preserved_source(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 90)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    preserved = paths.root / session["assets"][0]["preserved_path"]
    preserved.write_bytes(b"tampered")

    with pytest.raises(ScannerWorkflowError, match="hash mismatch"):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)