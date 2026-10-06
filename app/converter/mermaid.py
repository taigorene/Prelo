"""Mermaid rendering and image rasterization in a locked-down Chromium page.

The page runs JavaScript (Mermaid needs it), so it is confined in layers: Mermaid runs
with ``securityLevel: 'strict'``; a Content-Security-Policy only allows the vendored
script, inline styles and ``data:``/``blob:`` images (no ``fetch``, XHR or WebSocket);
every HTTP request is aborted except the two served from a fake origin (an HTML shell
and ``mermaid.min.js``); WebSockets are never connected; and the browser itself sends
anything that escapes interception to a closed local proxy port (see ``browser``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from playwright.async_api import BrowserContext, Page, Route
from playwright.async_api import Error as PlaywrightError

from app.converter.browser import BrowserManager
from app.converter.resources import MAX_IMAGE_PIXELS

logger = logging.getLogger("prelo.converter")

ORIGIN = "http://mermaid.local"
SHELL_URL = f"{ORIGIN}/index.html"
SCRIPT_URL = f"{ORIGIN}/mermaid.min.js"
FONT_FAMILY = '"Inter Variable", "Noto Sans", "DejaVu Sans", "Liberation Sans", sans-serif'
# Background painted behind diagrams of dark themes (their text is light).
THEME_BACKGROUNDS = {"dark": "#1e1e1e"}
DIAGRAM_TIMEOUT = 30.0
RASTER_TIMEOUT = 30.0
RASTER_MAX_SIDE = 8000
# Gantt charts are laid out at least this wide (then scaled down) so date ticks fit.
GANTT_MIN_WIDTH = 960
SHELL_CSP = (
    f"default-src 'none'; script-src {ORIGIN}; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'"
)
JPEG_QUALITY = 90
_ERROR_CHARS = 300
_MAX_EXPECTED_TOKENS = 4
_PARSE_ERROR = re.compile(r"^Parse error on line (\d+):?$")
_EXPECTING = re.compile(r"^Expecting (.+), got (.+)$")
_UNKNOWN_DIAGRAM = re.compile(r"No diagram type detected|UnknownDiagramError", re.IGNORECASE)

_SHELL_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
html, body {{ margin: 0; padding: 0; background: transparent; }}
#raster {{ position: absolute; left: 0; top: 0; line-height: 0; }}
#raster img {{ display: block; }}
</style>
{script}
</head><body><div id="diagram"></div><div id="raster"></div></body></html>
"""

_INIT_JS = """
(cfg) => {
  document.body.style.width = cfg.width + 'px';
  if (!window.mermaid) return false;
  window.mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: cfg.theme,
    fontFamily: cfg.fontFamily,
    gantt: { useWidth: Math.max(cfg.width, cfg.ganttWidth) },
  });
  return true;
}
"""

_RENDER_JS = """
async ({ id, code, background }) => {
  const host = document.getElementById('diagram');
  try {
    const { svg } = await window.mermaid.render(id, code);
    host.innerHTML = svg;
    const el = host.querySelector('svg');
    if (!el) throw new Error('Mermaid did not produce an SVG');
    const box = el.viewBox && el.viewBox.baseVal;
    let width = box && box.width ? box.width : 0;
    let height = box && box.height ? box.height : 0;
    if (!width || !height) {
      const rect = el.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
    }
    el.setAttribute('width', String(width));
    el.setAttribute('height', String(height));
    el.style.maxWidth = '';
    if (background) el.style.backgroundColor = background;
    return { ok: true, svg: new XMLSerializer().serializeToString(el), width, height };
  } catch (error) {
    const leftover = document.getElementById('d' + id);
    if (leftover) leftover.remove();
    return { ok: false, error: String((error && error.message) || error) };
  } finally {
    host.innerHTML = '';
  }
}
"""

