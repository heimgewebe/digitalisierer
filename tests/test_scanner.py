import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image
import pytest

import digitalisierer.review as review_module
import digitalisierer.scanner as scanner_module
from digitalisierer.scanner import (
    ScannerWorkflowError,
    create_or_resume_scan_session,
    finalize_scan_session,
    load_processing_session,
    observe_scan_folder,
    scan_session_paths,
    update_review_item,
)


def _image(path: Path, value: int, *, size: tuple[int, int] = (120, 160)) -> None:
    image = Image.new("L", size, color=value)
    image.save(path, format="JPEG")


def test_init_rejects_occupied_non_scanner_session_root(tmp_path: Path) -> None:
    library = tmp_path / "library"
    foreign_root = library / "projects" / "inbox" / "sessions" / "shared"
    foreign_root.mkdir(parents=True)
    foreign = foreign_root / "transcription.json"
    foreign.write_text('{"kind":"digitalisierer.transcription"}\n', encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="already occupied by non-scanner content",
    ):
        create_or_resume_scan_session("inbox", "shared", library)

    assert foreign.read_text(encoding="utf-8") == (
        '{"kind":"digitalisierer.transcription"}\n'
    )
    assert not (foreign_root / "session.json").exists()
    assert sorted(path.name for path in foreign_root.iterdir()) == ["transcription.json"]


def test_init_claims_preexisting_empty_session_root(tmp_path: Path) -> None:
    library = tmp_path / "library"
    root = library / "projects" / "book" / "sessions" / "empty"
    root.mkdir(parents=True)

    paths = create_or_resume_scan_session("book", "empty", library)

    assert paths.root == root
    assert paths.session_file.is_file()
    assert paths.sources.is_dir()
    assert paths.thumbnails.is_dir()
    assert paths.exports.is_dir()


@pytest.mark.parametrize("storage_name", ["sources", "thumbnails", "exports"])
def test_resume_rejects_symlinked_storage_directory(
    tmp_path: Path,
    storage_name: str,
) -> None:
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", "symlink-storage", library)
    storage = getattr(paths, storage_name)
    outside = tmp_path / f"outside-{storage_name}"
    outside.mkdir()
    storage.rmdir()
    storage.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ScannerWorkflowError,
        match=rf"scanner {storage_name} directory must be a canonical direct child",
    ):
        create_or_resume_scan_session("book", "symlink-storage", library)

    assert list(outside.iterdir()) == []


def test_init_rejects_symlinked_session_root_before_writing(tmp_path: Path) -> None:
    library = tmp_path / "library"
    root = library / "projects" / "book" / "sessions" / "symlink-root"
    outside = tmp_path / "outside-session-root"
    outside.mkdir()
    root.parent.mkdir(parents=True)
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ScannerWorkflowError,
        match="scan session root must not be a symlink",
    ):
        create_or_resume_scan_session("book", "symlink-root", library)

    assert list(outside.iterdir()) == []


def test_review_lock_rejects_symlinked_session_root_without_writing_target(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", "symlink-review-lock", library)
    outside = tmp_path / "outside-session-root"
    paths.root.rename(outside)
    paths.root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ScannerWorkflowError,
        match="scan session root must not be a symlink",
    ):
        load_processing_session(paths)

    assert not (outside / ".review.lock").exists()


