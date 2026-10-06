"""PDF output: print the HTML document with headless Chromium (JavaScript disabled).

The document is served from a fake origin through request interception:

* ``http://document.local/<base dir>/__document__.html`` is the HTML itself, so
  relative URLs in raw HTML resolve against the main document's directory and
  ``/``-prefixed ones against the upload root (like on GitHub);
* ``/__prelo__/<sha256>`` serves the images (and Mermaid diagrams) the conversion
  already resolved, so each is sent to the browser once however often it is used;
* other ``document.local`` paths map to regular files under ``root_dir`` (images,
  fonts, stylesheets, media bundled in a ``.zip``), never outside it; images are
  registered like the others and redirected to their ``/__prelo__/`` URL;
* remote ``http(s)`` images/fonts/media go through the SSRF-guarded fetcher;
* everything else (frames, navigations, scripts, XHR, ``file:``...) is aborted.

Files and downloads are charged to the job's :class:`ResourceBudget`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import os
import posixpath
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Route

from app.converter.browser import BrowserManager, RendererCrashedError
from app.converter.inputs import format_mb
from app.converter.options import PageGeometry
from app.converter.resources import (
    FetchError,
    RemoteFetcher,
    ResourceBudget,
    Warn,
    display_url,
    sniff_image_type,
)

logger = logging.getLogger("prelo.converter")

MM_PER_INCH = 25.4
DOCUMENT_HOST = "document.local"
DOCUMENT_ORIGIN = f"http://{DOCUMENT_HOST}"
DOCUMENT_NAME = "__document__.html"
RESOURCE_PREFIX = "/__prelo__/"
LOCAL_RESOURCE_TYPES = frozenset({"image", "stylesheet", "font", "media"})
REMOTE_RESOURCE_TYPES = frozenset({"image", "font", "media"})
# Extra bottom margin (mm) reserving room for the page-number footer.
FOOTER_EXTRA_MM = 3.0
# Defense in depth on top of the disabled JavaScript and the request router.
DOCUMENT_CSP = (
    "script-src 'none'; object-src 'none'; frame-src 'none'; child-src 'none'; "
    "worker-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'"
)
EMPTY_TEMPLATE = "<span></span>"
# Chromium shrinks the whole document when one element is wider than the page; zoom out
# only such elements instead (tables with long words, wide equations, fixed-width HTML).
# Runs through the DevTools protocol, so it works with JavaScript and scripts disabled.
FIT_WIDE_BLOCKS_JS = """
() => {
  const root = document.querySelector('main') || document.body;
  const selector = 'table, math[display="block"], pre, div, figure, details, dl, blockquote, ' +
    'img, svg, video';
  let fitted = 0;
  for (const el of root.querySelectorAll(selector)) {
    const parent = el.parentElement;
    if (!parent || el.closest('[data-fitted]')) continue;
    const style = getComputedStyle(parent);
    const available = parent.clientWidth - parseFloat(style.paddingLeft || '0') -
      parseFloat(style.paddingRight || '0');
    const width = Math.max(el.getBoundingClientRect().width, el.scrollWidth);
    if (available > 0 && width > available + 0.5) {
      el.style.zoom = String(Math.max(0.05, (available - 1) / width));
      el.setAttribute('data-fitted', '');
      fitted += 1;
    }
  }
  return fitted;
}
"""
_DOCUMENT_URI = re.compile(rb"/URI\s*\((http://document\.local/(?:[^()\\]|\\.)*)\)")
_RENDERER_CRASH = re.compile(
    r"Target crashed|Page crashed|Printing failed|"
    r"Target page, context or browser has been closed",
    re.IGNORECASE,
)
FOOTER_TEMPLATE = (
    '<div style="width:100%;text-align:center;font-size:9px;color:#6e7781;'
    "font-family:'Inter Variable','Noto Sans','DejaVu Sans',sans-serif;\">"
    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
)


def document_path(base_dir: Path, root_dir: Path) -> str:
    """URL path of the HTML document (inside the main file's directory)."""
    relative = base_dir.resolve().relative_to(root_dir.resolve())
    segments = [quote(part) for part in relative.parts]
    return "/" + "/".join([*segments, DOCUMENT_NAME])


def local_resource_path(url_path: str, root_dir: Path) -> Path | None:
    """File under ``root_dir`` for a ``document.local`` URL path (``None`` if outside)."""
    relative = unquote(url_path).lstrip("/")
    if not relative or "\x00" in relative:
        return None
    try:
        root = root_dir.resolve()
        candidate = (root / relative).resolve()
    except (OSError, RuntimeError):
        return None
    return candidate if candidate.is_relative_to(root) else None


def inches(value_mm: float) -> str:
    """Length for ``page.pdf`` in inches: Playwright's own ``mm`` conversion is coarse
    (3.78 px/mm) and would make e.g. Letter pages 612.96 pt wide instead of 612 pt."""
    return f"{value_mm / MM_PER_INCH:.5f}in"


def pdf_margins(geometry: PageGeometry, *, page_numbers: bool) -> dict[str, str]:
    margin = geometry.margin_mm
    bottom = margin + FOOTER_EXTRA_MM if page_numbers else margin
    return {
        "top": inches(margin),
        "right": inches(margin),
        "bottom": inches(bottom),
        "left": inches(margin),
    }


def resource_url(digest: str) -> str:
    """URL (absolute path on the document origin) of a registered resource."""
    return f"{RESOURCE_PREFIX}{digest}"


def relativize_document_links(pdf: bytes, page_path: str) -> bytes:
    """Turn link annotations pointing at the internal origin back into relative links.

    Chromium resolves relative ``href``s (``LICENSE``, ``docs/guide.md``) against the
    page URL, which would leave dead ``http://document.local/...`` links in the PDF.
    Each URI string is replaced by the path relative to the main document (as in the
    DOCX output) and padded with spaces to its original length, so no object offset
    of the file changes.
    """
    base = posixpath.dirname(page_path) or "/"

    def replace(match: re.Match[bytes]) -> bytes:
        original = match.group(0)
        url = _pdf_unescape(match.group(1)).decode("latin-1")
        parts = urlsplit(url)
        relative = posixpath.relpath(parts.path or "/", base)
        if parts.path.endswith("/") and not relative.endswith("/"):
            relative += "/"
        if parts.query:
            relative += f"?{parts.query}"
        if parts.fragment:
            relative += f"#{parts.fragment}"
        replacement = b"/URI (" + _pdf_escape(relative.encode("latin-1", "replace")) + b")"
        if len(replacement) > len(original):
            return original
        return replacement + b" " * (len(original) - len(replacement))

    return _DOCUMENT_URI.sub(replace, pdf)


def _pdf_unescape(value: bytes) -> bytes:
    return re.sub(rb"\\(.)", rb"\1", value)


def _pdf_escape(value: bytes) -> bytes:
    return re.sub(rb"([()\\])", rb"\\\1", value)


class DocumentRouter:
    """Request interception for the document page (see module docstring)."""

    def __init__(
        self,
        *,
        html: str,
        page_path: str,
        root_dir: Path,
        fetcher: RemoteFetcher,
        budget: ResourceBudget,
        resources: dict[str, tuple[str, bytes]],
        max_file_bytes: int,
        max_file_mb: float,
        warn: Warn,
    ) -> None:
        self.url = f"{DOCUMENT_ORIGIN}{page_path}"
        self._html = html.encode("utf-8")
        self._page_path = page_path
        self._root_dir = root_dir
        self._fetcher = fetcher
        self._budget = budget
        self._resources = resources
        self._max_file_bytes = max_file_bytes
        self._max_file_mb = max_file_mb
        self._warn = warn
        self._served = False
        # Local images by URL path (any query string): read and registered once each.
        self._local_images: dict[str, asyncio.Future[str]] = {}

    async def handle(self, route: Route) -> None:
        try:
            await self._dispatch(route, route.request)
        except PlaywrightError:
            pass  # the page/context is gone
        except Exception:
            logger.exception("Request routing failed")
            with contextlib.suppress(PlaywrightError):
                await route.abort("failed")

    async def _dispatch(self, route: Route, request: Request) -> None:
        parts = urlsplit(request.url)
        kind = request.resource_type
        if parts.scheme == "http" and parts.netloc == DOCUMENT_HOST:
            if parts.path == self._page_path and kind == "document" and not self._served:
                self._served = True
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": DOCUMENT_CSP},
                    body=self._html,
                )
            elif parts.path.startswith(RESOURCE_PREFIX) and kind in LOCAL_RESOURCE_TYPES:
                await self._serve_resource(route, parts.path[len(RESOURCE_PREFIX) :])
            elif kind in LOCAL_RESOURCE_TYPES and self._take_request():
                await self._serve_local(route, parts.path, kind)
            else:
                await self._refuse(route, request)
        elif (
            parts.scheme in ("http", "https")
            and kind in REMOTE_RESOURCE_TYPES
            and self._take_request()
        ):
            await self._serve_remote(route, request.url, kind)
        else:
            await self._refuse(route, request)

    @staticmethod
    async def _refuse(route: Route, request: Request) -> None:
        if request.is_navigation_request() and request.frame.parent_frame is None:
            # A blocked navigation (e.g. <meta http-equiv="refresh">) would replace the
            # document with an error page; "204 No Content" keeps the current one.
            await route.fulfill(status=204, body=b"")
        else:
            await route.abort("blockedbyclient")

    def _take_request(self) -> bool:
        if self._budget.take_reference():
            return True
        self._warn(self._budget.references_limit_message)
        return False

    async def _serve_resource(self, route: Route, digest: str) -> None:
        resource = self._resources.get(digest)
        if resource is None:
            await route.fulfill(status=404, content_type="text/plain", body=b"")
            return
        media_type, body = resource
        await route.fulfill(status=200, content_type=media_type, body=body)

    async def _serve_local(self, route: Route, url_path: str, kind: str) -> None:
        shown = unquote(url_path).lstrip("/")
        try:
            if kind == "image":
                # Every spelling of the image (``a.png?1``, ``a.png?2``...) is redirected to
                # one URL, so the browser receives and decodes the file once.
                digest = await self._local_image(url_path, shown)
                await route.fulfill(status=302, headers={"Location": resource_url(digest)})
                return
            body = await asyncio.to_thread(self._read_local, url_path, shown)
        except FetchError as exc:
            if kind == "image" or not exc.message.startswith("Imagem não encontrada"):
                self._warn(exc.message)
            await route.fulfill(status=404, content_type="text/plain", body=b"")
            return
        content_type = sniff_image_type(body) or mimetypes.guess_type(shown)[0]
        await route.fulfill(
            status=200, content_type=content_type or "application/octet-stream", body=body
        )

    async def _local_image(self, url_path: str, shown: str) -> str:
        """Digest of a local image, registered (read, checked, charged) on first use."""
        task = self._local_images.get(url_path)
        if task is None:
            task = asyncio.ensure_future(self._register_local_image(url_path, shown))
            self._local_images[url_path] = task
        return await asyncio.shield(task)

    async def _register_local_image(self, url_path: str, shown: str) -> str:
        body = await asyncio.to_thread(self._read_local, url_path, shown)
        content_type = sniff_image_type(body)
        if content_type is None:
            raise FetchError(f"Formato de imagem não reconhecido: {shown}")
        self._budget.check_pixels(content_type, body, shown)
        digest = hashlib.sha256(body).hexdigest()
        self._resources.setdefault(digest, (content_type, body))
        return digest

    def _read_local(self, url_path: str, shown: str) -> bytes:
        """Bytes of a file under the upload root, charged to the budget (blocking)."""
        path = local_resource_path(url_path, self._root_dir)
        # os.path.isfile is False on any OSError (e.g. a name longer than the OS allows).
        if path is None or not os.path.isfile(path):
            raise FetchError(f"Imagem não encontrada: {shown}")
        try:
            size = path.stat().st_size
            if size > self._max_file_bytes:
                raise FetchError(
                    f"Arquivo maior que o limite de {format_mb(self._max_file_mb)} MB: {shown}"
                )
            self._budget.reserve(size, shown)
            return path.read_bytes()
        except OSError:
            raise FetchError(f"Imagem não encontrada: {shown}") from None

    async def _serve_remote(self, route: Route, url: str, kind: str) -> None:
        try:
            resource = await self._fetcher.fetch(url)
            content_type = sniff_image_type(resource.data)
            if kind == "image" and content_type is not None:
                self._budget.check_pixels(content_type, resource.data, display_url(url))
        except FetchError as exc:
            self._warn(exc.message)
            await route.abort("blockedbyclient")
            return
        await route.fulfill(
            status=200,
            content_type=content_type or resource.content_type or "application/octet-stream",
            body=resource.data,
        )


