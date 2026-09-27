from __future__ import annotations

from dataclasses import dataclass
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .scanner import ScanSessionPaths, ScannerWorkflowError, update_review_item


MAX_FORM_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class ReviewServer:
    server: ThreadingHTTPServer
    csrf_token: str

    @property
    def url(self) -> str:
        host_value, port_value = self.server.server_address[:2]
        host = (
            host_value.decode("ascii")
            if isinstance(host_value, bytes)
            else str(host_value)
        )
        port = int(port_value)
        rendered_host = f"[{host}]" if ":" in host else host
        return f"http://{rendered_host}:{port}/"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScannerWorkflowError(f"cannot read review data: {path}") from exc
    if not isinstance(value, dict):
        raise ScannerWorkflowError(f"review data must be an object: {path}")
    return value


def _loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _finding_index(paths: ScanSessionPaths) -> dict[str, list[str]]:
    payload = _read_json(paths.findings_file)
    raw = payload.get("findings")
    index: dict[str, list[str]] = {}
    if not isinstance(raw, list):
        return index
    for finding in raw:
        if not isinstance(finding, dict):
            continue
        message = finding.get("message")
        kind = finding.get("kind")
        asset_ids = finding.get("asset_ids")
        if (
            not isinstance(message, str)
            or not isinstance(kind, str)
            or not isinstance(asset_ids, list)
        ):
            continue
        rendered = f"{kind}: {message}"
        for asset_id in asset_ids:
            if isinstance(asset_id, str):
                index.setdefault(asset_id, []).append(rendered)
    return index


def render_review_html(paths: ScanSessionPaths, *, csrf_token: str) -> str:
    session = _read_json(paths.session_file)
    review = _read_json(paths.review_file)
    assets = session.get("assets")
    decisions = review.get("items")
    if not isinstance(assets, list) or not isinstance(decisions, dict):
        raise ScannerWorkflowError("scan session/review state has invalid shape")

    findings = _finding_index(paths)
    known_ids = [
        str(asset["asset_id"])
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("asset_id"), str)
    ]
    cards: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("asset_id")
        source_name = asset.get("source_name")
        thumbnail = asset.get("thumbnail_path")
        if (
            not isinstance(asset_id, str)
            or not isinstance(source_name, str)
            or not isinstance(thumbnail, str)
        ):
            continue
        decision = decisions.get(asset_id)
        if not isinstance(decision, dict):
            continue
        included = decision.get("included") is True
        sequence = decision.get("sequence")
        replacement_for = decision.get("replacement_for")
        image = asset.get("image")
        dimensions = ""
        if isinstance(image, dict):
            dimensions = (
                f"{html.escape(str(image.get('width', '?')))} × "
                f"{html.escape(str(image.get('height', '?')))}"
            )
        finding_html = "".join(
            f"<li>{html.escape(message)}</li>"
            for message in findings.get(asset_id, [])
        )
        if not finding_html:
            finding_html = "<li>keine Hinweise</li>"
        options = ['<option value="">— kein Ersatz —</option>']
        for candidate in known_ids:
            if candidate == asset_id:
                continue
            selected = " selected" if candidate == replacement_for else ""
            options.append(
                f'<option value="{html.escape(candidate, quote=True)}"{selected}>'
                f"{html.escape(candidate)}</option>"
            )
        checked = " checked" if included else ""
        sequence_value = "" if sequence is None else html.escape(str(sequence))
        replacement_value = (
            "" if replacement_for is None else html.escape(replacement_for, quote=True)
        )
        cards.append(
            f"""
<article class="page-card">
  <a class="page-image-link" href="/source/{quote(asset_id, safe='')}" title="Original in voller Auflösung öffnen">
    <img loading="lazy" src="/thumbnail/{quote(asset_id, safe='')}" srcset="/source/{quote(asset_id, safe='')} 2x" alt="{html.escape(source_name, quote=True)}">
  </a>
  <div class="page-meta">
    <h2>{html.escape(source_name)}</h2>
    <p><code>{html.escape(asset_id)}</code> · {dimensions}</p>
    <ul>{finding_html}</ul>
    <form method="post" action="/save">
      <input type="hidden" name="csrf" value="{html.escape(csrf_token, quote=True)}">
      <input type="hidden" name="asset_id" value="{html.escape(asset_id, quote=True)}">
      <input type="hidden" name="original_sequence" value="{sequence_value}">
      <input type="hidden" name="original_replacement_for" value="{replacement_value}">
      <label><input type="checkbox" name="included" value="1"{checked}> enthalten</label>
      <label>Position <input name="sequence" inputmode="numeric" value="{sequence_value}"></label>
      <label>Ersatz für
        <select name="replacement_for">{''.join(options)}</select>
      </label>
      <button type="submit">Speichern</button>
    </form>
  </div>
</article>
"""
        )

    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Digitalisierer · Scan-Review</title>