def test_observe_rejects_symlinked_existing_preserved_target(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-symlink-target"
    capture.mkdir()
    source = capture / "image00001.jpg"
    _image(source, 100)
    paths = create_or_resume_scan_session(
        "book", "symlink-target", tmp_path / "library"
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset_id = scanner_module._asset_id(source.name, digest)
    outside = tmp_path / "outside-source.jpg"
    outside.write_bytes(source.read_bytes())
    target = paths.sources / f"{asset_id}.jpg"
    target.symlink_to(outside)

    before = (
        paths.session_file.read_bytes(),
        paths.review_file.read_bytes(),
        paths.findings_file.read_bytes(),
    )
    with pytest.raises(ScannerWorkflowError, match="non-symlink regular file"):
        observe_scan_folder(paths, capture)

    assert target.is_symlink()
    assert outside.read_bytes() == source.read_bytes()
    assert (
        paths.session_file.read_bytes(),
        paths.review_file.read_bytes(),
        paths.findings_file.read_bytes(),
    ) == before


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
        thumbnail = paths.root / asset["thumbnail_path"]
        assert thumbnail.is_file()
        assert hashlib.sha256(thumbnail.read_bytes()).hexdigest() == asset["thumbnail_sha256"]


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


def test_observe_rejects_out_of_order_backfill_from_same_capture_folder(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-backfill"
    capture.mkdir()
    for index in range(1, 13):
        _image(capture / f"image{index:05d}.jpg", 20 + index * 10)
    paths = create_or_resume_scan_session(
        "book",
        "out-of-order-backfill",
        tmp_path / "library",
    )

    first = observe_scan_folder(paths, capture, start=11, limit=2)
    assert len(first.imported_asset_ids) == 2
    review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    assert [
        review["items"][asset_id]["sequence"]
        for asset_id in first.imported_asset_ids
    ] == [11, 12]

    metadata_before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    sources_before = {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    }
    thumbnails_before = {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    }

    with pytest.raises(
        ScannerWorkflowError,
        match="out-of-order capture backfill is not supported",
    ):
        observe_scan_folder(paths, capture, start=1, limit=10)

    assert {
        path: path.read_bytes() for path in metadata_before
    } == metadata_before
    assert {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    } == sources_before
    assert {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    } == thumbnails_before


def test_observe_allows_forward_overlapping_range_from_same_capture_folder(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-forward-overlap"
    capture.mkdir()
    for index in range(1, 13):
        _image(capture / f"image{index:05d}.jpg", 20 + index * 10)
    paths = create_or_resume_scan_session(
        "book",
        "forward-overlap",
        tmp_path / "library",
    )

    first = observe_scan_folder(paths, capture, start=1, limit=10)
    second = observe_scan_folder(paths, capture, start=5, limit=8)
    processing = load_processing_session(paths)

    assert len(first.imported_asset_ids) == 10
    assert len(second.imported_asset_ids) == 2
    assert second.skipped_asset_ids == first.imported_asset_ids[4:10]
    assert [item.sequence for item in processing.ordered_items()] == list(
        range(1, 13)
    )


def test_observe_appends_sequences_for_a_second_capture_folder(
    tmp_path: Path,
) -> None:
    first_capture = tmp_path / "capture-1"
    second_capture = tmp_path / "capture-2"
    first_capture.mkdir()
    second_capture.mkdir()
    _image(first_capture / "image00001.jpg", 40)
    _image(first_capture / "image00002.jpg", 80)
    _image(second_capture / "image00001.jpg", 120)
    _image(second_capture / "image00002.jpg", 160)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")

    first = observe_scan_folder(paths, first_capture)
    second = observe_scan_folder(paths, second_capture)
    processing = load_processing_session(paths)

    assert len(first.imported_asset_ids) == 2
    assert len(second.imported_asset_ids) == 2
    assert [asset.sequence for asset in processing.ordered_items()] == [1, 2, 3, 4]


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


class _FakePdf:
    name = "fake-pdf"

    def __init__(self, version_value: str = "test") -> None:
        self.version_value = version_value

    def version(self) -> str:
        return self.version_value

    def __call__(self, images: list[Path], output: Path) -> None:
        output.write_bytes(
            b"PDF:"
            + b"|".join(hashlib.sha256(path.read_bytes()).digest() for path in images)
        )


_fake_pdf = _FakePdf()


def test_near_duplicate_findings_aggregate_large_similarity_group() -> None:
    assets = [
        {
            "asset_id": f"page-{index:04d}",
            "image": {
                "width": 120,
                "height": 160,
                "stddev": 50.0,
                "dark_ratio": 0.25,
                "average_hash": "0",
            },
        }
        for index in range(1000)
    ]

    findings = scanner_module._findings_from_assets(assets)
    near_duplicates = [
        finding for finding in findings if finding.kind == "near-duplicate"
    ]

    assert len(findings) == 1
    assert len(near_duplicates) == 1
    grouped = near_duplicates[0]
    assert grouped.asset_ids == tuple(f"page-{index:04d}" for index in range(1000))
    assert grouped.evidence == (
        "average_hash_distance_threshold=20",
        "member_count=1000",
        "similar_pair_count=499500",
    )
    payload = {
        "schema_version": 1,
        "kind": "digitalisierer.scan-findings",
        "findings": [scanner_module._finding_payload(grouped)],
    }
    scanner_module._validate_findings_payload(payload)
    assert len(grouped.asset_ids) == 1000


def test_near_duplicate_group_supports_transitive_similarity() -> None:
    hashes = (
        "0",
        f"{(1 << 20) - 1:x}",
        f"{(1 << 40) - 1:x}",
    )
    assets = [
        {
            "asset_id": f"page-{index + 1}",
            "image": {
                "width": 120,
                "height": 160,
                "stddev": 50.0,
                "dark_ratio": 0.25,
                "average_hash": average_hash,
            },
        }
        for index, average_hash in enumerate(hashes)
    ]

    findings = scanner_module._findings_from_assets(assets)
    near_duplicates = [
        finding for finding in findings if finding.kind == "near-duplicate"
    ]

    assert len(near_duplicates) == 1
    assert near_duplicates[0].asset_ids == ("page-1", "page-2", "page-3")
    assert near_duplicates[0].evidence == (
        "average_hash_distance_threshold=20",
        "member_count=3",
        "similar_pair_count=2",
    )


def test_grouped_near_duplicates_render_and_finalize(tmp_path: Path) -> None:
    capture = tmp_path / "grouped-near-duplicates"
    capture.mkdir()
    for index in range(3):
        _image(capture / f"page-{index + 1}.jpg", 90)

    paths = create_or_resume_scan_session(
        "book",
        "grouped-near-duplicates",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    near_duplicates = [
        finding for finding in observed.findings if finding.kind == "near-duplicate"
    ]

    assert len(near_duplicates) == 1
    assert near_duplicates[0].asset_ids == observed.imported_asset_ids

    review_html = review_module.render_review_html(paths, csrf_token="test-token")
    rendered_message = (
        "near-duplicate: pages form a near-duplicate visual-hash similarity group"
    )
    assert review_html.count(rendered_message) == 3

    exported = finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)
    manifest = json.loads(
        (exported.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    manifest_near_duplicates = [
        finding
        for finding in manifest["findings"]["findings"]
        if finding["kind"] == "near-duplicate"
    ]
    assert len(manifest_near_duplicates) == 1
    assert manifest_near_duplicates[0]["asset_ids"] == list(
        observed.imported_asset_ids
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"kind": ""},
        {"kind": "   "},
        {"message": ""},
        {"message": "   "},
        {"asset_ids": ["   "]},
        {"asset_ids": ["duplicate", "duplicate"]},
    ],
)
def test_finalize_rejects_findings_that_violate_domain_contract(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    capture = tmp_path / "invalid-findings"
    capture.mkdir()
    _image(capture / "page.jpg", 90)
    paths = create_or_resume_scan_session(
        "book",
        "invalid-findings",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    finding: dict[str, object] = {
        "kind": "manual-note",
        "message": "valid finding",
        "asset_ids": [asset_id],
        "confidence": 0.5,
        "evidence": ["manual"],
    }
    finding.update(mutation)
    findings = json.loads(paths.findings_file.read_text(encoding="utf-8"))
    findings["findings"] = [finding]
    paths.findings_file.write_text(
        json.dumps(findings) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ScannerWorkflowError, match="invalid shape"):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


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
    assert manifest["pdf_builder"] == {"adapter": "fake-pdf", "version": "test"}
    assert [item["asset_id"] for item in manifest["active_order"]] == [second, first]
    for name, expected in manifest["output_hashes"].items():
        actual = hashlib.sha256((exported.export_dir / name).read_bytes()).hexdigest()
        assert actual == expected
    assert exported.output_hashes["manifest.json"] == hashlib.sha256(
        (exported.export_dir / "manifest.json").read_bytes()
    ).hexdigest()

    with pytest.raises(ScannerWorkflowError, match="already exists"):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)


def test_finalize_flushes_staging_and_publication_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "durable-export"
    capture.mkdir()
    _image(capture / "image00001.jpg", 90)
    paths = create_or_resume_scan_session(
        "book", "durable-export", tmp_path / "library"
    )
    observe_scan_folder(paths, capture)

    events: list[tuple[str, str]] = []
    original_file_fsync = scanner_module._fsync_regular_file
    original_dir_fsync = scanner_module._fsync_directory
    original_rename = scanner_module._rename_noreplace

    def file_fsync(path: Path) -> None:
        events.append(("file", path.name))
        original_file_fsync(path)

    def dir_fsync(path: Path) -> None:
        kind = "staging-dir" if ".staging-" in path.name else "exports-dir"
        events.append((kind, path.name))
        original_dir_fsync(path)

    def rename(source: Path, target: Path) -> None:
        events.append(("rename", target.name))
        original_rename(source, target)

    monkeypatch.setattr(scanner_module, "_fsync_regular_file", file_fsync)
    monkeypatch.setattr(scanner_module, "_fsync_directory", dir_fsync)
    monkeypatch.setattr(scanner_module, "_rename_noreplace", rename)

    exported = finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert [name for kind, name in events if kind == "file"] == [
        "master.pdf",
        "searchable.pdf",
        "text.txt",
        "report.txt",
        "manifest.json",
    ]
    last_file = max(i for i, event in enumerate(events) if event[0] == "file")
    staging_dir = next(i for i, event in enumerate(events) if event[0] == "staging-dir")
    rename_at = next(i for i, event in enumerate(events) if event[0] == "rename")
    exports_dir = next(i for i, event in enumerate(events) if event[0] == "exports-dir")
    assert last_file < staging_dir < rename_at < exports_dir
    assert exported.export_dir.is_dir()


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

def test_finalize_uses_one_review_snapshot_when_review_changes_during_ocr(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 30)
    _image(capture / "image00002.jpg", 200)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, second = observed.imported_asset_ids

    class _ReviewMutatingOcr(_FakeOcr):
        def searchable_pdf(
            self,
            master_pdf: Path,
            output_pdf: Path,
            sidecar_txt: Path,
            *,
            language: str,
        ) -> None:
            super().searchable_pdf(
                master_pdf,
                output_pdf,
                sidecar_txt,
                language=language,
            )
            review = json.loads(paths.review_file.read_text(encoding="utf-8"))
            review["items"][first]["included"] = False
            paths.review_file.write_text(
                json.dumps(review, indent=2) + "\n",
                encoding="utf-8",
            )

    with pytest.raises(
        ScannerWorkflowError,
        match="scanner session metadata changed while finalizing",
    ):
        finalize_scan_session(
            paths,
            _ReviewMutatingOcr(),
            pdf_builder=_fake_pdf,
        )

    current_review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    assert current_review["items"][first]["included"] is False
    assert list(paths.exports.iterdir()) == []

def test_observe_resume_rejects_known_asset_missing_review_state(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    first = observe_scan_folder(paths, capture)
    asset_id = first.imported_asset_ids[0]
    update_review_item(paths, asset_id, included=False)

    review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    review["items"].pop(asset_id)
    paths.review_file.write_text(
        json.dumps(review, indent=2) + "\n",
        encoding="utf-8",
    )
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }

    with pytest.raises(
        ScannerWorkflowError,
        match="missing a decision for existing asset",
    ):
        observe_scan_folder(paths, capture)

    assert {path: path.read_bytes() for path in before} == before
    assert asset_id not in json.loads(
        paths.review_file.read_text(encoding="utf-8")
    )["items"]


def test_observe_is_recoverable_if_session_write_fails_after_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import digitalisierer.scanner as scanner_module

    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    original_write = scanner_module._atomic_write_text
    failed = False

    def fail_session_once(path: Path, content: str) -> None:
        nonlocal failed
        if path == paths.session_file and not failed:
            failed = True
            raise OSError("synthetic session write failure")
        original_write(path, content)

    monkeypatch.setattr(scanner_module, "_atomic_write_text", fail_session_once)
    with pytest.raises(OSError, match="synthetic session write failure"):
        observe_scan_folder(paths, capture)

    monkeypatch.setattr(scanner_module, "_atomic_write_text", original_write)
    resumed = observe_scan_folder(paths, capture)
    processing = load_processing_session(paths)

    assert len(resumed.imported_asset_ids) == 1
    assert len(processing.ordered_assets()) == 1


def test_review_on_uninitialized_session_has_no_filesystem_side_effect(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    paths = scan_session_paths("book", "not-yet-initialized", library)

    assert not paths.root.exists()

    with pytest.raises(
        ScannerWorkflowError,
        match="scanner session is not initialized",
    ):
        load_processing_session(paths)
    assert not paths.root.exists()

    with pytest.raises(
        ScannerWorkflowError,
        match="scanner session is not initialized",
    ):
        update_review_item(paths, "missing", included=False)
    assert not paths.root.exists()

    created = create_or_resume_scan_session(
        "book",
        "not-yet-initialized",
        library,
    )
    assert created.session_file.is_file()


def test_review_state_snapshot_serializes_observe_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_capture = tmp_path / "snapshot-first"
    second_capture = tmp_path / "snapshot-second"
    first_capture.mkdir()
    second_capture.mkdir()
    _image(first_capture / "image00001.jpg", 80)
    _image(second_capture / "image00002.jpg", 160)
    paths = create_or_resume_scan_session(
        "book",
        "snapshot-race",
        tmp_path / "library",
    )
    observe_scan_folder(paths, first_capture)

    original_snapshot = scanner_module._stable_json_snapshot
    session_snapshot_reached = threading.Event()
    release_snapshot = threading.Event()
    observe_done = threading.Event()
    failures: list[BaseException] = []
    loaded_asset_counts: list[int] = []

    def delayed_snapshot(path: Path) -> tuple[dict[str, object], str]:
        result = original_snapshot(path)
        if path == paths.session_file and not session_snapshot_reached.is_set():
            session_snapshot_reached.set()
            assert release_snapshot.wait(timeout=2.0)
        return result

    monkeypatch.setattr(scanner_module, "_stable_json_snapshot", delayed_snapshot)

    def load_state() -> None:
        try:
            session, _review, _findings, _processing = scanner_module.load_review_state(
                paths
            )
            assets = session.get("assets")
            assert isinstance(assets, list)
            loaded_asset_counts.append(len(assets))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    def observe_second() -> None:
        try:
            observe_scan_folder(paths, second_capture)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
        finally:
            observe_done.set()

    reader = threading.Thread(target=load_state)
    writer = threading.Thread(target=observe_second)
    reader.start()
    assert session_snapshot_reached.wait(timeout=2.0)
    writer.start()

    # The observer needs the same lock, so it cannot publish review/findings/session
    # between the three snapshot reads.
    assert observe_done.wait(timeout=0.1) is False
    release_snapshot.set()
    reader.join(timeout=2.0)
    writer.join(timeout=2.0)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert failures == []
    assert loaded_asset_counts == [1]
    assert len(load_processing_session(paths).items) == 2


def test_review_updates_are_serialized_without_lost_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import digitalisierer.scanner as scanner_module

    capture = tmp_path / "capture-concurrent"
    capture.mkdir()
    _image(capture / "image00001.jpg", 80)
    _image(capture / "image00002.jpg", 160)
    paths = create_or_resume_scan_session("book", "concurrent", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, second = observed.imported_asset_ids

    original_write = scanner_module._atomic_write_text
    first_write_reached = threading.Event()
    second_write_reached = threading.Event()
    release_first_write = threading.Event()
    counter_lock = threading.Lock()
    review_write_count = 0

    def delayed_review_write(path: Path, content: str) -> None:
        nonlocal review_write_count
        call = 0
        if path == paths.review_file:
            with counter_lock:
                review_write_count += 1
                call = review_write_count
            if call == 1:
                first_write_reached.set()
                assert release_first_write.wait(timeout=2.0)
            elif call == 2:
                second_write_reached.set()
        original_write(path, content)

    monkeypatch.setattr(scanner_module, "_atomic_write_text", delayed_review_write)
    failures: list[BaseException] = []

    def change(asset_id: str, sequence: int) -> None:
        try:
            update_review_item(paths, asset_id, sequence=sequence)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    first_thread = threading.Thread(target=change, args=(first, 10))
    second_thread = threading.Thread(target=change, args=(second, 20))
    first_thread.start()
    assert first_write_reached.wait(timeout=2.0)
    second_thread.start()

    # Without the session lock, the second writer reaches publication while the
    # first still holds a stale review snapshot and one accepted edit is lost.
    second_write_reached.wait(timeout=0.1)
    release_first_write.set()
    first_thread.join(timeout=2.0)
    second_thread.join(timeout=2.0)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert failures == []
    review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    assert review["items"][first]["sequence"] == 10
    assert review["items"][second]["sequence"] == 20
    assert [item.sequence for item in load_processing_session(paths).ordered_items()] == [
        10,
        20,
    ]

def test_observe_rejects_malformed_existing_asset_record(tmp_path: Path) -> None:
    capture = tmp_path / "capture-malformed"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session("book", "malformed", tmp_path / "library")
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    session["assets"] = ["not-an-object"]
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")

    with pytest.raises(ScannerWorkflowError, match="asset record must be an object"):
        observe_scan_folder(paths, capture)

    persisted = json.loads(paths.session_file.read_text(encoding="utf-8"))
    assert persisted["assets"] == ["not-an-object"]


def test_finalize_initial_snapshot_serializes_observe_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_capture = tmp_path / "finalize-snapshot-first"
    second_capture = tmp_path / "finalize-snapshot-second"
    first_capture.mkdir()
    second_capture.mkdir()
    _image(first_capture / "image00001.jpg", 80)
    _image(second_capture / "image00002.jpg", 160)
    paths = create_or_resume_scan_session(
        "book",
        "finalize-snapshot",
        tmp_path / "library",
    )
    observe_scan_folder(paths, first_capture)

    original_snapshot = scanner_module._stable_json_snapshot
    first_session_snapshot = threading.Event()
    release_snapshot = threading.Event()
    observe_done = threading.Event()
    finalizer_failures: list[BaseException] = []
    observer_failures: list[BaseException] = []

    def delayed_snapshot(path: Path) -> tuple[dict[str, object], str]:
        result = original_snapshot(path)
        if path == paths.session_file and not first_session_snapshot.is_set():
            first_session_snapshot.set()
            assert release_snapshot.wait(timeout=2.0)
        return result

    monkeypatch.setattr(scanner_module, "_stable_json_snapshot", delayed_snapshot)

    class _DelayedPdf(_FakePdf):
        def __call__(self, images: list[Path], output: Path) -> None:
            assert observe_done.wait(timeout=2.0)
            super().__call__(images, output)

    delayed_pdf = _DelayedPdf()

    def finalize() -> None:
        try:
            finalize_scan_session(paths, _FakeOcr(), pdf_builder=delayed_pdf)
        except BaseException as exc:  # pragma: no cover - surfaced below
            finalizer_failures.append(exc)

    def observe_second() -> None:
        try:
            observe_scan_folder(paths, second_capture)
        except BaseException as exc:  # pragma: no cover - surfaced below
            observer_failures.append(exc)
        finally:
            observe_done.set()

    finalizer = threading.Thread(target=finalize)
    observer = threading.Thread(target=observe_second)
    finalizer.start()
    assert first_session_snapshot.wait(timeout=2.0)
    observer.start()

    assert observe_done.wait(timeout=0.1) is False
    release_snapshot.set()
    finalizer.join(timeout=2.0)
    observer.join(timeout=2.0)

    assert not finalizer.is_alive()
    assert not observer.is_alive()
    assert observer_failures == []
    assert len(finalizer_failures) == 1
    assert isinstance(finalizer_failures[0], ScannerWorkflowError)
    assert "metadata changed while finalizing" in str(finalizer_failures[0])
    assert len(load_processing_session(paths).items) == 2
    assert list(paths.exports.iterdir()) == []


def test_finalize_serializes_review_update_through_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import digitalisierer.scanner as scanner_module

    capture = tmp_path / "capture-publish-race"
    capture.mkdir()
    _image(capture / "image00001.jpg", 50)
    _image(capture / "image00002.jpg", 150)
    paths = create_or_resume_scan_session("book", "publish-race", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, _second = observed.imported_asset_ids

    original_rename = scanner_module._rename_noreplace
    rename_reached = threading.Event()
    allow_rename = threading.Event()
    review_update_done = threading.Event()
    failures: list[BaseException] = []
    exports = []

    def delayed_rename(source: Path, target: Path) -> None:
        rename_reached.set()
        assert allow_rename.wait(timeout=2.0)
        original_rename(source, target)

    monkeypatch.setattr(scanner_module, "_rename_noreplace", delayed_rename)

    def finalize() -> None:
        try:
            exports.append(
                finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    def change_review() -> None:
        try:
            update_review_item(paths, first, included=False)
            review_update_done.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    finalize_thread = threading.Thread(target=finalize)
    finalize_thread.start()
    assert rename_reached.wait(timeout=2.0)

    update_thread = threading.Thread(target=change_review)
    update_thread.start()
    assert not review_update_done.wait(timeout=0.1)

    allow_rename.set()
    finalize_thread.join(timeout=2.0)
    update_thread.join(timeout=2.0)

    assert not finalize_thread.is_alive()
    assert not update_thread.is_alive()
    assert failures == []
    assert review_update_done.is_set()
    assert len(exports) == 1

    manifest = json.loads(
        (exports[0].export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["review"]["items"][first]["included"] is True
    current_review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    assert current_review["items"][first]["included"] is False

def test_finalize_rejects_orphan_review_entries(tmp_path: Path) -> None:
    capture = tmp_path / "capture-orphan-review"
    capture.mkdir()
    _image(capture / "image00001.jpg", 90)
    paths = create_or_resume_scan_session("book", "orphan-review", tmp_path / "library")
    observe_scan_folder(paths, capture)

    review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    review["items"]["orphan-asset"] = {
        "sequence": 2,
        "included": True,
        "replacement_for": None,
    }
    paths.review_file.write_text(json.dumps(review) + "\n", encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="review state contains assets missing from scan session",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


def test_observe_ignores_symlinked_image_entries(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-image-symlink"
    capture.mkdir()
    _image(capture / "image00001.jpg", 90)
    outside = tmp_path / "outside.jpg"
    _image(outside, 180)
    (capture / "image00002.jpg").symlink_to(outside)
    paths = create_or_resume_scan_session(
        "book",
        "image-symlink",
        tmp_path / "library",
    )

    observed = observe_scan_folder(paths, capture)

    assert len(observed.imported_asset_ids) == 1
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    assert [asset["source_name"] for asset in session["assets"]] == [
        "image00001.jpg",
    ]
    assert outside.read_bytes() != b""


def test_stable_hash_rejects_symlink_swap_before_descriptor_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.jpg"
    outside = tmp_path / "outside.jpg"
    _image(source, 90)
    _image(outside, 180)
    original_open = os.open
    injected = False

    def raced_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if os.fspath(path) == os.fspath(source) and not injected:
            injected = True
            source.unlink()
            source.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)

    with pytest.raises(
        ScannerWorkflowError,
        match="non-symlink regular file",
    ):
        scanner_module._stable_hash(source)

    assert injected is True
    assert source.is_symlink()


@pytest.mark.parametrize("preserve_name", ["_copy_preserved", "_replace_preserved"])
def test_preserve_rejects_source_symlink_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preserve_name: str,
) -> None:
    source = tmp_path / "source.jpg"
    outside = tmp_path / "outside.jpg"
    target = tmp_path / "preserved.jpg"
    _image(source, 90)
    _image(outside, 180)
    expected_sha256, expected_stat = scanner_module._stable_hash(source)
    original_open = os.open
    injected = False

    def raced_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if os.fspath(path) == os.fspath(source) and not injected:
            injected = True
            source.unlink()
            source.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)
    preserve = getattr(scanner_module, preserve_name)

    with pytest.raises(
        ScannerWorkflowError,
        match="non-symlink regular file",
    ):
        preserve(
            source,
            target,
            expected_sha256=expected_sha256,
            expected_stat=expected_stat,
        )

    assert injected is True
    assert not target.exists()


def test_default_pdf_builder_streams_to_img2pdf_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import digitalisierer.scanner as scanner_module
    import img2pdf  # type: ignore[import-untyped]

    image = tmp_path / "page.jpg"
    _image(image, 100)
    output = tmp_path / "master.pdf"

    def fake_convert(
        images: list[str],
        *,
        outputstream: object,
    ) -> bytes:
        assert images == [str(image)]
        write = getattr(outputstream, "write")
        write(b"streamed-pdf")
        return b"buffered-return-must-not-be-used"

    monkeypatch.setattr(img2pdf, "convert", fake_convert)
    scanner_module._default_pdf_builder([image], output)

    assert output.read_bytes() == b"streamed-pdf"

def test_observe_flushes_artifact_directories_before_dependent_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "durable-observe"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "durable-observe",
        tmp_path / "library",
    )

    events: list[tuple[str, str]] = []
    original_dir_fsync = scanner_module._fsync_directory
    original_atomic_write = scanner_module._atomic_write_text

    def dir_fsync(path: Path) -> None:
        events.append(("dir", path.name))
        original_dir_fsync(path)

    def metadata_write(path: Path, content: str) -> None:
        events.append(("metadata", path.name))
        original_atomic_write(path, content)

    monkeypatch.setattr(scanner_module, "_fsync_directory", dir_fsync)
    monkeypatch.setattr(scanner_module, "_atomic_write_text", metadata_write)

    observed = observe_scan_folder(paths, capture)

    assert len(observed.imported_asset_ids) == 1
    sources_fsync = events.index(("dir", "sources"))
    thumbnails_fsync = events.index(("dir", "thumbnails"))
    first_metadata = min(
        index
        for index, event in enumerate(events)
        if event[0] == "metadata"
        and event[1] in {"review.json", "findings.json", "session.json"}
    )
    assert sources_fsync < first_metadata
    assert thumbnails_fsync < first_metadata


@pytest.mark.parametrize("failure_stage", ["second-hash", "findings"])
def test_observe_precommit_failure_rolls_back_earlier_new_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    capture = tmp_path / f"capture-precommit-{failure_stage}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 90)
    _image(capture / "image00002.jpg", 160)
    paths = create_or_resume_scan_session(
        "book",
        f"precommit-{failure_stage}",
        tmp_path / "library",
    )
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }

    if failure_stage == "second-hash":
        original_hash = scanner_module._stable_hash
        calls = 0

        def fail_second_hash(path: Path) -> tuple[str, os.stat_result]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ScannerWorkflowError("synthetic second source hash failure")
            return original_hash(path)

        monkeypatch.setattr(scanner_module, "_stable_hash", fail_second_hash)
        expected = "synthetic second source hash failure"
    else:
        def fail_findings(
            assets: list[dict[str, object]],
        ) -> list[object]:
            del assets
            raise ScannerWorkflowError("synthetic findings preparation failure")

        monkeypatch.setattr(scanner_module, "_findings_from_assets", fail_findings)
        expected = "synthetic findings preparation failure"

    with pytest.raises(ScannerWorkflowError, match=expected):
        observe_scan_folder(paths, capture)

    assert list(paths.sources.iterdir()) == []
    assert list(paths.thumbnails.iterdir()) == []
    assert {path: path.read_bytes() for path in before} == before


def test_observe_findings_failure_does_not_publish_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import digitalisierer.scanner as scanner_module

    capture = tmp_path / "capture-findings-failure"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "findings-failure",
        tmp_path / "library",
    )
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    original_write = scanner_module._atomic_write_text
    failed = False

    def fail_findings_once(path: Path, content: str) -> None:
        nonlocal failed
        if path == paths.findings_file and not failed:
            failed = True
            raise OSError("synthetic findings write failure")
        original_write(path, content)

    monkeypatch.setattr(scanner_module, "_atomic_write_text", fail_findings_once)
    with pytest.raises(OSError, match="synthetic findings write failure"):
        observe_scan_folder(paths, capture)

    for metadata_path, expected in before.items():
        assert metadata_path.read_bytes() == expected
    assert list(paths.sources.iterdir()) == []
    assert list(paths.thumbnails.iterdir()) == []
    with pytest.raises(
        ScannerWorkflowError,
        match="scanner session has no included pages",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)
    assert list(paths.exports.iterdir()) == []

    monkeypatch.setattr(scanner_module, "_atomic_write_text", original_write)
    resumed = observe_scan_folder(paths, capture)
    processing = load_processing_session(paths)

    assert len(resumed.imported_asset_ids) == 1
    assert len(processing.ordered_assets()) == 1
    session_after_retry = json.loads(paths.session_file.read_text(encoding="utf-8"))
    review_after_retry = json.loads(paths.review_file.read_text(encoding="utf-8"))
    assert {item["asset_id"] for item in session_after_retry["assets"]} == set(
        review_after_retry["items"]
    )

def test_observe_session_commit_failure_restores_previous_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "capture-session-commit-failure"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "session-commit-failure",
        tmp_path / "library",
    )
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    original_write = scanner_module._atomic_write_text
    failed = False

    def fail_session_after_replace(path: Path, content: str) -> None:
        nonlocal failed
        original_write(path, content)
        if path == paths.session_file and not failed:
            failed = True
            raise OSError("synthetic session commit failure after replace")

    monkeypatch.setattr(
        scanner_module,
        "_atomic_write_text",
        fail_session_after_replace,
    )

    with pytest.raises(
        OSError,
        match="synthetic session commit failure after replace",
    ):
        observe_scan_folder(paths, capture)

    assert failed is True
    for metadata_path, expected in before.items():
        assert metadata_path.read_bytes() == expected
    assert list(paths.sources.iterdir()) == []
    assert list(paths.thumbnails.iterdir()) == []

    monkeypatch.setattr(scanner_module, "_atomic_write_text", original_write)
    resumed = observe_scan_folder(paths, capture)
    assert len(resumed.imported_asset_ids) == 1
    processing = load_processing_session(paths)
    assert len(processing.ordered_assets()) == 1


@pytest.mark.parametrize("artifact_kind", ["source", "thumbnail"])
def test_publication_verification_failure_removes_owned_new_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_kind: str,
) -> None:
    source = tmp_path / "source.jpg"
    _image(source, 100)
    output_dir = tmp_path / "published"
    output_dir.mkdir()
    target = output_dir / ("source.jpg" if artifact_kind == "source" else "thumb.jpg")
    original_snapshot = scanner_module._regular_file_snapshot
    failed = False

    def fail_published_snapshot(
        path: Path,
        *,
        purpose: str,
    ) -> tuple[str, tuple[int, int, int, int]]:
        nonlocal failed
        if purpose.startswith("published ") and not failed:
            failed = True
            raise ScannerWorkflowError("synthetic post-publication verification failure")
        return original_snapshot(path, purpose=purpose)

    monkeypatch.setattr(
        scanner_module,
        "_regular_file_snapshot",
        fail_published_snapshot,
    )

    with pytest.raises(
        ScannerWorkflowError,
        match="synthetic post-publication verification failure",
    ):
        if artifact_kind == "source":
            sha256, source_stat = scanner_module._stable_hash(source)
            scanner_module._copy_preserved(
                source,
                target,
                expected_sha256=sha256,
                expected_stat=source_stat,
            )
        else:
            scanner_module._write_thumbnail(source, target)

    assert failed is True
    assert not target.exists()
    assert list(output_dir.iterdir()) == []


@pytest.mark.parametrize("artifact_kind", ["source", "thumbnail"])
def test_observe_rollback_preserves_foreign_replacement_of_new_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_kind: str,
) -> None:
    capture = tmp_path / f"capture-new-artifact-foreign-{artifact_kind}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        f"new-artifact-foreign-{artifact_kind}",
        tmp_path / "library",
    )
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    original_write = scanner_module._atomic_write_text
    foreign = f"foreign-{artifact_kind}-replacement".encode()
    replacement_path: Path | None = None
    failed = False

    def fail_session_with_foreign_artifact(path: Path, content: str) -> None:
        nonlocal failed, replacement_path
        original_write(path, content)
        if path == paths.session_file and not failed:
            artifact_dir = (
                paths.sources if artifact_kind == "source" else paths.thumbnails
            )
            created = list(artifact_dir.iterdir())
            assert len(created) == 1
            replacement_path = created[0]
            replacement_path.unlink()
            replacement_path.write_bytes(foreign)
            failed = True
            raise OSError(
                f"synthetic session failure after foreign {artifact_kind} replacement"
            )

    monkeypatch.setattr(
        scanner_module,
        "_atomic_write_text",
        fail_session_with_foreign_artifact,
    )

    with pytest.raises(
        ScannerWorkflowError,
        match="failed to restore scanner observation after failure",
    ):
        observe_scan_folder(paths, capture)

    assert failed is True
    assert replacement_path is not None
    assert replacement_path.read_bytes() == foreign
    other_dir = paths.thumbnails if artifact_kind == "source" else paths.sources
    assert list(other_dir.iterdir()) == []
    assert {path: path.read_bytes() for path in before} == before


def test_observe_rejects_capture_membership_change_at_commit_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "capture-membership-change"
    capture.mkdir()
    _image(capture / "image00001.jpg", 90)
    paths = create_or_resume_scan_session(
        "book",
        "membership-change",
        tmp_path / "library",
    )
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    original_write = scanner_module._atomic_write_text
    injected = False

    def write_with_new_capture(path: Path, content: str) -> None:
        nonlocal injected
        original_write(path, content)
        if path == paths.findings_file and not injected:
            _image(capture / "image00002.jpg", 150)
            injected = True

    monkeypatch.setattr(
        scanner_module,
        "_atomic_write_text",
        write_with_new_capture,
    )

    with pytest.raises(
        ScannerWorkflowError,
        match="capture folder changed while Digitalisierer observed it",
    ):
        observe_scan_folder(paths, capture)

    assert injected is True
    for metadata_path, expected in before.items():
        assert metadata_path.read_bytes() == expected
    assert list(paths.sources.iterdir()) == []
    assert list(paths.thumbnails.iterdir()) == []

    monkeypatch.setattr(scanner_module, "_atomic_write_text", original_write)
    retried = observe_scan_folder(paths, capture)
    assert len(retried.imported_asset_ids) == 2
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    assert [asset["source_name"] for asset in session["assets"]] == [
        "image00001.jpg",
        "image00002.jpg",
    ]


def test_finalize_uses_verified_snapshot_during_transient_source_rewrite(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-finalize-source-snapshot"
    capture.mkdir()
    source = capture / "image00001.jpg"
    _image(source, 100)
    replacement = tmp_path / "replacement.jpg"
    _image(replacement, 210)
    paths = create_or_resume_scan_session(
        "book",
        "finalize-source-snapshot",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    preserved = paths.root / session["assets"][0]["preserved_path"]
    original_bytes = preserved.read_bytes()
    replacement_bytes = replacement.read_bytes()
    expected_digest = hashlib.sha256(original_bytes).digest()

    class _RacingPdf:
        name = "racing-pdf"

        def version(self) -> str:
            return "test"

        def __call__(self, images: list[Path], output: Path) -> None:
            assert len(images) == 1
            assert images[0] != preserved
            preserved.write_bytes(replacement_bytes)
            try:
                bound_bytes = images[0].read_bytes()
            finally:
                preserved.write_bytes(original_bytes)
            output.write_bytes(b"PDF:" + hashlib.sha256(bound_bytes).digest())

    exported = finalize_scan_session(
        paths,
        _FakeOcr(),
        pdf_builder=_RacingPdf(),
    )

    assert preserved.read_bytes() == original_bytes
    assert (exported.export_dir / "master.pdf").read_bytes() == (
        b"PDF:" + expected_digest
    )
    manifest = json.loads(
        (exported.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["active_order"][0]["sha256"] == hashlib.sha256(
        original_bytes
    ).hexdigest()



def test_finalize_verified_snapshot_is_readable_by_pdf_subprocess(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-finalize-subprocess-snapshot"
    capture.mkdir()
    source = capture / "image00001.jpg"
    _image(source, 100)
    paths = create_or_resume_scan_session(
        "book",
        "finalize-subprocess-snapshot",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    preserved = paths.root / session["assets"][0]["preserved_path"]
    original_bytes = preserved.read_bytes()
    expected_digest = hashlib.sha256(original_bytes).digest()

    class _SubprocessPdf:
        name = "subprocess-pdf"

        def version(self) -> str:
            return "test"

        def __call__(self, images: list[Path], output: Path) -> None:
            assert len(images) == 1
            assert images[0] != preserved
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import hashlib,pathlib,sys;"
                        "sys.stdout.buffer.write("
                        "hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).digest()"
                        ")"
                    ),
                    str(images[0]),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            output.write_bytes(b"PDF:" + completed.stdout)

    exported = finalize_scan_session(
        paths,
        _FakeOcr(),
        pdf_builder=_SubprocessPdf(),
    )

    assert (exported.export_dir / "master.pdf").read_bytes() == (
        b"PDF:" + expected_digest
    )


def test_finalize_export_identity_includes_pdf_builder_provenance(tmp_path: Path) -> None:
    capture = tmp_path / "capture-pdf-builder-identity"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "pdf-builder-identity",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)

    first = finalize_scan_session(
        paths,
        _FakeOcr(),
        pdf_builder=_FakePdf("1.0"),
    )
    second = finalize_scan_session(
        paths,
        _FakeOcr(),
        pdf_builder=_FakePdf("2.0"),
    )

    assert first.export_dir != second.export_dir
    first_manifest = json.loads(
        (first.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    second_manifest = json.loads(
        (second.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert first_manifest["pdf_builder"] == {
        "adapter": "fake-pdf",
        "version": "1.0",
    }
    assert second_manifest["pdf_builder"] == {
        "adapter": "fake-pdf",
        "version": "2.0",
    }

    with pytest.raises(ScannerWorkflowError, match="already exists"):
        finalize_scan_session(
            paths,
            _FakeOcr(),
            pdf_builder=_FakePdf("2.0"),
        )


def test_finalize_rejects_pdf_builder_without_provenance(tmp_path: Path) -> None:
    capture = tmp_path / "capture-unversioned-pdf-builder"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "unversioned-pdf-builder",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)

    def unversioned_builder(images: list[Path], output: Path) -> None:
        _fake_pdf(images, output)

    with pytest.raises(
        ScannerWorkflowError,
        match="PDF builder must expose stable name/version provenance",
    ):
        finalize_scan_session(
            paths,
            _FakeOcr(),
            pdf_builder=unversioned_builder,  # type: ignore[arg-type]
        )

    assert list(paths.exports.iterdir()) == []


def test_finalize_export_identity_includes_ocr_provenance(tmp_path: Path) -> None:
    capture = tmp_path / "capture-ocr-identity"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "ocr-identity",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)

    class _VersionedFakeOcr(_FakeOcr):
        def __init__(self, version_value: str) -> None:
            self.version_value = version_value

        def version(self) -> str:
            return self.version_value

    first = finalize_scan_session(
        paths,
        _VersionedFakeOcr("1.0"),
        pdf_builder=_fake_pdf,
    )
    second = finalize_scan_session(
        paths,
        _VersionedFakeOcr("2.0"),
        pdf_builder=_fake_pdf,
    )

    assert first.export_dir != second.export_dir
    first_manifest = json.loads(
        (first.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    second_manifest = json.loads(
        (second.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert first_manifest["ocr"]["version"] == "1.0"
    assert second_manifest["ocr"]["version"] == "2.0"

    with pytest.raises(ScannerWorkflowError, match="already exists"):
        finalize_scan_session(
            paths,
            _VersionedFakeOcr("2.0"),
            pdf_builder=_fake_pdf,
        )

@pytest.mark.parametrize("version_value", [None, ""])
def test_finalize_rejects_missing_ocr_provenance(
    tmp_path: Path,
    version_value: str | None,
) -> None:
    capture = tmp_path / "capture-missing-ocr-provenance"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "missing-ocr-provenance",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)

    class _MissingVersionOcr:
        name = "fake-ocr"

        def version(self) -> str | None:
            return version_value

        def searchable_pdf(
            self,
            master_pdf: Path,
            output_pdf: Path,
            sidecar_txt: Path,
            *,
            language: str,
        ) -> None:
            raise AssertionError("OCR execution must not start without provenance")

    with pytest.raises(
        ScannerWorkflowError,
        match="OCR backend must expose stable name/version provenance",
    ):
        finalize_scan_session(
            paths,
            _MissingVersionOcr(),  # type: ignore[arg-type]
            pdf_builder=_fake_pdf,
        )

    assert list(paths.exports.iterdir()) == []


def test_finalize_rejects_ocr_backend_without_version_method(tmp_path: Path) -> None:
    capture = tmp_path / "capture-unversioned-ocr"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "unversioned-ocr",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)

    class _UnversionedOcr:
        name = "fake-ocr"

        def searchable_pdf(
            self,
            master_pdf: Path,
            output_pdf: Path,
            sidecar_txt: Path,
            *,
            language: str,
        ) -> None:
            raise AssertionError("OCR execution must not start without provenance")

    with pytest.raises(
        ScannerWorkflowError,
        match="OCR backend must expose stable name/version provenance",
    ):
        finalize_scan_session(
            paths,
            _UnversionedOcr(),  # type: ignore[arg-type]
            pdf_builder=_fake_pdf,
        )

    assert list(paths.exports.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("schema_version", 999),
        ("kind", "foreign.scan-review"),
    ],
)
def test_finalize_rejects_incompatible_review_metadata_contract(
    tmp_path: Path,
    field: str,
    bad_value: object,
) -> None:
    capture = tmp_path / "capture-review-contract"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "review-contract",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    review[field] = bad_value
    paths.review_file.write_text(json.dumps(review) + "\n", encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="scan review metadata contract is incompatible",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("schema_version", 999, "scan findings metadata contract is incompatible"),
        ("kind", "foreign.scan-findings", "scan findings metadata contract is incompatible"),
        ("findings", "not-a-list", "scan findings must be a list"),
        ("findings", ["not-an-object"], "scan finding entry must be an object"),
        (
            "findings",
            [
                {
                    "kind": "blank-or-near-blank",
                    "message": "bad asset_ids shape",
                    "asset_ids": "not-a-list",
                    "confidence": None,
                    "evidence": [],
                }
            ],
            "scan finding entry has invalid shape",
        ),
    ],
)
def test_finalize_rejects_incompatible_findings_metadata_contract(
    tmp_path: Path,
    field: str,
    bad_value: object,
    message: str,
) -> None:
    capture = tmp_path / "capture-findings-contract"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "findings-contract",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    findings = json.loads(paths.findings_file.read_text(encoding="utf-8"))
    findings[field] = bad_value
    paths.findings_file.write_text(json.dumps(findings) + "\n", encoding="utf-8")

    with pytest.raises(ScannerWorkflowError, match=message):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


def test_review_paths_reject_foreign_session_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "capture-review-identity"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "review-identity",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    session["project_id"] = "foreign"
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")
    before_review = paths.review_file.read_bytes()
    writes: list[Path] = []
    original_write = scanner_module._atomic_write_text

    def record_write(path: Path, content: str) -> None:
        writes.append(path)
        original_write(path, content)

    monkeypatch.setattr(scanner_module, "_atomic_write_text", record_write)

    with pytest.raises(
        ScannerWorkflowError,
        match="identity/layout does not match the session path",
    ):
        load_processing_session(paths)
    with pytest.raises(
        ScannerWorkflowError,
        match="identity/layout does not match the session path",
    ):
        update_review_item(paths, asset_id, included=False)

    assert writes == []
    assert paths.review_file.read_bytes() == before_review


def test_observe_rejects_foreign_session_identity_before_publication(
    tmp_path: Path,
) -> None:
    first = tmp_path / "capture-first-identity"
    second = tmp_path / "capture-second-identity"
    first.mkdir()
    second.mkdir()
    _image(first / "image00001.jpg", 100)
    _image(second / "image00002.jpg", 140)
    paths = create_or_resume_scan_session(
        "book",
        "observe-identity",
        tmp_path / "library",
    )
    observe_scan_folder(paths, first)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    session["session_id"] = "foreign"
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")
    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }

    with pytest.raises(
        ScannerWorkflowError,
        match="identity/layout does not match the session path",
    ):
        observe_scan_folder(paths, second)

    for path, expected in before.items():
        assert path.read_bytes() == expected


def test_finalize_export_identity_includes_findings_snapshot(tmp_path: Path) -> None:
    capture = tmp_path / "capture-findings-identity"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "findings-identity",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]

    first = finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)
    first_manifest = json.loads(
        (first.export_dir / "manifest.json").read_text(encoding="utf-8")
    )

    findings = json.loads(paths.findings_file.read_text(encoding="utf-8"))
    findings["findings"].append(
        {
            "kind": "manual-note",
            "message": "reviewed finding",
            "asset_ids": [asset_id],
            "confidence": None,
            "evidence": ["manual"],
        }
    )
    paths.findings_file.write_text(json.dumps(findings) + "\n", encoding="utf-8")

    second = finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)
    second_manifest = json.loads(
        (second.export_dir / "manifest.json").read_text(encoding="utf-8")
    )

    assert first.export_dir != second.export_dir
    assert first_manifest["findings_sha256"] != second_manifest["findings_sha256"]
    assert second_manifest["findings"] == findings


def test_finalize_rejects_symlinked_exports_before_staging(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-export-symlink"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "export-symlink",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    outside = tmp_path / "outside-exports"
    outside.mkdir()
    paths.exports.rmdir()
    paths.exports.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ScannerWorkflowError,
        match="scanner exports directory must be a canonical direct child",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "metadata_name",
    ["session_file", "review_file", "findings_file"],
)
def test_processing_rejects_symlinked_metadata_snapshot(
    tmp_path: Path,
    metadata_name: str,
) -> None:
    capture = tmp_path / f"capture-metadata-symlink-{metadata_name}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        f"metadata-symlink-{metadata_name}",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    metadata = getattr(paths, metadata_name)
    outside = tmp_path / f"outside-{metadata_name}.json"
    outside.write_bytes(metadata.read_bytes())
    metadata.unlink()
    metadata.symlink_to(outside)

    with pytest.raises(
        ScannerWorkflowError,
        match="non-symlink regular file",
    ):
        load_processing_session(paths)
    with pytest.raises(
        ScannerWorkflowError,
        match="non-symlink regular file",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert metadata.is_symlink()
    assert list(paths.exports.iterdir()) == []


def test_processing_rejects_symlinked_sources_directory_alias(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture-sources-alias"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "sources-alias",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    aliased_sources = paths.root / "aliased-sources"
    paths.sources.rename(aliased_sources)
    paths.sources.symlink_to(aliased_sources.name, target_is_directory=True)

    with pytest.raises(
        ScannerWorkflowError,
        match="scanner sources directory must be a canonical direct child",
    ):
        load_processing_session(paths)
    with pytest.raises(
        ScannerWorkflowError,
        match="scanner sources directory must be a canonical direct child",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


@pytest.mark.parametrize("escape_kind", ["parent", "absolute", "symlink"])
def test_processing_rejects_preserved_source_path_escape(
    tmp_path: Path,
    escape_kind: str,
) -> None:
    capture = tmp_path / "capture-path-escape"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        f"path-escape-{escape_kind}",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    asset = session["assets"][0]
    outside = paths.root.parent / f"outside-{escape_kind}.jpg"
    outside.write_bytes((paths.root / asset["preserved_path"]).read_bytes())

    if escape_kind == "parent":
        asset["preserved_path"] = f"../{outside.name}"
    elif escape_kind == "absolute":
        asset["preserved_path"] = str(outside.resolve())
    else:
        link = paths.sources / "escape.jpg"
        link.symlink_to(outside)
        asset["preserved_path"] = "sources/escape.jpg"

    asset["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="preserved scanner source path is outside session sources",
    ):
        load_processing_session(paths)
    with pytest.raises(
        ScannerWorkflowError,
        match="preserved scanner source path is outside session sources",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


@pytest.mark.parametrize("alias_kind", ["wrong-name", "symlink-alias"])
def test_processing_rejects_preserved_source_alias(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    capture = tmp_path / "capture-path-alias"
    capture.mkdir()
    _image(capture / "image00001.jpg", 70)
    _image(capture / "image00002.jpg", 170)
    paths = create_or_resume_scan_session(
        "book",
        f"path-alias-{alias_kind}",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    first, second = session["assets"]

    if alias_kind == "wrong-name":
        first["preserved_path"] = second["preserved_path"]
    else:
        first_path = paths.root / first["preserved_path"]
        second_path = paths.root / second["preserved_path"]
        first_path.unlink()
        first_path.symlink_to(second_path.name)

    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="preserved scanner source path is not canonical",
    ):
        load_processing_session(paths)
    with pytest.raises(
        ScannerWorkflowError,
        match="preserved scanner source path is not canonical",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("project_id", "other-project"),
        ("session_id", "other-session"),
        ("layout", "digitalisierer.scan-session.v999"),
        ("kind", "foreign.scan-session"),
        ("schema_version", 999),
    ],
)
def test_finalize_rejects_session_identity_or_layout_mismatch(
    tmp_path: Path,
    field: str,
    bad_value: object,
) -> None:
    capture = tmp_path / "capture-session-identity"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "identity",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    session[field] = bad_value
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="identity/layout does not match the session path",
    ):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


def test_natural_key_uses_original_filename_as_deterministic_tie_breaker() -> None:
    assert scanner_module._natural_key(Path("Page01.JPG")) < scanner_module._natural_key(
        Path("page1.jpg")
    )


@pytest.mark.parametrize(
    "confidence",
    [-0.1, 1.1, float("nan"), float("inf"), 10**1000],
)
def test_finalize_rejects_invalid_finding_confidence(
    tmp_path: Path,
    confidence: float | int,
) -> None:
    capture = tmp_path / "capture-bad-confidence"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "bad-confidence",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    findings = json.loads(paths.findings_file.read_text(encoding="utf-8"))
    findings["findings"] = [
        {
            "kind": "manual",
            "message": "invalid confidence",
            "asset_ids": [observed.imported_asset_ids[0]],
            "confidence": confidence,
            "evidence": ["test"],
        }
    ]
    paths.findings_file.write_text(json.dumps(findings) + "\n", encoding="utf-8")

    with pytest.raises(ScannerWorkflowError, match="invalid shape"):
        finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert list(paths.exports.iterdir()) == []


def test_observe_preserves_distinct_sources_when_generated_asset_ids_collide(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "asset-id-collision"
    capture.mkdir()
    first_source = capture / "page one.jpg"
    second_source = capture / "page-one.jpg"
    _image(first_source, 90)
    second_source.write_bytes(first_source.read_bytes())
    paths = create_or_resume_scan_session(
        "book",
        "asset-id-collision",
        tmp_path / "library",
    )

    first = observe_scan_folder(paths, capture)
    second = observe_scan_folder(paths, capture)

    assert len(first.imported_asset_ids) == 2
    assert len(set(first.imported_asset_ids)) == 2
    assert second.imported_asset_ids == ()
    assert second.skipped_asset_ids == first.imported_asset_ids
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    assert {asset["source_name"] for asset in session["assets"]} == {
        "page one.jpg",
        "page-one.jpg",
    }
    assert len(session["assets"]) == 2
    assert any(
        finding.kind == "near-duplicate"
        and set(finding.asset_ids) == set(first.imported_asset_ids)
        for finding in second.findings
    )


def test_observe_resume_repairs_recorded_preserved_source_and_thumbnail(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "resume-repair"
    capture.mkdir()
    source = capture / "page.jpg"
    _image(source, 110)
    paths = create_or_resume_scan_session(
        "book",
        "resume-repair",
        tmp_path / "library",
    )
    first = observe_scan_folder(paths, capture)
    asset_id = first.imported_asset_ids[0]
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    asset = session["assets"][0]
    preserved = paths.root / asset["preserved_path"]
    thumbnail = paths.root / asset["thumbnail_path"]
    preserved.unlink()
    thumbnail.unlink()

    with pytest.raises(
        ScannerWorkflowError,
        match="preserved scanner source must be a regular file",
    ):
        create_or_resume_scan_session(
            "book",
            "resume-repair",
            tmp_path / "library",
        )

    resumed_paths = create_or_resume_scan_session(
        "book",
        "resume-repair",
        tmp_path / "library",
        repairable_capture_sources=frozenset({str(source.resolve())}),
    )
    assert resumed_paths == paths
    resumed = observe_scan_folder(resumed_paths, capture)

    assert resumed.imported_asset_ids == ()
    assert resumed.skipped_asset_ids == (asset_id,)
    repaired = json.loads(paths.session_file.read_text(encoding="utf-8"))["assets"][0]
    assert preserved.read_bytes() == source.read_bytes()
    assert hashlib.sha256(preserved.read_bytes()).hexdigest() == repaired["sha256"]
    assert thumbnail.is_file()
    assert hashlib.sha256(thumbnail.read_bytes()).hexdigest() == repaired["thumbnail_sha256"]


def test_observe_resume_does_not_allow_missing_source_outside_selected_range(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "resume-repair-range"
    capture.mkdir()
    first_source = capture / "image00001.jpg"
    second_source = capture / "image00002.jpg"
    _image(first_source, 90)
    _image(second_source, 150)
    library = tmp_path / "library"
    paths = create_or_resume_scan_session(
        "book",
        "resume-repair-range",
        library,
    )
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    missing = paths.root / session["assets"][1]["preserved_path"]
    missing.unlink()

    with pytest.raises(
        ScannerWorkflowError,
        match="preserved scanner source must be a regular file",
    ):
        create_or_resume_scan_session(
            "book",
            "resume-repair-range",
            library,
            repairable_capture_sources=frozenset({str(first_source.resolve())}),
        )

    assert not missing.exists()


@pytest.mark.parametrize("thumbnail_existed", [True, False])
def test_observe_failure_restores_repaired_thumbnail_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thumbnail_existed: bool,
) -> None:
    capture = tmp_path / "resume-repair-rollback"
    capture.mkdir()
    source = capture / "page.jpg"
    _image(source, 110)
    paths = create_or_resume_scan_session(
        "book",
        "resume-repair-rollback",
        tmp_path / "library",
    )
    first = observe_scan_folder(paths, capture)
    asset_id = first.imported_asset_ids[0]
    session_before = json.loads(paths.session_file.read_text(encoding="utf-8"))
    recorded_thumbnail_sha = session_before["assets"][0]["thumbnail_sha256"]
    thumbnail = paths.root / session_before["assets"][0]["thumbnail_path"]
    if thumbnail_existed:
        thumbnail.write_bytes(b"preexisting-corrupt-thumbnail")
        thumbnail_preimage: bytes | None = thumbnail.read_bytes()
    else:
        thumbnail.unlink()
        thumbnail_preimage = None
    metadata_before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }

    original_thumbnail_writer = scanner_module._write_thumbnail
    original_atomic_write = scanner_module._atomic_write_text
    failed = False

    def renderer_with_changed_bytes(source_path: Path, target: Path) -> object:
        published = original_thumbnail_writer(source_path, target)
        target.write_bytes(target.read_bytes() + b"-renderer-drift")
        return published

    def fail_session_once(path: Path, content: str) -> None:
        nonlocal failed
        if path == paths.session_file and not failed:
            failed = True
            raise OSError("synthetic session write failure after thumbnail repair")
        original_atomic_write(path, content)

    monkeypatch.setattr(
        scanner_module,
        "_write_thumbnail",
        renderer_with_changed_bytes,
    )
    monkeypatch.setattr(scanner_module, "_atomic_write_text", fail_session_once)

    with pytest.raises(
        OSError,
        match="synthetic session write failure after thumbnail repair",
    ):
        observe_scan_folder(paths, capture)

    assert failed is True
    if thumbnail_preimage is None:
        assert not thumbnail.exists()
    else:
        assert thumbnail.read_bytes() == thumbnail_preimage
    assert {
        path: path.read_bytes() for path in metadata_before
    } == metadata_before
    assert not any(
        path.name.endswith(".rollback") for path in paths.thumbnails.iterdir()
    )

    monkeypatch.setattr(scanner_module, "_atomic_write_text", original_atomic_write)
    resumed = observe_scan_folder(paths, capture)

    assert resumed.imported_asset_ids == ()
    assert resumed.skipped_asset_ids == (asset_id,)
    repaired = json.loads(paths.session_file.read_text(encoding="utf-8"))["assets"][0]
    assert repaired["thumbnail_sha256"] != recorded_thumbnail_sha
    assert hashlib.sha256(thumbnail.read_bytes()).hexdigest() == repaired["thumbnail_sha256"]
    assert not any(
        path.name.endswith(".rollback") for path in paths.thumbnails.iterdir()
    )


def test_thumbnail_rollback_refuses_replaced_preimage_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "thumbnail-rollback-claim-race"
    capture.mkdir()
    source = capture / "page.jpg"
    _image(source, 110)
    paths = create_or_resume_scan_session(
        "book",
        "thumbnail-rollback-claim-race",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session_before = json.loads(paths.session_file.read_text(encoding="utf-8"))
    thumbnail = paths.root / session_before["assets"][0]["thumbnail_path"]
    recorded_thumbnail_sha = session_before["assets"][0]["thumbnail_sha256"]
    thumbnail.write_bytes(b"preexisting-corrupt-thumbnail")
    metadata_before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    foreign = b"foreign-rollback-preimage"
    original_thumbnail_writer = scanner_module._write_thumbnail
    original_atomic_write = scanner_module._atomic_write_text
    failed = False

    def replace_preimage_claim(source_path: Path, target: Path) -> object:
        published = original_thumbnail_writer(source_path, target)
        claims = list(paths.thumbnails.glob(".*.rollback-preimage"))
        assert len(claims) == 1
        claims[0].unlink()
        claims[0].write_bytes(foreign)
        return published

    def fail_session_once(path: Path, content: str) -> None:
        nonlocal failed
        if path == paths.session_file and not failed:
            failed = True
            raise OSError("synthetic session failure with replaced rollback claim")
        original_atomic_write(path, content)

    monkeypatch.setattr(scanner_module, "_write_thumbnail", replace_preimage_claim)
    monkeypatch.setattr(scanner_module, "_atomic_write_text", fail_session_once)

    with pytest.raises(
        ScannerWorkflowError,
        match="failed to restore scanner observation after failure",
    ):
        observe_scan_folder(paths, capture)

    assert failed is True
    assert thumbnail.read_bytes() != foreign
    assert hashlib.sha256(thumbnail.read_bytes()).hexdigest() == recorded_thumbnail_sha
    assert {path: path.read_bytes() for path in metadata_before} == metadata_before
    claims = list(paths.thumbnails.glob(".*.rollback-preimage"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == foreign


def test_thumbnail_cleanup_does_not_delete_replaced_preimage_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "thumbnail-cleanup-claim-race"
    capture.mkdir()
    source = capture / "page.jpg"
    _image(source, 110)
    paths = create_or_resume_scan_session(
        "book",
        "thumbnail-cleanup-claim-race",
        tmp_path / "library",
    )
    first = observe_scan_folder(paths, capture)
    asset_id = first.imported_asset_ids[0]
    session_before = json.loads(paths.session_file.read_text(encoding="utf-8"))
    thumbnail = paths.root / session_before["assets"][0]["thumbnail_path"]
    thumbnail.write_bytes(b"preexisting-corrupt-thumbnail")
    foreign = b"foreign-success-cleanup-preimage"
    original_atomic_write = scanner_module._atomic_write_text
    injected = False

    def replace_claim_before_session_commit(path: Path, content: str) -> None:
        nonlocal injected
        if path == paths.session_file and not injected:
            claims = list(paths.thumbnails.glob(".*.rollback-preimage"))
            assert len(claims) == 1
            claims[0].unlink()
            claims[0].write_bytes(foreign)
            injected = True
        original_atomic_write(path, content)

    monkeypatch.setattr(
        scanner_module,
        "_atomic_write_text",
        replace_claim_before_session_commit,
    )
    resumed = observe_scan_folder(paths, capture)

    assert injected is True
    assert resumed.imported_asset_ids == ()
    assert resumed.skipped_asset_ids == (asset_id,)
    repaired = json.loads(paths.session_file.read_text(encoding="utf-8"))["assets"][0]
    assert hashlib.sha256(thumbnail.read_bytes()).hexdigest() == repaired["thumbnail_sha256"]
    claims = list(paths.thumbnails.glob(".*.rollback-preimage"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == foreign


@pytest.mark.parametrize(
    ("metadata_name", "content", "message"),
    [
        ("review_file", "{broken\n", "scanner session JSON is invalid"),
        ("findings_file", "{broken\n", "scanner session JSON is invalid"),
        (
            "review_file",
            json.dumps(
                {
                    "schema_version": 999,
                    "kind": "digitalisierer.scan-review",
                    "items": {},
                }
            )
            + "\n",
            "scan review metadata contract is incompatible",
        ),
        (
            "findings_file",
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "foreign.scan-findings",
                    "findings": [],
                }
            )
            + "\n",
            "scan findings metadata contract is incompatible",
        ),
    ],
)
def test_resume_validates_existing_metadata_before_reporting_ready(
    tmp_path: Path,
    metadata_name: str,
    content: str,
    message: str,
) -> None:
    capture = tmp_path / f"invalid-{metadata_name}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", metadata_name, library)
    observe_scan_folder(paths, capture)

    target = getattr(paths, metadata_name)
    target.write_text(content, encoding="utf-8")
    session_before = paths.session_file.read_bytes()
    review_before = paths.review_file.read_bytes()
    findings_before = paths.findings_file.read_bytes()
    sources_before = {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    }
    thumbnails_before = {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    }

    with pytest.raises(ScannerWorkflowError, match=message):
        create_or_resume_scan_session("book", metadata_name, library)

    assert paths.session_file.read_bytes() == session_before
    assert paths.review_file.read_bytes() == review_before
    assert paths.findings_file.read_bytes() == findings_before
    assert {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    } == sources_before
    assert {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    } == thumbnails_before


@pytest.mark.parametrize(
    "metadata_name",
    ["session_file", "review_file", "findings_file"],
)
def test_resume_rejects_fifo_metadata_without_blocking(
    tmp_path: Path,
    metadata_name: str,
) -> None:
    capture = tmp_path / f"fifo-{metadata_name}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", metadata_name, library)
    observe_scan_folder(paths, capture)

    target = getattr(paths, metadata_name)
    target.unlink()
    os.mkfifo(target)
    errors: list[BaseException] = []

    def resume() -> None:
        try:
            create_or_resume_scan_session("book", metadata_name, library)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=resume, daemon=True)
    worker.start()
    worker.join(0.5)
    if worker.is_alive():
        # Unblock a regressed blocking FIFO reader so the test can fail cleanly.
        writer = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        worker.join(1.0)
        pytest.fail(f"resume blocked while opening FIFO metadata: {metadata_name}")

    assert len(errors) == 1
    assert isinstance(errors[0], ScannerWorkflowError)
    assert "non-symlink regular file" in str(errors[0])


@pytest.mark.parametrize(
    ("inconsistency", "message"),
    [
        ("missing-review-decision", "review state missing"),
        ("unknown-finding-asset", "scan finding references unknown asset"),
    ],
)
def test_resume_rejects_logically_inconsistent_metadata_before_ready(
    tmp_path: Path,
    inconsistency: str,
    message: str,
) -> None:
    capture = tmp_path / f"inconsistent-{inconsistency}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", inconsistency, library)
    observe_scan_folder(paths, capture)

    if inconsistency == "missing-review-decision":
        review = json.loads(paths.review_file.read_text(encoding="utf-8"))
        review["items"] = {}
        paths.review_file.write_text(json.dumps(review) + "\n", encoding="utf-8")
    else:
        findings = json.loads(paths.findings_file.read_text(encoding="utf-8"))
        findings["findings"].append(
            {
                "kind": "manual",
                "message": "orphan finding",
                "asset_ids": ["missing-asset"],
                "confidence": None,
                "evidence": ["test"],
            }
        )
        paths.findings_file.write_text(
            json.dumps(findings) + "\n",
            encoding="utf-8",
        )

    before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    sources_before = {path.name: path.read_bytes() for path in paths.sources.iterdir()}
    thumbnails_before = {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    }

    with pytest.raises(ScannerWorkflowError, match=message):
        create_or_resume_scan_session("book", inconsistency, library)

    assert {path: path.read_bytes() for path in before} == before
    assert {path.name: path.read_bytes() for path in paths.sources.iterdir()} == sources_before
    assert {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    } == thumbnails_before


@pytest.mark.parametrize(
    ("field_path", "bad_value", "message"),
    [
        ("source_name", None, "asset source_name is invalid"),
        ("capture_source", "", "asset capture_source is invalid"),
        ("sha256", "0" * 63, "source digest is invalid"),
        ("bytes", 0, "byte count is invalid"),
        ("thumbnail_path", "thumbnails/wrong.jpg", "thumbnail path is not canonical"),
        ("thumbnail_sha256", "0" * 63, "thumbnail digest is invalid"),
        ("image", None, "image metadata is invalid"),
        ("image.width", 0, "image metadata is invalid"),
        ("image.stddev", "bad", "image metadata is invalid"),
        ("image.average_hash", "0" * 255, "image metadata is invalid"),
    ],
)
def test_resume_and_observe_reject_incomplete_persisted_asset_records(
    tmp_path: Path,
    field_path: str,
    bad_value: object,
    message: str,
) -> None:
    capture = tmp_path / f"invalid-asset-{field_path.replace('.', '-')}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    library = tmp_path / "library"
    session_id = f"invalid-asset-{field_path.replace('.', '-')}"
    paths = create_or_resume_scan_session("book", session_id, library)
    observe_scan_folder(paths, capture)

    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    asset = session["assets"][0]
    if field_path.startswith("image."):
        image_field = field_path.split(".", 1)[1]
        asset["image"][image_field] = bad_value
    else:
        asset[field_path] = bad_value
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")

    metadata_before = {
        paths.session_file: paths.session_file.read_bytes(),
        paths.review_file: paths.review_file.read_bytes(),
        paths.findings_file: paths.findings_file.read_bytes(),
    }
    sources_before = {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    }
    thumbnails_before = {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    }

    with pytest.raises(ScannerWorkflowError, match=message):
        create_or_resume_scan_session("book", session_id, library)
    with pytest.raises(ScannerWorkflowError, match=message):
        observe_scan_folder(paths, capture)

    assert {path: path.read_bytes() for path in metadata_before} == metadata_before
    assert {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    } == sources_before
    assert {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    } == thumbnails_before


@pytest.mark.parametrize(
    ("missing_name", "preserved_name"),
    [
        ("review_file", "findings_file"),
        ("findings_file", "review_file"),
    ],
)
def test_resume_rejects_missing_metadata_for_populated_session_without_writes(
    tmp_path: Path,
    missing_name: str,
    preserved_name: str,
) -> None:
    capture = tmp_path / f"missing-{missing_name}"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", missing_name, library)
    observe_scan_folder(paths, capture)

    missing_path = getattr(paths, missing_name)
    preserved_path = getattr(paths, preserved_name)
    session_before = paths.session_file.read_bytes()
    preserved_metadata_before = preserved_path.read_bytes()
    sources_before = {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    }
    thumbnails_before = {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    }
    missing_path.unlink()

    with pytest.raises(
        ScannerWorkflowError,
        match="metadata is missing for populated session",
    ):
        create_or_resume_scan_session("book", missing_name, library)

    assert not missing_path.exists()
    assert paths.session_file.read_bytes() == session_before
    assert preserved_path.read_bytes() == preserved_metadata_before
    assert {
        path.name: path.read_bytes() for path in paths.sources.iterdir()
    } == sources_before
    assert {
        path.name: path.read_bytes() for path in paths.thumbnails.iterdir()
    } == thumbnails_before


@pytest.mark.parametrize(
    ("missing_name", "expected_kind", "payload_key"),
    [
        ("review_file", "digitalisierer.scan-review", "items"),
        ("findings_file", "digitalisierer.scan-findings", "findings"),
    ],
)
def test_resume_recreates_missing_metadata_for_empty_session(
    tmp_path: Path,
    missing_name: str,
    expected_kind: str,
    payload_key: str,
) -> None:
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", missing_name, library)
    target = getattr(paths, missing_name)
    target.unlink()

    resumed = create_or_resume_scan_session("book", missing_name, library)
    payload = json.loads(getattr(resumed, missing_name).read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["kind"] == expected_kind
    assert payload[payload_key] == ([] if payload_key == "findings" else {})


def test_resume_rejects_non_list_session_assets_before_metadata_repair(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    paths = create_or_resume_scan_session("book", "invalid-assets", library)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    session["assets"] = {}
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")
    paths.review_file.unlink()

    with pytest.raises(ScannerWorkflowError, match="session assets must be a list"):
        create_or_resume_scan_session("book", "invalid-assets", library)

    assert not paths.review_file.exists()


def test_finalize_uses_atomic_writer_for_report_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "atomic-export-metadata"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session(
        "book",
        "atomic-export-metadata",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    original_write = scanner_module._atomic_write_text
    written: list[Path] = []

    def recording_write(path: Path, content: str) -> None:
        written.append(path)
        original_write(path, content)

    monkeypatch.setattr(scanner_module, "_atomic_write_text", recording_write)
    exported = finalize_scan_session(paths, _FakeOcr(), pdf_builder=_fake_pdf)

    assert exported.export_dir.is_dir()
    assert [path.name for path in written] == ["report.txt", "manifest.json"]