async def print_pdf(
    html: str,
    *,
    browser: BrowserManager,
    geometry: PageGeometry,
    page_numbers: bool,
    base_dir: Path,
    root_dir: Path,
    fetcher: RemoteFetcher,
    budget: ResourceBudget,
    resources: dict[str, tuple[str, bytes]],
    max_file_bytes: int,
    max_file_mb: float,
    warn: Warn,
) -> bytes:
    """Load ``html`` in an isolated, script-less browser context and print it to PDF."""
    page_path = document_path(base_dir, root_dir)
    router = DocumentRouter(
        html=html,
        page_path=page_path,
        root_dir=root_dir,
        fetcher=fetcher,
        budget=budget,
        resources=resources,
        max_file_bytes=max_file_bytes,
        max_file_mb=max_file_mb,
        warn=warn,
    )
    crashed = False

    def on_crash(_: Page) -> None:
        nonlocal crashed
        crashed = True

    async with browser.context(
        java_script_enabled=False,
        viewport={"width": geometry.content_width_px, "height": 1024},
        device_scale_factor=1,
        service_workers="block",
        accept_downloads=False,
    ) as context:
        page: Page | None = None
        try:
            await context.route("**/*", router.handle)
            page = await context.new_page()
            page.on("crash", on_crash)
            # No Playwright timeouts: the job-level timeout bounds the whole conversion.
            await page.goto(router.url, wait_until="load", timeout=0)
            await page.emulate_media(media="print")
            fitted = await page.evaluate(FIT_WIDE_BLOCKS_JS)
            if fitted:
                logger.debug("Scaled down %d element(s) wider than the page", fitted)
            pdf = await page.pdf(
                width=inches(geometry.width_mm),
                height=inches(geometry.height_mm),
                landscape=False,  # the geometry is already oriented
                margin=pdf_margins(geometry, page_numbers=page_numbers),
                print_background=True,
                display_header_footer=page_numbers,
                header_template=EMPTY_TEMPLATE,
                footer_template=FOOTER_TEMPLATE if page_numbers else EMPTY_TEMPLATE,
                prefer_css_page_size=False,
                outline=True,
                tagged=True,
            )
        except PlaywrightError as exc:
            await browser.raise_if_crashed(exc)
            if (
                crashed
                or (page is not None and page.is_closed())
                or _RENDERER_CRASH.search(str(exc))
            ):
                raise RendererCrashedError(str(exc).splitlines()[0]) from exc
            raise
    return await asyncio.to_thread(relativize_document_links, pdf, page_path)
