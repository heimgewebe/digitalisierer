import hashlib
import json
import threading
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

def test_observe_resume_repairs_known_asset_missing_review_state(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _image(capture / "image00001.jpg", 100)
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    first = observe_scan_folder(paths, capture)
    asset_id = first.imported_asset_ids[0]

    review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    review["items"].pop(asset_id)
    paths.review_file.write_text(
        json.dumps(review, indent=2) + "\n",
        encoding="utf-8",
    )

    resumed = observe_scan_folder(paths, capture)
    processing = load_processing_session(paths)

    assert resumed.imported_asset_ids == ()
    assert resumed.skipped_asset_ids == (asset_id,)
    assert [asset.asset_id for asset in processing.ordered_assets()] == [asset_id]


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

    session_after_failure = json.loads(
        paths.session_file.read_text(encoding="utf-8")
    )
    assert session_after_failure["assets"] == []
    with pytest.raises(
        ScannerWorkflowError,
        match="review state contains assets missing from scan session",
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
