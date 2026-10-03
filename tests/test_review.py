from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http import HTTPStatus
import json
import os
from threading import Barrier, Event
from typing import Any
from pathlib import Path
import socket
import tempfile
from urllib.parse import urlencode

from PIL import Image
import pytest

import digitalisierer.review as review_module
import digitalisierer.scanner as scanner_module
from digitalisierer.review import ReviewServer, build_review_server, render_review_html
from digitalisierer.scanner import (
    ScanSessionPaths,
    ScannerWorkflowError,
    create_or_resume_scan_session,
    load_processing_session,
    observe_scan_folder,
    review_item_snapshot,
    update_review_item,
)


def _review_host_header(review_server: object) -> str:
    server = review_server.server  # type: ignore[attr-defined]
    host_value, port_value = server.server_address[:2]
    host = (
        host_value.decode("ascii")
        if isinstance(host_value, bytes)
        else str(host_value)
    )
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{rendered_host}:{int(port_value)}"


def _post_review_form(
    review_server: object,
    fields: dict[str, str],
    *,
    body_suffix: str = "",
) -> bytes:
    body = (urlencode(fields) + body_suffix).encode("utf-8")
    host_header = _review_host_header(review_server)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            (
                "POST /save HTTP/1.0\r\n"
                f"Host: {host_header}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Content-Type: application/x-www-form-urlencoded\r\n"
                "\r\n"
            ).encode("ascii")
            + body
        )
        server = review_server.server  # type: ignore[attr-defined]
        handler = server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), server)
        handler_socket.close()

        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
        return response
    finally:
        client.close()
        handler_socket.close()


def _get_review_path(review_server: object, path: str) -> bytes:
    host_header = _review_host_header(review_server)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            (
                f"GET {path} HTTP/1.0\r\n"
                f"Host: {host_header}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        server = review_server.server  # type: ignore[attr-defined]
        handler = server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), server)
        handler_socket.close()
        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
        return response
    finally:
        client.close()
        handler_socket.close()


def test_review_server_supports_ipv6_loopback(tmp_path: Path) -> None:
    paths = create_or_resume_scan_session(
        "book",
        "ipv6-loopback",
        tmp_path / "library",
    )
    review_server = build_review_server(paths, host="::1", port=0)
    try:
        assert review_server.server.address_family == socket.AF_INET6
        assert review_server.url.startswith("http://[::1]:")
    finally:
        review_server.server.server_close()


