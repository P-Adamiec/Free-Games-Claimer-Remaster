"""Small dependency-free HTTP server for the localhost dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse


logger = logging.getLogger("fgc.gui")
STATIC_DIR = Path(__file__).resolve().parent / "static"


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        loop: asyncio.AbstractEventLoop,
        status_callback: Callable[[], Awaitable[dict]],
        config_callback: Callable[[], Awaitable[dict]],
        save_callback: Callable[[dict], Awaitable[dict]],
        setup_callback: Callable[[dict], Awaitable[dict]],
        run_callback: Callable[[list[str] | None], Awaitable[bool]],
    ) -> None:
        super().__init__(address, DashboardHandler)
        self.loop = loop
        self.status_callback = status_callback
        self.config_callback = config_callback
        self.save_callback = save_callback
        self.setup_callback = setup_callback
        self.run_callback = run_callback
        self.csrf_token = secrets.token_urlsafe(32)

    def await_result(self, awaitable: Awaitable[dict] | Awaitable[bool]):
        future = asyncio.run_coroutine_threadsafe(awaitable, self.loop)
        return future.result(timeout=15)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; font-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request size") from exc
        if length < 0 or length > 64 * 1024:
            raise ValueError("Request is too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        return payload

    def _csrf_ok(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-FGC-Token", ""),
            self.server.csrf_token,
        )

    def _serve_static(self, path: str) -> None:
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
            "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/assets/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
            "/assets/locales/en.json": ("locales/en.json", "application/json; charset=utf-8"),
            "/assets/locales/pt-BR.json": ("locales/pt-BR.json", "application/json; charset=utf-8"),
            "/assets/locales/es.json": ("locales/es.json", "application/json; charset=utf-8"),
            "/assets/icons/steam.svg": ("icons/steam.svg", "image/svg+xml"),
            "/assets/icons/epicgames.svg": ("icons/epicgames.svg", "image/svg+xml"),
            "/assets/icons/gogdotcom.svg": ("icons/gogdotcom.svg", "image/svg+xml"),
            "/assets/icons/ubisoft.svg": ("icons/ubisoft.svg", "image/svg+xml"),
            "/assets/icons/aliexpress.svg": ("icons/aliexpress.svg", "image/svg+xml"),
            "/assets/fonts/newsreader-latin.woff2": ("fonts/newsreader-latin.woff2", "font/woff2"),
            "/assets/fonts/jetbrains-mono-latin.woff2": ("fonts/jetbrains-mono-latin.woff2", "font/woff2"),
        }
        item = files.get(path)
        if item is None:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        filename, content_type = item
        try:
            body = (STATIC_DIR / filename).read_bytes()
            if filename == "index.html":
                body = body.replace(b"__FGC_TOKEN__", self.server.csrf_token.encode("ascii"))
            self._send(body, content_type)
        except OSError:
            self._json({"error": "Dashboard unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                self._json(self.server.await_result(self.server.status_callback()))
            elif path == "/api/config":
                self._json(self.server.await_result(self.server.config_callback()))
            else:
                self._serve_static(path)
        except Exception:
            logger.exception("Dashboard GET failed for %s", path)
            self._json({"error": "Internal dashboard error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._csrf_ok():
            self._json({"error": "Invalid session; refresh the page"}, HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self._read_json()
            if path == "/api/run":
                stores = payload.get("stores")
                if stores is not None and not isinstance(stores, list):
                    raise ValueError("Invalid store list")
                accepted = self.server.await_result(self.server.run_callback(stores))
                status = HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT
                self._json({"accepted": accepted}, status)
            elif path == "/api/config":
                result = self.server.await_result(self.server.save_callback(payload.get("values")))
                self._json(result)
            elif path == "/api/setup":
                result = self.server.await_result(self.server.setup_callback(payload.get("values")))
                self._json(result)
            else:
                self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._json(
                {"error": str(exc), "code": getattr(exc, "code", "error.invalidRequest")},
                HTTPStatus.BAD_REQUEST,
            )
        except Exception:
            logger.exception("Dashboard POST failed for %s", path)
            self._json({"error": "Internal dashboard error"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def start_dashboard(
    *,
    loop: asyncio.AbstractEventLoop,
    port: int,
    status_callback: Callable[[], Awaitable[dict]],
    config_callback: Callable[[], Awaitable[dict]],
    save_callback: Callable[[dict], Awaitable[dict]],
    setup_callback: Callable[[dict], Awaitable[dict]],
    run_callback: Callable[[list[str] | None], Awaitable[bool]],
) -> DashboardHTTPServer:
    """Start the dashboard server in a daemon thread."""
    server = DashboardHTTPServer(
        ("0.0.0.0", port),
        loop,
        status_callback,
        config_callback,
        save_callback,
        setup_callback,
        run_callback,
    )
    Thread(target=server.serve_forever, name="fgc-dashboard", daemon=True).start()
    logger.info("🖥️ Local dashboard ready at http://localhost:%s", port)
    return server