_RASTER_JS = """
async ({ src, scale, maxSide, maxPixels, width, height, fit }) => {
  const host = document.getElementById('raster');
  host.innerHTML = '';
  const img = new Image();
  img.src = src;
  await img.decode();
  // Raster images are decoded at their natural size (vectors are drawn at the final size).
  const vector = src.startsWith('data:image/svg+xml');
  if (!vector && img.naturalWidth * img.naturalHeight > maxPixels) {
    throw new Error('image too large');
  }
  let w = width || img.naturalWidth;
  let h = height || img.naturalHeight;
  if (!w || !h) { w = 300; h = 150; }
  if (fit) {
    const shrink = Math.min(1, fit[0] / w, fit[1] / h);
    w *= shrink;
    h *= shrink;
  }
  const factor = Math.min(scale, maxSide / Math.max(w, h));
  img.width = Math.max(1, Math.round(w * factor));
  img.height = Math.max(1, Math.round(h * factor));
  host.appendChild(img);
  await img.decode();
  return { width: w, height: h, pixelWidth: img.width, pixelHeight: img.height };
}
"""


@dataclass(frozen=True)
class MermaidDiagram:
    svg: str  # standalone SVG document (XML), with explicit width/height
    width: float  # CSS px
    height: float


@dataclass(frozen=True)
class MermaidFailure:
    message: str  # first line(s) of Mermaid's error, for the user


@dataclass(frozen=True)
class RasterImage:
    data: bytes
    media_type: str  # image/png or image/jpeg
    width: float  # displayed size in CSS px (before the raster scale factor)
    height: float


class MermaidRenderer:
    """Factory for per-job :class:`ScriptPage` instances."""

    def __init__(self, browser: BrowserManager, script: bytes | None) -> None:
        self._browser = browser
        self._script = script

    @property
    def available(self) -> bool:
        return self._script is not None

    @contextlib.asynccontextmanager
    async def page(self, *, theme: str, width_px: int) -> AsyncIterator[ScriptPage]:
        """A page ready to render diagrams ``width_px`` wide with the given theme."""
        async with self._browser.context(
            java_script_enabled=True,
            viewport={"width": max(width_px + 64, 1024), "height": 1024},
            device_scale_factor=1,
            service_workers="block",
            accept_downloads=False,
        ) as context:
            try:
                page = await self._open(context, theme=theme, width_px=width_px)
            except PlaywrightError as exc:
                await self._browser.raise_if_crashed(exc)
                raise
            yield ScriptPage(page, self._browser, theme=theme, mermaid_ready=self.available)

    async def _open(self, context: BrowserContext, *, theme: str, width_px: int) -> Page:
        script_tag = '<script src="/mermaid.min.js"></script>' if self._script else ""
        shell = _SHELL_HTML.format(script=script_tag).encode("utf-8")
        script = self._script

        async def handle(route: Route) -> None:
            url = route.request.url
            try:
                if url == SHELL_URL:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": SHELL_CSP},
                        body=shell,
                    )
                elif url == SCRIPT_URL and script is not None:
                    await route.fulfill(
                        status=200, content_type="text/javascript; charset=utf-8", body=script
                    )
                else:
                    await route.abort("blockedbyclient")
            except PlaywrightError:
                pass  # page already closed

        await context.route("**/*", handle)
        # A WebSocket route that never calls connect_to_server() keeps sockets offline.
        await context.route_web_socket("**/*", lambda _: None)
        page = await context.new_page()
        await page.goto(SHELL_URL, wait_until="load")
        await page.evaluate(
            _INIT_JS,
            {
                "width": width_px,
                "theme": theme,
                "fontFamily": FONT_FAMILY,
                "ganttWidth": GANTT_MIN_WIDTH,
            },
        )
        return page