def test_review_html_is_large_preview_and_escapes_source_names(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    image_path = capture / "page&one.jpg"
    Image.new("RGB", (100, 140), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observe_scan_folder(paths, capture)

    rendered = render_review_html(paths, csrf_token="token")

    assert "Scan-Review" in rendered
    assert "page&amp;one.jpg" in rendered
    assert 'name="csrf" value="token"' in rendered
    assert "max-height: 72vh" in rendered
    assert 'srcset="/source/' not in rendered
    assert 'href="/source/' in rendered
    assert "Original in voller Auflösung öffnen" in rendered


def test_review_html_renders_surrogateescaped_source_name_as_utf8(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "surrogate-name"
    capture.mkdir()
    source_name = b"page-\xff.jpg".decode("utf-8", errors="surrogateescape")
    image_path = capture / source_name
    Image.new("RGB", (100, 140), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session(
        "book",
        "surrogate-name",
        tmp_path / "library",
    )

    observe_scan_folder(paths, capture)
    persisted = json.loads(paths.session_file.read_text(encoding="utf-8"))
    assert persisted["assets"][0]["source_name"] == source_name

    rendered = render_review_html(paths, csrf_token="token")
    rendered_bytes = rendered.encode("utf-8")
    assert rendered_bytes.decode("utf-8") == rendered
    assert "page-\\xff.jpg" in rendered
    assert source_name not in rendered

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            (
                "GET / HTTP/1.0\r\n"
                f"Host: {_review_host_header(review_server)}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        handler = review_server.server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), review_server.server)
        handler_socket.close()

        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
        headers, _, body = response.partition(b"\r\n\r\n")
        assert b" 200 " in headers.splitlines()[0]
        decoded = body.decode("utf-8")
        assert "page-\\xff.jpg" in decoded
        persisted_after = json.loads(
            paths.session_file.read_text(encoding="utf-8")
        )
        assert persisted_after["assets"][0]["source_name"] == source_name
    finally:
        client.close()
        handler_socket.close()
        review_server.server.server_close()


def test_review_html_surfaces_session_wide_findings(tmp_path: Path) -> None:
    capture = tmp_path / "session-finding"
    capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        "session-finding",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    findings = json.loads(paths.findings_file.read_text(encoding="utf-8"))
    findings["findings"] = [
        {
            "kind": "session-warning",
            "message": "<rotate> the whole session",
            "asset_ids": [],
            "confidence": None,
            "evidence": ["manual"],
        }
    ]
    paths.findings_file.write_text(
        json.dumps(findings) + "\n",
        encoding="utf-8",
    )

    rendered = render_review_html(paths, csrf_token="token")

    assert "Hinweise zur Sitzung" in rendered
    assert "session-warning: &lt;rotate&gt; the whole session" in rendered
    assert rendered.count("session-warning:") == 1


def test_review_render_rejects_foreign_session_identity(tmp_path: Path) -> None:
    capture = tmp_path / "foreign-session"
    capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        "foreign-session",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session = json.loads(
        paths.session_file.read_text(encoding="utf-8")
    )
    session["project_id"] = "foreign"
    paths.session_file.write_text(
        json.dumps(session) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ScannerWorkflowError,
        match="identity/layout does not match the session path",
    ):
        render_review_html(paths, csrf_token="token")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 999}, "metadata contract is incompatible"),
        ({"kind": "foreign.scan-findings"}, "metadata contract is incompatible"),
        ({"findings": "not-a-list"}, "findings must be a list"),
        ({"findings": [{"kind": "", "message": "valid", "asset_ids": ["asset"], "confidence": None, "evidence": []}]}, "invalid shape"),
        ({"findings": [{"kind": "   ", "message": "valid", "asset_ids": ["asset"], "confidence": None, "evidence": []}]}, "invalid shape"),
        ({"findings": [{"kind": "manual-note", "message": "", "asset_ids": ["asset"], "confidence": None, "evidence": []}]}, "invalid shape"),
        ({"findings": [{"kind": "manual-note", "message": "   ", "asset_ids": ["asset"], "confidence": None, "evidence": []}]}, "invalid shape"),
        ({"findings": [{"kind": "manual-note", "message": "valid", "asset_ids": ["   "], "confidence": None, "evidence": []}]}, "invalid shape"),
        ({"findings": [{"kind": "manual-note", "message": "valid", "asset_ids": ["asset","asset"], "confidence": None, "evidence": []}]}, "invalid shape"),
    ],
)
def test_review_render_rejects_invalid_findings_contract(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    capture = tmp_path / "bad-findings"
    capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        "bad-findings",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    findings = json.loads(
        paths.findings_file.read_text(encoding="utf-8")
    )
    findings.update(mutation)
    paths.findings_file.write_text(
        json.dumps(findings) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ScannerWorkflowError, match=message):
        render_review_html(paths, csrf_token="token")


def test_review_rejects_findings_for_unknown_asset_before_post_mutation(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "unknown-finding"
    capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        "unknown-finding",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    findings = json.loads(
        paths.findings_file.read_text(encoding="utf-8")
    )
    findings["findings"] = [
        {
            "kind": "manual-note",
            "message": "foreign asset",
            "asset_ids": ["missing-asset"],
            "confidence": None,
            "evidence": ["manual"],
        }
    ]
    paths.findings_file.write_text(
        json.dumps(findings) + "\n",
        encoding="utf-8",
    )
    before = paths.review_file.read_bytes()
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        with pytest.raises(
            ScannerWorkflowError,
            match="references unknown asset",
        ):
            render_review_html(paths, csrf_token=review_server.csrf_token)

        response = _post_review_form(
            review_server,
            {
                "csrf": review_server.csrf_token,
                "asset_id": asset_id,
                "review_snapshot": _current_review_snapshot(paths, asset_id),
                "included": "1",
                "sequence": "1",
                "original_sequence": "1",
                "replacement_for": "",
                "original_replacement_for": "",
            },
        )
        assert b" 500 " in response.splitlines()[0]
        assert paths.review_file.read_bytes() == before
    finally:
        review_server.server.server_close()


def test_review_server_is_loopback_only(tmp_path: Path) -> None:
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    with pytest.raises(ValueError, match="loopback"):
        build_review_server(paths, host="0.0.0.0", port=0)

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        assert review_server.url.startswith("http://127.0.0.1:")
        assert review_server.csrf_token
    finally:
        review_server.server.server_close()


def test_review_server_serves_preserved_source_for_hidpi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    image_path = capture / "page.jpg"
    Image.new("RGB", (1200, 1800), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    expected = image_path.read_bytes()
    preserved_source = next(paths.sources.glob(f"{asset_id}.*"))
    original_read_bytes = Path.read_bytes

    review_server = build_review_server(paths, host="127.0.0.1", port=0)

    def reject_source_read_bytes(self: Path) -> bytes:
        if self == preserved_source:
            pytest.fail("review image responses must stream from file handles")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", reject_source_read_bytes)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            f"GET /source/{asset_id} HTTP/1.0\r\nHost: {_review_host_header(review_server)}\r\n\r\n".encode()
        )
        handler = review_server.server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), review_server.server)
        handler_socket.close()

        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
        headers, _, body = response.partition(b"\r\n\r\n")
        assert b" 200 " in headers.splitlines()[0]
        assert b"Content-Type: image/jpeg" in headers
        assert body == expected
    finally:
        client.close()
        handler_socket.close()
        review_server.server.server_close()


@pytest.mark.parametrize("escape_kind", ["parent", "absolute", "symlink", "wrong-name"])
def test_review_rejects_thumbnail_path_escape(
    tmp_path: Path,
    escape_kind: str,
) -> None:
    capture = tmp_path / "thumbnail-escape"
    capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        f"thumbnail-escape-{escape_kind}",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    asset = session["assets"][0]
    current = paths.root / asset["thumbnail_path"]
    outside = paths.root.parent / f"outside-thumbnail-{escape_kind}.jpg"
    outside.write_bytes(current.read_bytes())

    if escape_kind == "parent":
        asset["thumbnail_path"] = f"../{outside.name}"
    elif escape_kind == "absolute":
        asset["thumbnail_path"] = str(outside.resolve())
    elif escape_kind == "symlink":
        link = paths.thumbnails / f"{asset_id}.jpg"
        link.unlink()
        link.symlink_to(outside)
    else:
        wrong = paths.thumbnails / "wrong.jpg"
        wrong.write_bytes(current.read_bytes())
        asset["thumbnail_path"] = "thumbnails/wrong.jpg"

    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="thumbnail path|thumbnail must",
    ):
        render_review_html(paths, csrf_token="token")


