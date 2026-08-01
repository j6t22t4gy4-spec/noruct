"""Loopback-only, read-only gateway operator dashboard.

This intentionally projects Noruct-owned state instead of importing an
upstream gateway dashboard, session authority, authentication or profile
store.  It serves only a small static page and one JSON snapshot endpoint.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


SnapshotFactory = Callable[[], Mapping[str, object]]
ReadyCallback = Callable[[str, int], None]


def _page() -> bytes:
    return b"""<!doctype html><meta charset=utf-8><title>Noruct Gateway</title>
<style>body{margin:0;background:#0d1117;color:#e6edf3;font:15px ui-monospace,monospace;padding:32px}main{max-width:960px;margin:auto}h1{font-size:26px}pre{padding:20px;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:auto}</style>
<main><h1>Noruct gateway</h1><p>Local read-only operator projection.</p><pre id=status>Loading...</pre></main>
<script>async function load(){let r=await fetch('/api/status',{cache:'no-store'});document.querySelector('#status').textContent=JSON.stringify(await r.json(),null,2)}load();setInterval(load,5000)</script>"""


def serve_gateway_dashboard(
    *,
    snapshot: SnapshotFactory,
    port: int = 0,
    maximum_requests: int | None = None,
    on_ready: ReadyCallback | None = None,
) -> tuple[str, int]:
    """Serve a loopback-only dashboard until interrupted or request cap is met.

    ``maximum_requests`` exists for deterministic diagnostics/tests.  It is
    not a production routing or service-supervision mechanism.
    """

    if not 0 <= port <= 65535:
        raise ValueError("Gateway dashboard port must be between 0 and 65535")
    if maximum_requests is not None and not 1 <= maximum_requests <= 100_000:
        raise ValueError("Gateway dashboard maximum requests must be between 1 and 100000")
    served = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonlocal served
            if self.path == "/":
                body, content_type, status = _page(), "text/html; charset=utf-8", HTTPStatus.OK
            elif self.path == "/api/status":
                body = json.dumps(snapshot(), ensure_ascii=False, sort_keys=True).encode("utf-8")
                content_type, status = "application/json; charset=utf-8", HTTPStatus.OK
            else:
                body, content_type, status = b'{"error":"not_found"}', "application/json; charset=utf-8", HTTPStatus.NOT_FOUND
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            served += 1

        def log_message(self, _format: str, *_args: object) -> None:
            return

    # A dashboard request is local and tiny.  A single-threaded server keeps
    # the optional request cap deterministic and avoids turning this status
    # projection into a general concurrent web-service runtime.
    server = HTTPServer(("127.0.0.1", port), Handler)
    host, bound_port = server.server_address[:2]
    try:
        if on_ready is not None:
            on_ready(str(host), int(bound_port))
        while maximum_requests is None or served < maximum_requests:
            server.handle_request()
    finally:
        server.server_close()
    return str(host), int(bound_port)