<style>
:root {{ font-family: system-ui, sans-serif; font-size: 18px; color-scheme: light dark; }}
body {{ margin: 0 auto; max-width: 1500px; padding: 24px; }}
header {{ position: sticky; top: 0; padding: 12px 0; backdrop-filter: blur(12px); z-index: 2; }}
.grid {{ display: grid; gap: 24px; grid-template-columns: repeat(auto-fit,minmax(440px,1fr)); }}
.page-card {{ border: 1px solid #7777; border-radius: 14px; padding: 16px; display: grid; grid-template-columns: minmax(220px,42%) 1fr; gap: 18px; }}
.page-image-link {{ display: block; align-self: start; }}
.page-card img {{ width: 100%; max-height: 72vh; object-fit: contain; background: #8882; }}
.page-meta h2 {{ margin-top: 0; }}
form {{ display: grid; gap: 12px; }}
input, select, button {{ font: inherit; padding: 8px; }}
button {{ min-height: 48px; }}
code {{ word-break: break-all; }}
@media (max-width: 760px) {{ .page-card {{ grid-template-columns: 1fr; }} .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>Scan-Review</h1>
  <p>{html.escape(str(session.get('project_id')))} / {html.escape(str(session.get('session_id')))} · {len(cards)} Seiten</p>
</header>
<main class="grid">
{''.join(cards)}
</main>
</body>
</html>
"""


def _review_asset_paths(
    paths: ScanSessionPaths,
) -> tuple[dict[str, Path], dict[str, Path]]:
    session_payload = _read_json(paths.session_file)
    raw_assets = session_payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ScannerWorkflowError("scan session assets must be a list")
    thumbnail_by_id: dict[str, Path] = {}
    source_by_id: dict[str, Path] = {}
    for asset in raw_assets:
        if not isinstance(asset, dict):
            raise ScannerWorkflowError("scan asset record must be an object")
        asset_id = asset.get("asset_id")
        thumbnail = asset.get("thumbnail_path")
        preserved = asset.get("preserved_path")
        if not isinstance(asset_id, str) or not asset_id:
            raise ScannerWorkflowError("scan asset record has an invalid asset_id")
        if not isinstance(thumbnail, str) or not isinstance(preserved, str):
            raise ScannerWorkflowError("scan asset record has invalid review paths")
        thumbnail_by_id[asset_id] = paths.root / thumbnail
        source_by_id[asset_id] = paths.root / preserved
    return thumbnail_by_id, source_by_id


def _trusted_host_header(
    raw_values: list[str],
    *,
    configured_host: str,
    bound_host: str,
    bound_port: int,
) -> bool:
    if len(raw_values) != 1:
        return False
    raw_host = raw_values[0]
    if not raw_host or raw_host != raw_host.strip():
        return False
    try:
        parsed = urlparse(f"//{raw_host}")
        requested_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    allowed_hosts = {configured_host.lower(), bound_host.lower()}
    if parsed.hostname.lower() not in allowed_hosts:
        return False
    return requested_port is None or requested_port == bound_port


def build_review_server(
    paths: ScanSessionPaths,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ReviewServer:
    if not _loopback_host(host):
        raise ValueError("review UI may bind only to a loopback address")
    if isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("review UI port must be between 0 and 65535")
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "DigitalisiererReview/1"

        def _headers(
            self,
            status: HTTPStatus,
            *,
            content_type: str,
            content_length: int,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()

        def _request_host_is_trusted(self) -> bool:
            server_address = self.server.server_address
            if not isinstance(server_address, tuple) or len(server_address) < 2:
                return False
            bound_host_value, bound_port_value = server_address[:2]
            bound_host = (
                bound_host_value.decode("ascii")
                if isinstance(bound_host_value, bytes)
                else str(bound_host_value)
            )
            return _trusted_host_header(
                list(self.headers.get_all("Host", [])),
                configured_host=host,
                bound_host=bound_host,
                bound_port=int(bound_port_value),
            )

        def do_GET(self) -> None:  # noqa: N802
            if not self._request_host_is_trusted():
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST)
                return
            parsed = urlparse(self.path)
            if parsed.path == "/":
                payload = render_review_html(paths, csrf_token=csrf_token).encode(
                    "utf-8"
                )
                self._headers(
                    HTTPStatus.OK,
                    content_type="text/html; charset=utf-8",
                    content_length=len(payload),
                )
                self.wfile.write(payload)
                return
            try:
                thumbnail_by_id, source_by_id = _review_asset_paths(paths)
            except ScannerWorkflowError:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            for prefix, assets in (
                ("/thumbnail/", thumbnail_by_id),
                ("/source/", source_by_id),
            ):
                if not parsed.path.startswith(prefix):
                    continue
                asset_id = parsed.path[len(prefix) :]
                target = assets.get(asset_id)
                if target is None or not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = (
                    "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
                )
                with target.open("rb") as source:
                    content_length = os.fstat(source.fileno()).st_size
                    self._headers(
                        HTTPStatus.OK,
                        content_type=content_type,
                        content_length=content_length,
                    )
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        self.wfile.write(chunk)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self._request_host_is_trusted():
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST)
                return
            if urlparse(self.path).path != "/save":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if length <= 0 or length > MAX_FORM_BYTES:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            fields = parse_qs(
                self.rfile.read(length).decode("utf-8", errors="strict"),
                keep_blank_values=True,
            )
            if fields.get("csrf", [""])[0] != csrf_token:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            asset_id = fields.get("asset_id", [""])[0]
            try:
                thumbnail_by_id, _ = _review_asset_paths(paths)
            except ScannerWorkflowError:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if asset_id not in thumbnail_by_id:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            sequence_raw = fields.get("sequence", [""])[0].strip()
            original_sequence_raw = fields.get("original_sequence", [""])[0].strip()
            replacement = fields.get("replacement_for", [""])[0].strip()
            original_replacement = fields.get(
                "original_replacement_for", [""]
            )[0].strip()
            try:
                sequence = int(sequence_raw) if sequence_raw else None
                if (
                    replacement
                    and replacement != original_replacement
                    and sequence_raw == original_sequence_raw
                ):
                    sequence = None
                update_review_item(
                    paths,
                    asset_id,
                    included=fields.get("included", [""])[0] == "1",
                    sequence=sequence,
                    replacement_for=replacement or None,
                )
            except (ValueError, ScannerWorkflowError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    return ReviewServer(server=server, csrf_token=csrf_token)


def serve_review(
    paths: ScanSessionPaths,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> str:
    review_server = build_review_server(paths, host=host, port=port)
    url = review_server.url
    print(url, flush=True)
    try:
        review_server.server.serve_forever()
    finally:
        review_server.server.server_close()
    return url