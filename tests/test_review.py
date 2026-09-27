from pathlib import Path
import socket

from PIL import Image
import pytest

from digitalisierer.review import build_review_server, render_review_html
from digitalisierer.scanner import create_or_resume_scan_session, observe_scan_folder


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


def test_review_server_serves_preserved_source_for_hidpi(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    image_path = capture / "page.jpg"
    Image.new("RGB", (1200, 1800), color="white").save(image_path, format="JPEG")
    paths = create_or_resume_scan_session("book", "chapter", tmp_path / "library")
    observed = observe_scan_folder(paths, capture)
    asset_id = observed.imported_asset_ids[0]

    review_server = build_review_server(paths, host="127.0.0.1", port=0)
    client, handler_socket = socket.socketpair()
    try:
        client.sendall(
            f"GET /source/{asset_id} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode()
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
        assert body == image_path.read_bytes()
    finally:
        client.close()
        handler_socket.close()
        review_server.server.server_close()
