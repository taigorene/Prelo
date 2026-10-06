"""Brand assets: favicons, app icons, web app manifest and the logo used by the API docs."""

from __future__ import annotations

import json
import re
import struct
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.utils import make_settings

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# PNG color types: 2 = RGB (opaque), 6 = RGBA, 3 = palette (alpha only with a tRNS chunk).
PNG_RGB, PNG_PALETTE = 2, 3

SVG_ASSETS = (
    "/favicon.svg",
    "/brand/icon.svg",
    "/brand/icon-glyph.svg",
    "/brand/logo-light.svg",
    "/brand/logo-dark.svg",
)
PNG_ASSETS = {
    "/favicon-32.png": 32,
    "/brand/apple-touch-icon.png": 180,
    "/brand/icon-192.png": 192,
    "/brand/icon-512.png": 512,
    "/brand/icon-maskable-512.png": 512,
}


def _png_info(data: bytes) -> tuple[int, int, int, bool]:
    """Width, height, color type and whether the image carries transparency."""
    assert data.startswith(PNG_SIGNATURE), "not a PNG file"
    width, height, _depth, color_type = struct.unpack(">IIBB", data[16:26])
    has_trns = b"tRNS" in data[: data.find(b"IDAT")]
    transparent = color_type in (4, 6) or has_trns
    return width, height, color_type, transparent


@pytest.fixture(scope="module")
def manifest(client: TestClient) -> dict[str, Any]:
    response = client.get("/site.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    return response.json()


# --------------------------------------------------------------------------- vector assets


@pytest.mark.parametrize("path", SVG_ASSETS)
def test_svg_assets_are_served(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers["content-type"].startswith("image/svg+xml")
    svg = response.text
    assert "<title>Prelo</title>" in svg
    # Outlined paths only: the logo must render the same without the font installed.
    assert "<text" not in svg
    assert "<image" not in svg
    assert re.search(r'<svg[^>]*\bwidth="\d+"[^>]*\bheight="\d+"', svg), "intrinsic size"


def test_glyph_is_single_color(client: TestClient) -> None:
    svg = client.get("/brand/icon-glyph.svg").text
    assert set(re.findall(r'fill="([^"]+)"', svg)) == {"currentColor"}


# --------------------------------------------------------------------------- raster icons


@pytest.mark.parametrize(("path", "size"), sorted(PNG_ASSETS.items()))
def test_png_icons_are_served(client: TestClient, path: str, size: int) -> None:
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers["content-type"] == "image/png"
    width, height, _, _ = _png_info(response.content)
    assert (width, height) == (size, size), path


@pytest.mark.parametrize("path", ["/brand/apple-touch-icon.png", "/brand/icon-maskable-512.png"])
def test_full_bleed_icons_are_opaque(client: TestClient, path: str) -> None:
    *_, transparent = _png_info(client.get(path).content)
    assert not transparent, f"{path} must be opaque (the platform applies its own mask)"


def test_favicon_ico_has_standard_sizes(client: TestClient) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] in ("image/vnd.microsoft.icon", "image/x-icon")
    data = response.content
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = {(data[6 + 16 * i] or 256, data[7 + 16 * i] or 256) for i in range(count)}
    assert {(16, 16), (32, 32), (48, 48)} <= sizes


# --------------------------------------------------------------------------- manifest


def test_manifest_describes_the_app(manifest: dict[str, Any]) -> None:
    assert manifest["name"] == "Prelo"
    assert manifest["short_name"] == "Prelo"
    assert manifest["lang"] == "pt-BR"
    assert "Markdown" in manifest["description"]
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] in ("/", "./")
    assert re.fullmatch(r"#[0-9a-f]{6}", manifest["theme_color"])
    assert re.fullmatch(r"#[0-9a-f]{6}", manifest["background_color"])


def test_manifest_icons_are_served(client: TestClient, manifest: dict[str, Any]) -> None:
    icons = manifest["icons"]
    purposes = {icon.get("purpose", "any") for icon in icons}
    assert {"any", "maskable"} <= purposes
    png_sizes = set()
    for icon in icons:
        # Icon URLs are relative to the manifest, which is served at the UI root.
        response = client.get(f"/{icon['src'].removeprefix('./')}")
        assert response.status_code == 200, icon["src"]
        assert response.headers["content-type"].startswith(icon["type"])
        if icon["type"] == "image/png":
            width, height, _, _ = _png_info(response.content)
            assert icon["sizes"] == f"{width}x{height}", icon["src"]
            png_sizes.add(width)
    assert {192, 512} <= png_sizes


# --------------------------------------------------------------------------- wiring


def test_ui_links_icons_and_manifest(client: TestClient) -> None:
    html = client.get("/").text
    links = {
        (rel, href) for rel, href in re.findall(r'<link\s+rel="([^"]+)"[^>]*\shref="([^"]+)"', html)
    }
    assert ("icon", "favicon.svg") in links
    assert ("icon", "favicon-32.png") in links
    assert ("apple-touch-icon", "brand/apple-touch-icon.png") in links
    assert ("manifest", "site.webmanifest") in links
    assert re.search(r'<svg class="mark"[^>]*aria-hidden="true"', html), "header mark"
    assert 'class="mark-rule"' in html


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
def test_docs_pages_use_the_favicon(client: TestClient, page: str) -> None:
    html = client.get(page).text
    match = re.search(r'<link[^>]*rel="(?:shortcut )?icon"[^>]*href="([^"]+)"', html)
    assert match, f"{page} has no favicon link"
    assert match.group(1) == "/favicon.svg"
    assert client.get(match.group(1)).status_code == 200


def test_swagger_ui_shows_the_logo(client: TestClient) -> None:
    html = client.get("/docs").text
    header = re.search(r'<header class="prelo-brand">(.*?)</header>', html, re.S)
    assert header, "Swagger UI page has no brand header"
    assert 'href="/"' in header.group(1)
    logo = re.search(r'<img src="([^"]+)" alt="Prelo"', header.group(1))
    assert logo and logo.group(1) == "/brand/logo-light.svg"
    assert html.index("prelo-brand{") < html.index("</head>"), "brand style belongs in <head>"


def test_redoc_theme_is_valid_json(client: TestClient) -> None:
    html = client.get("/redoc").text
    theme = re.search(r"<redoc theme='([^']+)'", html)
    assert theme, "ReDoc theme attribute missing"
    assert "logo" in json.loads(theme.group(1))


def test_openapi_has_redoc_logo(client: TestClient) -> None:
    logo = client.get("/openapi.json").json()["info"]["x-logo"]
    assert logo["altText"] == "Prelo"
    assert logo["url"] == "/brand/logo-light.svg"
    response = client.get(logo["url"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_openapi_logo_follows_root_path() -> None:
    from app.main import create_app

    app = create_app(make_settings(root_path="/prelo"))
    logo = app.openapi()["info"]["x-logo"]
    assert logo["url"] == "/prelo/brand/logo-light.svg"
    assert logo["href"] == "/prelo/"