def test_review_rejects_thumbnail_bytes_from_another_asset(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "thumbnail-byte-alias"
    capture.mkdir()
    for index, value in enumerate((60, 180), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session(
        "book",
        "thumbnail-byte-alias",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    first, second = observed.imported_asset_ids
    first_thumbnail = paths.thumbnails / f"{first}.jpg"
    second_thumbnail = paths.thumbnails / f"{second}.jpg"
    first_thumbnail.write_bytes(second_thumbnail.read_bytes())

    with pytest.raises(
        ScannerWorkflowError,
        match="thumbnail hash mismatch",
    ):
        render_review_html(paths, csrf_token="token")


def test_review_thumbnail_hash_rejects_symlink_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "thumbnail-hash-open-race"
    capture.mkdir()
    Image.new("RGB", (100, 140), color=(80, 80, 80)).save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        "thumbnail-hash-open-race",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    thumbnail = paths.thumbnails / f"{asset_id}.jpg"
    outside = tmp_path / "outside-thumbnail.jpg"
    outside.write_bytes(thumbnail.read_bytes())
    original_open = os.open
    raced = False

    def raced_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if os.fspath(path) == os.fspath(thumbnail) and not raced:
            raced = True
            thumbnail.unlink()
            thumbnail.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)

    with pytest.raises(
        ScannerWorkflowError,
        match="review asset must be a non-symlink regular file",
    ):
        render_review_html(paths, csrf_token="token")

    assert raced is True
    assert thumbnail.is_symlink()


@pytest.mark.parametrize("replacement_kind", ["regular", "symlink"])
def test_review_thumbnail_revalidates_opened_descriptor_after_path_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    capture = tmp_path / f"thumbnail-open-race-{replacement_kind}"
    capture.mkdir()
    Image.new("RGB", (100, 140), color=(80, 80, 80)).save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        f"thumbnail-open-race-{replacement_kind}",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    replacement = tmp_path / "unverified-thumbnail.jpg"
    replacement.write_bytes(b"unverified-thumbnail-bytes")

    original_resolver = review_module._review_asset_path
    raced = False

    def replace_after_resolution(
        scan_paths: ScanSessionPaths,
        requested_asset_id: str,
        *,
        kind: str,
    ) -> tuple[Path, str] | None:
        nonlocal raced
        resolved = original_resolver(
            scan_paths,
            requested_asset_id,
            kind=kind,
        )
        if (
            resolved is not None
            and kind == "thumbnail"
            and requested_asset_id == asset_id
            and not raced
        ):
            raced = True
            target, expected_sha256 = resolved
            target.unlink()
            if replacement_kind == "regular":
                target.write_bytes(replacement.read_bytes())
            else:
                target.symlink_to(replacement)
            return target, expected_sha256
        return resolved

    monkeypatch.setattr(review_module, "_review_asset_path", replace_after_resolution)
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        response = _get_review_path(review_server, f"/thumbnail/{asset_id}")
        assert raced is True
        assert b" 500 " in response.splitlines()[0]
        assert b"unverified-thumbnail-bytes" not in response
    finally:
        review_server.server.server_close()


def test_review_rejects_thumbnail_symlink_to_another_asset(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "thumbnail-alias"
    capture.mkdir()
    for index, value in enumerate((60, 180), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session(
        "book",
        "thumbnail-alias",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    first, second = observed.imported_asset_ids
    first_thumbnail = paths.thumbnails / f"{first}.jpg"
    second_thumbnail = paths.thumbnails / f"{second}.jpg"
    first_thumbnail.unlink()
    first_thumbnail.symlink_to(second_thumbnail.name)

    with pytest.raises(
        ScannerWorkflowError,
        match="thumbnail path must not be a symlink",
    ):
        render_review_html(paths, csrf_token="token")


@pytest.mark.parametrize("alias_kind", ["wrong-name", "symlink-alias"])
def test_review_rejects_preserved_source_alias(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    capture = tmp_path / "source-alias"
    capture.mkdir()
    for index, value in enumerate((60, 180), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session(
        "book",
        f"source-alias-{alias_kind}",
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
        render_review_html(paths, csrf_token="token")


def test_review_rejects_invalid_source_name_instead_of_hiding_asset(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "invalid-source-name"
    capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session(
        "book",
        "invalid-source-name",
        tmp_path / "library",
    )
    observe_scan_folder(paths, capture)
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    asset_id = session["assets"][0]["asset_id"]
    session["assets"][0]["source_name"] = None
    paths.session_file.write_text(json.dumps(session) + "\n", encoding="utf-8")

    with pytest.raises(
        ScannerWorkflowError,
        match="asset source_name is invalid",
    ):
        load_processing_session(paths)
    with pytest.raises(
        ScannerWorkflowError,
        match="asset source_name is invalid",
    ):
        render_review_html(paths, csrf_token="token")

    assert asset_id


def test_review_replacement_defaults_to_replaced_page_position(tmp_path: Path) -> None:
    capture = tmp_path / "replacement-default"
    capture.mkdir()
    for index, value in enumerate((40, 100, 180), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session("book", "replacement", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, second, replacement = observed.imported_asset_ids
    update_review_item(paths, first, included=False)

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        response = _post_review_form(
            review_server,
            {
                "csrf": review_server.csrf_token,
                "asset_id": replacement,
                "review_snapshot": _current_review_snapshot(paths, replacement),
                "included": "1",
                "sequence": "3",
                "original_sequence": "3",
                "replacement_for": first,
                "original_replacement_for": "",
            },
        )
        assert b" 303 " in response.splitlines()[0]
        processing = load_processing_session(paths)
        replacement_item = next(
            item for item in processing.items if item.asset.asset_id == replacement
        )
        assert replacement_item.sequence is None
        assert replacement_item.replacement_for == first
        assert [asset.asset_id for asset in processing.ordered_assets()] == [
            replacement,
            second,
        ]
    finally:
        review_server.server.server_close()


def test_review_replacement_preserves_explicit_position_change(tmp_path: Path) -> None:
    capture = tmp_path / "replacement-explicit"
    capture.mkdir()
    for index, value in enumerate((40, 100, 180), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session("book", "replacement-explicit", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    first, second, replacement = observed.imported_asset_ids
    update_review_item(paths, first, included=False)

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        response = _post_review_form(
            review_server,
            {
                "csrf": review_server.csrf_token,
                "asset_id": replacement,
                "review_snapshot": _current_review_snapshot(paths, replacement),
                "included": "1",
                "sequence": "20",
                "original_sequence": "3",
                "replacement_for": first,
                "original_replacement_for": "",
            },
        )
        assert b" 303 " in response.splitlines()[0]
        processing = load_processing_session(paths)
        replacement_item = next(
            item for item in processing.items if item.asset.asset_id == replacement
        )
        assert replacement_item.sequence == 20
        assert replacement_item.replacement_for == first
        assert [asset.asset_id for asset in processing.ordered_assets()] == [
            second,
            replacement,
        ]
    finally:
        review_server.server.server_close()

def test_review_server_refreshes_asset_maps_after_observe(tmp_path: Path) -> None:
    first_capture = tmp_path / "capture-first"
    second_capture = tmp_path / "capture-second"
    first_capture.mkdir()
    second_capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        first_capture / "page1.jpg",
        format="JPEG",
    )
    Image.new("RGB", (100, 140), color="gray").save(
        second_capture / "page2.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session("book", "live-review", tmp_path / "library")
    observe_scan_folder(paths, first_capture)
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        observed = observe_scan_folder(paths, second_capture)
        new_asset = observed.imported_asset_ids[0]

        client, handler_socket = socket.socketpair()
        try:
            client.sendall(
                f"GET /source/{new_asset} HTTP/1.0\r\nHost: {_review_host_header(review_server)}\r\n\r\n".encode()
            )
            server = review_server.server
            handler = server.RequestHandlerClass
            handler(handler_socket, ("127.0.0.1", 1), server)
            handler_socket.close()
            response = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
            assert b" 200 " in response.splitlines()[0]
        finally:
            client.close()
            handler_socket.close()

        response = _post_review_form(
            review_server,
            {
                "csrf": review_server.csrf_token,
                "asset_id": new_asset,
                "review_snapshot": _current_review_snapshot(paths, new_asset),
                "included": "1",
                "sequence": "2",
                "original_sequence": "2",
                "replacement_for": "",
                "original_replacement_for": "",
            },
        )
        assert b" 303 " in response.splitlines()[0]
    finally:
        review_server.server.server_close()

@pytest.mark.parametrize(
    "host_lines",
    [
        "Host: attacker.example\r\n",
        "Host: 127.0.0.1\r\nHost: attacker.example\r\n",
        "Host: [::1\r\n",
    ],
)
def test_review_server_rejects_untrusted_or_ambiguous_host_headers(
    tmp_path: Path,
    host_lines: str,
) -> None:
    paths = create_or_resume_scan_session("book", "host-check", tmp_path / "library")
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            (
                "GET / HTTP/1.0\r\n"
                + host_lines
                + "\r\n"
            ).encode("ascii")
        )
        server = review_server.server
        handler = server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), server)
        handler_socket.close()

        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
        status_line, _, body = response.partition(b"\r\n")
        assert b" 421 " in status_line
        assert review_server.csrf_token.encode("ascii") not in body
        assert b"Scan-Review" not in body
    finally:
        client.close()
        handler_socket.close()
        review_server.server.server_close()


def test_review_server_rejects_untrusted_host_before_post_mutation(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "host-post"
    capture.mkdir()
    Image.new("RGB", (100, 140), color="white").save(
        capture / "page.jpg",
        format="JPEG",
    )
    paths = create_or_resume_scan_session("book", "host-post", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    before = paths.review_file.read_bytes()
    review_server = build_review_server(paths, host="127.0.0.1", port=0)

    body = urlencode(
        {
            "csrf": review_server.csrf_token,
            "asset_id": asset_id,
            "review_snapshot": _current_review_snapshot(paths, asset_id),
            "included": "",
            "sequence": "1",
            "original_sequence": "1",
            "replacement_for": "",
            "original_replacement_for": "",
        }
    ).encode("utf-8")
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            (
                "POST /save HTTP/1.0\r\n"
                "Host: attacker.example\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Content-Type: application/x-www-form-urlencoded\r\n"
                "\r\n"
            ).encode("ascii")
            + body
        )
        server = review_server.server
        handler = server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), server)
        handler_socket.close()

        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
        assert b" 421 " in response.splitlines()[0]
        assert paths.review_file.read_bytes() == before
    finally:
        client.close()
        handler_socket.close()
        review_server.server.server_close()


@pytest.mark.parametrize(
    ("request_kind", "expected_thumbnail_checks"),
    [
        ("thumbnail", 1),
        ("source", 0),
    ],
)
def test_review_asset_request_validates_only_requested_thumbnail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_kind: str,
    expected_thumbnail_checks: int,
) -> None:
    capture = tmp_path / "requested-asset-validation"
    capture.mkdir()
    for index, value in enumerate((50, 120, 200), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session(
        "book",
        "requested-asset-validation",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    requested_asset = observed.imported_asset_ids[0]
    checked_assets: list[str] = []
    original = review_module._review_thumbnail_path

    def traced_thumbnail_path(
        scan_paths: ScanSessionPaths,
        asset_id: str,
        relative: str,
        expected_sha256: str,
    ) -> Path:
        checked_assets.append(asset_id)
        return original(scan_paths, asset_id, relative, expected_sha256)

    monkeypatch.setattr(review_module, "_review_thumbnail_path", traced_thumbnail_path)
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            (
                f"GET /{request_kind}/{requested_asset} HTTP/1.0\r\n"
                f"Host: {_review_host_header(review_server)}\r\n\r\n"
            ).encode()
        )
        server = review_server.server
        handler = server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), server)
        handler_socket.close()
        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk

        assert b" 200 " in response.splitlines()[0]
        assert checked_assets == (
            [requested_asset] if expected_thumbnail_checks else []
        )
    finally:
        client.close()
        handler_socket.close()
        review_server.server.server_close()


def test_review_save_does_not_hash_thumbnails_for_asset_existence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "save-requested-asset"
    capture.mkdir()
    for index, value in enumerate((70, 170), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session(
        "book",
        "save-requested-asset",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]

    def reject_thumbnail_validation(
        scan_paths: ScanSessionPaths,
        requested_asset_id: str,
        relative: str,
        expected_sha256: str,
    ) -> Path:
        pytest.fail(
            f"POST /save must not hash thumbnail for {requested_asset_id}"
        )

    monkeypatch.setattr(
        review_module,
        "_review_thumbnail_path",
        reject_thumbnail_validation,
    )
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        response = _post_review_form(
            review_server,
            {
                "csrf": review_server.csrf_token,
                "asset_id": asset_id,
                "review_snapshot": _current_review_snapshot(paths, asset_id),
                "included": "1",
                "sequence": "1",
                "original_sequence": "1",
                "replacement_for": "",
                "original_replacement_for": "",
            },
        )
        assert b" 303 " in response.splitlines()[0]
    finally:
        review_server.server.server_close()


def test_review_server_rejects_corrupted_preserved_source_bytes(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "source-byte-corruption"
    capture.mkdir()
    image_path = capture / "page.jpg"
    Image.new("RGB", (100, 140), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session(
        "book",
        "source-byte-corruption",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    preserved = paths.root / session["assets"][0]["preserved_path"]
    preserved.write_bytes(b"wrong-full-resolution-page")
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            f"GET /source/{asset_id} HTTP/1.0\r\nHost: {_review_host_header(review_server)}\r\n\r\n".encode()
        )
        server = review_server.server
        handler = server.RequestHandlerClass
        handler(handler_socket, ("127.0.0.1", 1), server)
        handler_socket.close()
        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
        assert b" 500 " in response.splitlines()[0]
        assert b"wrong-full-resolution-page" not in response
    finally:
        client.close()
        handler_socket.close()
        review_server.server.server_close()


def test_review_server_streams_verified_source_snapshot_after_in_place_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "source-post-verify-race"
    capture.mkdir()
    image_path = capture / "page.jpg"
    Image.new("RGB", (100, 140), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session(
        "book",
        "source-post-verify-race",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    preserved = paths.root / session["assets"][0]["preserved_path"]
    original_bytes = preserved.read_bytes()
    assert original_bytes
    raced_bytes = bytes((original_bytes[0] ^ 1,)) + original_bytes[1:]
    assert len(raced_bytes) == len(original_bytes)

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    handler: Any = review_server.server.RequestHandlerClass
    original_headers = handler._headers
    raced = False

    def mutate_after_verification(
        self: Any,
        status: Any,
        *,
        content_type: str,
        content_length: int,
    ) -> None:
        nonlocal raced
        if not raced and status == HTTPStatus.OK:
            preserved.write_bytes(raced_bytes)
            raced = True
        original_headers(
            self,
            status,
            content_type=content_type,
            content_length=content_length,
        )

    monkeypatch.setattr(handler, "_headers", mutate_after_verification)
    try:
        response = _get_review_path(review_server, f"/source/{asset_id}")
    finally:
        review_server.server.server_close()

    assert raced is True
    assert b" 200 " in response.splitlines()[0]
    _, body = response.split(b"\r\n\r\n", 1)
    assert body == original_bytes
    assert body != preserved.read_bytes()


def test_review_server_streams_source_above_sealed_snapshot_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "source-over-snapshot-budget"
    capture.mkdir()
    image_path = capture / "page.jpg"
    Image.new("RGB", (100, 140), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session(
        "book",
        "source-over-snapshot-budget",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    preserved = paths.root / session["assets"][0]["preserved_path"]
    original_bytes = preserved.read_bytes()
    assert original_bytes
    monkeypatch.setattr(
        review_module,
        "MAX_REVIEW_SOURCE_SNAPSHOT_BYTES",
        preserved.stat().st_size - 1,
        raising=False,
    )

    def reject_memfd(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("large review sources must not use sealed memfd snapshots")

    monkeypatch.setattr(review_module, "_sealed_memfd_snapshot", reject_memfd)

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        response = _get_review_path(review_server, f"/source/{asset_id}")
    finally:
        review_server.server.server_close()

    assert b" 200 " in response.splitlines()[0]
    _, body = response.split(b"\r\n\r\n", 1)
    assert body == original_bytes


def test_review_server_serializes_sealed_source_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "source-single-sealed-snapshot"
    capture.mkdir()
    image_path = capture / "page.jpg"
    Image.new("RGB", (100, 140), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session(
        "book",
        "source-single-sealed-snapshot",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]

    original_snapshot: Any = getattr(review_module, "_sealed_memfd_snapshot")
    first_snapshot_ready = Event()
    release_first_snapshot = Event()
    snapshot_calls = 0

    def blocking_snapshot(*args: Any, **kwargs: Any) -> int:
        nonlocal snapshot_calls
        descriptor = int(original_snapshot(*args, **kwargs))
        snapshot_calls += 1
        if snapshot_calls == 1:
            first_snapshot_ready.set()
            assert release_first_snapshot.wait(timeout=5)
        return descriptor

    monkeypatch.setattr(review_module, "_sealed_memfd_snapshot", blocking_snapshot)
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                _get_review_path,
                review_server,
                f"/source/{asset_id}",
            )
            assert first_snapshot_ready.wait(timeout=5)
            second = executor.submit(
                _get_review_path,
                review_server,
                f"/source/{asset_id}",
            )
            second_response = second.result(timeout=5)
            assert b" 503 " in second_response.splitlines()[0]
            release_first_snapshot.set()
            first_response = first.result(timeout=5)
    finally:
        release_first_snapshot.set()
        review_server.server.server_close()

    assert b" 200 " in first_response.splitlines()[0]
    assert snapshot_calls == 1


def test_review_replacement_choices_are_materialized_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = create_or_resume_scan_session(
        "book",
        "large-replacement-list",
        tmp_path / "library",
    )
    assets = [
        {
            "asset_id": f"asset-{index:04d}",
            "source_name": f"page-{index:04d}.jpg",
            "thumbnail_path": f"thumbnails/asset-{index:04d}.jpg",
            "image": {"width": 100, "height": 140},
        }
        for index in range(1000)
    ]
    review = {
        "schema_version": 1,
        "kind": "digitalisierer.scan-review",
        "items": {
            asset["asset_id"]: {
                "included": True,
                "sequence": index + 1,
                "replacement_for": None,
            }
            for index, asset in enumerate(assets)
        },
    }
    session = {
        "project_id": "book",
        "session_id": "large-replacement-list",
        "assets": assets,
    }
    findings = {
        "schema_version": 1,
        "kind": "digitalisierer.scan-findings",
        "findings": [],
    }
    monkeypatch.setattr(
        review_module,
        "load_review_state",
        lambda _paths: (session, review, findings, object()),
    )
    monkeypatch.setattr(
        review_module,
        "_review_asset_paths_from_session",
        lambda _paths, _session: ({}, {}),
    )

    rendered = render_review_html(paths, csrf_token="token")

    assert rendered.count('<option value="') == 1000
    assert rendered.count('list="replacement-assets"') == 1000
    assert rendered.count('id="replacement-assets"') == 1
    assert '<select name="replacement_for">' not in rendered


def test_review_rejects_unknown_replacement_from_free_text_input(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "replacement-free-text"
    capture.mkdir()
    for index, value in enumerate((40, 180), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg",
            format="JPEG",
        )
    paths = create_or_resume_scan_session(
        "book",
        "replacement-free-text",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    before = paths.review_file.read_bytes()

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    try:
        response = _post_review_form(
            review_server,
            {
                "csrf": review_server.csrf_token,
                "asset_id": asset_id,
                "review_snapshot": _current_review_snapshot(paths, asset_id),
                "included": "1",
                "sequence": "1",
                "original_sequence": "1",
                "replacement_for": "missing-asset",
                "original_replacement_for": "",
            },
        )
        assert b" 400 " in response.splitlines()[0]
        assert paths.review_file.read_bytes() == before
    finally:
        review_server.server.server_close()



def _current_review_snapshot(paths: ScanSessionPaths, asset_id: str) -> str:
    review = json.loads(paths.review_file.read_text(encoding="utf-8"))
    return review_item_snapshot(asset_id, review["items"][asset_id])


class _ReviewForms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.current = {}
        elif tag == "input" and self.current is not None:
            name = values.get("name")
            if name is not None:
                if values.get("type") == "checkbox" and "checked" not in values:
                    return
                self.current[name] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def _rendered_form(
    paths: ScanSessionPaths, server: ReviewServer, asset_id: str,
) -> dict[str, str]:
    parser = _ReviewForms()
    parser.feed(render_review_html(paths, csrf_token=server.csrf_token))
    matches = [form for form in parser.forms if form.get("asset_id") == asset_id]
    assert len(matches) == 1
    assert len(matches[0]["review_snapshot"]) == 64
    return matches[0]


def _stale_form_session(tmp_path: Path) -> tuple[ScanSessionPaths, tuple[str, ...]]:
    capture = tmp_path / "stale-forms"
    capture.mkdir()
    for index, value in enumerate((40, 100, 180), start=1):
        Image.new("RGB", (100, 140), color=(value, value, value)).save(
            capture / f"page{index}.jpg", format="JPEG",
        )
    paths = create_or_resume_scan_session("book", "stale-forms", tmp_path / "library")
    return paths, observe_scan_folder(paths, capture).imported_asset_ids


@pytest.mark.parametrize("changed_field", ["included", "sequence", "replacement_for"])
def test_stale_review_form_preserves_every_current_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_field: str,
) -> None:
    paths, (first, _, asset_id) = _stale_form_session(tmp_path)
    update_review_item(paths, first, included=False)
    server = build_review_server(paths)
    try:
        tab_a = _rendered_form(paths, server, asset_id)
        tab_b = _rendered_form(paths, server, asset_id)
        tab_a[changed_field] = {
            "included": "", "sequence": "30", "replacement_for": first,
        }[changed_field]
        tab_b["sequence" if changed_field != "sequence" else "included"] = (
            "40" if changed_field != "sequence" else ""
        )
        assert b" 303 " in _post_review_form(server, tab_a).splitlines()[0]
        before = paths.review_file.read_bytes()

        def reject_write(*args: Any, **kwargs: Any) -> None:
            pytest.fail("stale review submission must not write or roll back metadata")

        with monkeypatch.context() as patch:
            patch.setattr(scanner_module, "_atomic_write_text", reject_write)
            response = _post_review_form(server, tab_b)
        assert b" 409 " in response.splitlines()[0]
        assert b"Seite neu laden" in response
        assert paths.review_file.read_bytes() == before
        decision = json.loads(before)["items"][asset_id]
        assert decision[changed_field] == {
            "included": False, "sequence": 30, "replacement_for": first,
        }[changed_field]

        refreshed = _rendered_form(paths, server, asset_id)
        refreshed["sequence"] = "50"
        assert b" 303 " in _post_review_form(server, refreshed).splitlines()[0]
        current = json.loads(paths.review_file.read_text())["items"][asset_id]
        assert current["sequence"] == 50
        assert current["included"] == decision["included"]
        assert current["replacement_for"] == decision["replacement_for"]
    finally:
        server.server.server_close()


def test_review_snapshot_rechecked_after_post_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, (_, _, asset_id) = _stale_form_session(tmp_path)
    server = build_review_server(paths)
    saved: list[bytes] = []
    try:
        form = _rendered_form(paths, server, asset_id)
        form["sequence"] = "30"
        original = update_review_item

        def change_before_lock(*args: Any, **kwargs: Any) -> Any:
            original(paths, asset_id, included=False)
            saved.append(paths.review_file.read_bytes())
            return original(*args, **kwargs)

        monkeypatch.setattr(review_module, "update_review_item", change_before_lock)
        response = _post_review_form(server, form)
        assert b" 409 " in response.splitlines()[0]
        assert len(saved) == 1
        assert paths.review_file.read_bytes() == saved[0]
    finally:
        server.server.server_close()


@pytest.mark.parametrize("same_asset", [False, True])
def test_parallel_review_forms_compare_inside_update_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_asset: bool,
) -> None:
    paths, (first, second, _) = _stale_form_session(tmp_path)
    server = build_review_server(paths)
    try:
        form_a = _rendered_form(paths, server, first)
        form_b = _rendered_form(paths, server, first if same_asset else second)
        form_a.pop("included")
        form_b["sequence"] = "30"
        barrier = Barrier(2, timeout=5)
        original = update_review_item

        def simultaneous(*args: Any, **kwargs: Any) -> Any:
            barrier.wait()
            return original(*args, **kwargs)

        monkeypatch.setattr(review_module, "update_review_item", simultaneous)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_post_review_form, server, form) for form in (form_a, form_b)]
            statuses = [int(f.result(timeout=10).splitlines()[0].split()[1]) for f in futures]
        decisions = json.loads(paths.review_file.read_text())["items"]
        if same_asset:
            assert sorted(statuses) == [303, 409]
            assert (decisions[first]["included"], decisions[first]["sequence"]) == (
                (False, 1) if statuses[0] == 303 else (True, 30)
            )
        else:
            assert statuses == [303, 303]
            assert decisions[first]["included"] is False
            assert decisions[second]["sequence"] == 30
    finally:
        server.server.server_close()


@pytest.mark.parametrize("token_kind", ["missing", "empty", "invalid", "duplicate", "other-asset", "csrf"])
def test_review_form_requires_unambiguous_snapshot_and_csrf(
    tmp_path: Path, token_kind: str,
) -> None:
    paths, (first, second, _) = _stale_form_session(tmp_path)
    server = build_review_server(paths)
    try:
        form = _rendered_form(paths, server, first)
        before = paths.review_file.read_bytes()
        suffix = ""
        expected = 400
        if token_kind == "missing":
            del form["review_snapshot"]
        elif token_kind == "empty":
            form["review_snapshot"] = ""
        elif token_kind == "invalid":
            form["review_snapshot"] = "x" * 64
        elif token_kind == "duplicate":
            suffix = "&review_snapshot=" + form["review_snapshot"]
        elif token_kind == "other-asset":
            form["review_snapshot"] = _rendered_form(paths, server, second)["review_snapshot"]
            expected = 409
        else:
            form["csrf"] = "wrong-token"
            expected = 403
        form.pop("included")
        response = _post_review_form(server, form, body_suffix=suffix)
        assert int(response.splitlines()[0].split()[1]) == expected
        assert paths.review_file.read_bytes() == before
    finally:
        server.server.server_close()



def test_review_server_stream_snapshot_is_immutable_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "source-verified-snapshot-in-place"
    capture.mkdir()
    image_path = capture / "page.jpg"
    Image.new("RGB", (100, 140), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session(
        "book",
        "source-verified-snapshot-in-place",
        tmp_path / "library",
    )
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]
    session = json.loads(paths.session_file.read_text(encoding="utf-8"))
    preserved = paths.root / session["assets"][0]["preserved_path"]
    original_bytes = preserved.read_bytes()
    raced_bytes = bytes((original_bytes[0] ^ 1,)) + original_bytes[1:]

    captured_verified: list[Any] = []
    original_temporary_file = tempfile.TemporaryFile

    def tracking_temporary_file(*args: Any, **kwargs: Any) -> Any:
        handle = original_temporary_file(*args, **kwargs)
        captured_verified.append(handle)
        return handle

    monkeypatch.setattr(
        tempfile,
        "TemporaryFile",
        tracking_temporary_file,
    )
    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    handler: Any = review_server.server.RequestHandlerClass
    original_headers = handler._headers
    attacked = False

    def mutate_verified_snapshot(
        self: Any,
        status: Any,
        *,
        content_type: str,
        content_length: int,
    ) -> None:
        nonlocal attacked
        if not attacked and status == HTTPStatus.OK and captured_verified:
            descriptor = captured_verified[-1].fileno()
            os.fchmod(descriptor, 0o600)
            writer = os.open(
                f"/proc/{os.getpid()}/fd/{descriptor}",
                os.O_WRONLY | os.O_CLOEXEC,
            )
            try:
                os.lseek(writer, 0, os.SEEK_SET)
                os.write(writer, raced_bytes)
                attacked = True
            finally:
                os.close(writer)
        original_headers(
            self,
            status,
            content_type=content_type,
            content_length=content_length,
        )

    monkeypatch.setattr(handler, "_headers", mutate_verified_snapshot)
    try:
        response = _get_review_path(review_server, f"/source/{asset_id}")
    finally:
        review_server.server.server_close()

    assert attacked is True
    assert b" 200 " in response.splitlines()[0]
    _, body = response.split(b"\r\n\r\n", 1)
    assert body == original_bytes
