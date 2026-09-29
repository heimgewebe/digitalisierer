import json
from pathlib import Path
import socket
from urllib.parse import urlencode

from PIL import Image
import pytest

import digitalisierer.review as review_module
from digitalisierer.review import build_review_server, render_review_html
from digitalisierer.scanner import (
    ScanSessionPaths,
    ScannerWorkflowError,
    create_or_resume_scan_session,
    load_processing_session,
    observe_scan_folder,
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
) -> bytes:
    body = urlencode(fields).encode("utf-8")
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

    assert len(load_processing_session(paths).items) == 1
    with pytest.raises(
        ScannerWorkflowError,
        match="source_name is invalid for review",
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