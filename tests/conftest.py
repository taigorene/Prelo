"""Shared fixtures.

Every app instance is created with explicit settings (see ``tests.utils.make_settings``) so the
suite never depends on ``PRELO_*`` variables, and runs its real lifespan (pandoc + Chromium).
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.utils import PIXEL_PNG, make_png, make_settings

ROOT_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT_DIR / "tests" / "fixtures"
EXAMPLES_DIR = ROOT_DIR / "examples"

ClientFactory = Callable[..., TestClient]


@pytest.fixture(scope="session")
def client_factory() -> Iterator[ClientFactory]:
    """Create (once per distinct settings override) a started ``TestClient``."""
    from app.main import create_app

    clients: dict[tuple[tuple[str, Any], ...], TestClient] = {}

    def factory(**overrides: Any) -> TestClient:
        key = tuple(sorted(overrides.items()))
        if key not in clients:
            app = create_app(make_settings(**overrides))
            client = TestClient(app, raise_server_exceptions=False)
            client.__enter__()  # runs the lifespan: starts the conversion engine
            clients[key] = client
        return clients[key]

    yield factory
    for client in clients.values():
        client.__exit__(None, None, None)


@pytest.fixture(scope="session")
def client(client_factory: ClientFactory) -> TestClient:
    """App with the default (production-like) settings."""
    return client_factory()


@pytest.fixture(scope="session")
def small_client(client_factory: ClientFactory) -> TestClient:
    """App with tiny upload/zip limits (about 10 KB upload, 5 zip entries, about 50 KB unzipped)."""
    return client_factory(max_upload_mb=0.01, zip_max_files=5, zip_max_uncompressed_mb=0.05)


@pytest.fixture(scope="session")
def private_client(client_factory: ClientFactory) -> TestClient:
    """App allowed to fetch from private networks, with small remote-image limits."""
    return client_factory(
        allow_private_networks=True, remote_image_max_mb=0.05, max_remote_images=3
    )


@pytest.fixture(scope="session")
def complete_md() -> bytes:
    return (FIXTURES_DIR / "complete.md").read_bytes()


@pytest.fixture(scope="session")
def latin1_md() -> bytes:
    return (FIXTURES_DIR / "latin1.md").read_bytes()


@pytest.fixture(scope="session")
def example_path() -> Path:
    return EXAMPLES_DIR / "exemplo-completo.md"


# --------------------------------------------------------------------------- local HTTP server


@dataclass
class ImageServer:
    """A tiny HTTP server on 127.0.0.1 that records every request path it receives."""

    port: int
    hits: list[str] = field(default_factory=list)

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"


def _numbered_png(path: str) -> bytes:
    """``/img/<n>.png`` -> a PNG whose colour depends on ``n`` (distinct bytes per path)."""
    number = int(path.rsplit("/", 1)[-1].split(".")[0])
    return make_png(5, 5, (number * 40 % 256, 120, 200))


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _RecordingServer

    def do_GET(self) -> None:
        self.server.record.hits.append(self.path)
        path = self.path.split("?", 1)[0]
        if path == "/page.html":
            self._reply(200, "text/html; charset=utf-8", b"<html><body>Not an image</body></html>")
        elif path == "/big.png":
            self._reply(200, "image/png", PIXEL_PNG + b"\0" * 200_000)
        elif path == "/redirect.png":
            self._redirect("/pixel.png")
        elif path == "/loop.png":
            self._redirect("/loop.png")
        elif path.startswith("/img/") and path.endswith(".png"):
            self._reply(200, "image/png", _numbered_png(path))
        elif path.endswith(".png"):
            self._reply(200, "image/png", PIXEL_PNG)
        else:
            self._reply(404, "text/plain", b"not found")

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Keep test output quiet."""


class _RecordingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    record: ImageServer


@pytest.fixture(scope="session")
def _image_server_session() -> Iterator[ImageServer]:
    server = _RecordingServer(("127.0.0.1", 0), _Handler)
    server.record = ImageServer(port=server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, name="image-server", daemon=True)
    thread.start()
    try:
        yield server.record
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def image_server(_image_server_session: ImageServer) -> ImageServer:
    """The local image server, with its request log cleared for the current test."""
    _image_server_session.hits.clear()
    return _image_server_session
