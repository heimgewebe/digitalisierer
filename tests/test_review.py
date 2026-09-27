from pathlib import Path
import socket
from urllib.parse import urlencode

from PIL import Image
import pytest

from digitalisierer.review import build_review_server, render_review_html
from digitalisierer.scanner import (
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
    assert 'srcset="/source/' in rendered
    assert "Original in voller Auflösung öffnen" in rendered


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

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: pytest.fail("review image responses must stream from file handles"),
    )
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