class ScriptPage:
    """Renders Mermaid diagrams and rasterizes images (PNG/JPEG) inside one page."""

    def __init__(
        self, page: Page, browser: BrowserManager, *, theme: str, mermaid_ready: bool
    ) -> None:
        self._page = page
        self._browser = browser
        self._background = THEME_BACKGROUNDS.get(theme)
        self._mermaid_ready = mermaid_ready
        self._broken = False
        self._counter = 0

    async def render(self, code: str) -> MermaidDiagram | MermaidFailure:
        if not self._mermaid_ready:
            return MermaidFailure("renderizador Mermaid indisponível no servidor")
        if self._broken:
            return MermaidFailure("renderização interrompida por um diagrama anterior")
        self._counter += 1
        payload = {"id": f"mmd{self._counter}", "code": code, "background": self._background}
        try:
            async with asyncio.timeout(DIAGRAM_TIMEOUT):
                result = await self._page.evaluate(_RENDER_JS, payload)
        except TimeoutError:
            self._broken = True
            return MermaidFailure("tempo limite excedido ao renderizar o diagrama")
        except PlaywrightError as exc:
            await self._browser.raise_if_crashed(exc)
            self._broken = True
            logger.warning("Mermaid page failed: %s", exc)
            return MermaidFailure("falha inesperada ao renderizar o diagrama")
        if not result.get("ok"):
            return MermaidFailure(summarize_error(str(result.get("error", ""))))
        return MermaidDiagram(
            svg=result["svg"], width=float(result["width"]), height=float(result["height"])
        )

    async def rasterize(
        self,
        src: str,
        *,
        scale: float = 1.0,
        width: float | None = None,
        height: float | None = None,
        fit: tuple[float, float] | None = None,
        image_type: Literal["png", "jpeg"] = "png",
    ) -> RasterImage | None:
        """Draw an image (any format Chromium decodes, e.g. a data URI) into a bitmap.

        ``width``/``height`` set the displayed CSS size (default: the natural size,
        shrunk to fit within ``fit`` when given); the bitmap has ``scale`` times as
        many pixels, limited to ``RASTER_MAX_SIDE``. PNG keeps transparency; JPEG
        (for photos) is opaque. Returns ``None`` when the image cannot be decoded.
        """
        if self._broken:
            return None
        payload = {
            "src": src,
            "scale": scale,
            "maxSide": RASTER_MAX_SIDE,
            "maxPixels": MAX_IMAGE_PIXELS,
            "width": width,
            "height": height,
            "fit": list(fit) if fit else None,
        }
        try:
            async with asyncio.timeout(RASTER_TIMEOUT):
                info = await self._page.evaluate(_RASTER_JS, payload)
                viewport = self._page.viewport_size or {"width": 0, "height": 0}
                needed_width = max(viewport["width"], int(info["pixelWidth"]) + 16)
                needed_height = max(viewport["height"], int(info["pixelHeight"]) + 16)
                if (needed_width, needed_height) != (viewport["width"], viewport["height"]):
                    await self._page.set_viewport_size(
                        {"width": needed_width, "height": needed_height}
                    )
                options: dict[str, Any] = (
                    {"type": "png", "omit_background": True}
                    if image_type == "png"
                    else {"type": "jpeg", "quality": JPEG_QUALITY}
                )
                bitmap = await self._page.locator("#raster img").screenshot(
                    animations="disabled", **options
                )
        except TimeoutError:
            self._broken = True
            return None
        except PlaywrightError as exc:
            await self._browser.raise_if_crashed(exc)
            logger.info("Image rasterization failed: %s", str(exc).splitlines()[0][:200])
            return None
        return RasterImage(
            data=bitmap,
            media_type=f"image/{image_type}",
            width=float(info["width"]),
            height=float(info["height"]),
        )


def summarize_error(message: str) -> str:
    """Short, single-line (and, for common parser errors, pt-BR) Mermaid error message."""
    lines = [line.strip() for line in message.strip().splitlines() if line.strip()]
    if not lines:
        return "erro desconhecido"
    first = lines[0]
    if _UNKNOWN_DIAGRAM.search(first):
        return "tipo de diagrama não reconhecido"
    summary = first
    parse_error = _PARSE_ERROR.match(first)
    if parse_error:
        summary = f"erro de sintaxe na linha {parse_error.group(1)}"
        expecting = _EXPECTING.match(lines[-1]) if len(lines) > 1 else None
        if expecting:
            expected = [token.strip() for token in expecting.group(1).split(",")]
            if len(expected) > _MAX_EXPECTED_TOKENS:
                expected = [*expected[:_MAX_EXPECTED_TOKENS], "…"]
            summary += f" (esperado {', '.join(expected)}; encontrado {expecting.group(2)})"
    elif first.endswith(":") and len(lines) > 1:
        summary = f"{first} {lines[-1]}"
    return summary if len(summary) <= _ERROR_CHARS else summary[:_ERROR_CHARS] + "…"
