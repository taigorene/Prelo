"""Conversion orchestrator: Markdown (or a ``.zip`` with Markdown + images) → PDF / DOCX.

Pipeline per job (isolated temp directory, bounded by a semaphore and a timeout):

1. validate/decode the upload (:mod:`inputs`);
2. parse GitHub-flavored Markdown into pandoc's JSON AST (:mod:`pandoc`), converting
   raw HTML to native elements for DOCX (``assets/raw-html.lua``);
3. transform the AST (:mod:`ast`): display math, GitHub alert titles, embedded
   images (:mod:`resources`), Mermaid diagrams (:mod:`mermaid`), print tweaks (PDF),
   alerts as quotes, table widths and block spacing (DOCX);
4. write the output: HTML + headless Chromium for PDF (:mod:`html`, :mod:`pdf`),
   pandoc with a per-job reference document for DOCX (:mod:`docx`).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError

from app.config import Settings
from app.converter import ast
from app.converter.ast import Node
from app.converter.browser import BrowserCrashedError, BrowserManager, RendererCrashedError
from app.converter.docx import MEDIA_META_KEY, MEDIA_SCHEME, build_job_reference, render_docx
from app.converter.errors import (
    ConversionError,
    ConversionTimeoutError,
    InputTooLargeError,
    InvalidInputError,
    ServiceUnavailableError,
)
from app.converter.html import ASSETS_DIR, render_html
from app.converter.inputs import PreparedInput, format_mb, prepare_input
from app.converter.mermaid import MermaidDiagram, MermaidFailure, MermaidRenderer, ScriptPage
from app.converter.options import (
    CSS_PX_PER_MM,
    ConversionOptions,
    OutputFormat,
    PageGeometry,
    page_geometry,
)
from app.converter.pandoc import MSG_TOO_COMPLEX, encode_ast, markdown_to_ast, pandoc_version
from app.converter.pdf import FOOTER_EXTRA_MM, print_pdf, resource_url
from app.converter.resources import (
    DOCX_NATIVE_TYPES,
    ImageResolver,
    RemoteFetcher,
    ResolvedImage,
    ResourceBudget,
    display_url,
    is_remote,
    jpeg_orientation,
    to_data_uri,
)

logger = logging.getLogger("prelo.converter")

RAW_HTML_FILTER = ASSETS_DIR / "raw-html.lua"
MAX_WARNINGS = 50
TOC_TITLE = "Sumário"
MERMAID_ALT = "Mermaid diagram"
MERMAID_DOCX_ALT = "Diagrama Mermaid"
MERMAID_ERROR_PREFIX = "⚠️ Erro no diagrama Mermaid: "
# Pixel density of rasterized vector graphics (Mermaid, SVG) in DOCX.
VECTOR_RASTER_SCALE = 3.0
# Share of the printable height a diagram/image may take: a tall one still leaves room on
# its page for the heading(s) that introduce it (matches ``max-height: 80vh`` in the CSS).
MAX_HEIGHT_RATIO = 0.8
# Thin (6 pt), borderless paragraph that keeps adjacent code blocks, quotes and tables apart.
DOCX_SPACER = ast.raw_block(
    "openxml",
    '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="120" w:lineRule="exact"/>'
    "</w:pPr></w:p>",
)
# Word styles (defined in the reference document) used through ``custom-style``.
DOCX_SMALL_TABLE_STYLE = "Compact Small"
DOCX_HEADING_CODE_STYLE = "Verbatim Heading Char"
# Requests waiting for a conversion slot (beyond the running ones), per running slot, and
# the longest a request waits before getting a "busy" answer.
QUEUE_PER_SLOT = 2
MAX_QUEUE_WAIT_SECONDS = 60.0

MSG_BUSY = "O servidor está ocupado com outras conversões. Tente novamente em instantes."
MSG_TOO_DEEP = "O documento tem estruturas aninhadas demais para ser convertido."
MSG_TOO_LARGE = "O arquivo excede o limite de {limit} MB."


@dataclass
class ConversionResult:
    content: bytes
    media_type: str
    filename: str
    warnings: list[str]  # at most MAX_WARNINGS (see ``warnings_total``)
    duration_ms: int
    warnings_total: int = 0  # every distinct warning, including those left out of the list

    def __post_init__(self) -> None:
        self.warnings_total = max(self.warnings_total, len(self.warnings))


class WarningCollector:
    """Deduplicated, capped list of user-facing warnings."""

    def __init__(self, limit: int = MAX_WARNINGS) -> None:
        self._limit = limit
        self._items: dict[str, None] = {}
        self._dropped: set[str] = set()

    def add(self, message: str) -> None:
        if message in self._items:
            return
        if len(self._items) >= self._limit:
            self._dropped.add(message)
            return
        self._items[message] = None

    def as_list(self) -> list[str]:
        """The first ``limit`` distinct warnings."""
        return list(self._items)

    @property
    def total(self) -> int:
        """Number of distinct warnings, including those beyond the limit."""
        return len(self._items) + len(self._dropped)


def fit_within(width: float, height: float, max_width: float, max_height: float) -> tuple[int, int]:
    """Scale (width, height) down — never up — to fit the box, keeping the aspect ratio."""
    if width <= 0 or height <= 0:
        return max(1, round(width)), max(1, round(height))
    factor = min(1.0, max_width / width, max_height / height)
    return max(1, round(width * factor)), max(1, round(height * factor))


@dataclass(frozen=True)
class _Picture:
    """An image or diagram registered once for the output, with its natural size."""

    digest: str  # key in the job's resources
    width: float | None  # CSS px, when known
    height: float | None


@dataclass
class _Job:
    fmt: OutputFormat
    options: ConversionOptions
    geometry: PageGeometry
    workdir: Path
    source: PreparedInput
    fetcher: RemoteFetcher
    budget: ResourceBudget
    warnings: WarningCollector
    open_page: Callable[[], Awaitable[ScriptPage]]
    lang: str | None = None  # document language (``lang`` metadata)
    # Images and diagrams of the output, each stored once: digest -> (media type, bytes).
    resources: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    aligned_headers: dict[str, str] = field(default_factory=dict)  # DOCX: id -> alignment
    _page: ScriptPage | None = field(default=None, init=False)

    @property
    def max_width_px(self) -> float:
        return float(self.geometry.content_width_px)

    @property
    def max_height_px(self) -> float:
        height_mm = self.geometry.content_height_mm
        if self.fmt is OutputFormat.PDF and self.options.page_numbers:
            height_mm -= FOOTER_EXTRA_MM  # room taken by the page-number footer
        return height_mm * CSS_PX_PER_MM * MAX_HEIGHT_RATIO

    def register(self, media_type: str, data: bytes, digest: str | None = None) -> str:
        """Store a resource of the output once; returns its key."""
        key = digest or hashlib.sha256(data).hexdigest()
        self.resources.setdefault(key, (media_type, data))
        return key

    async def script_page(self) -> ScriptPage:
        """The job's JavaScript page (Mermaid/rasterization), opened on first use."""
        if self._page is None:
            self._page = await self.open_page()
        return self._page


class ConversionService:
    """Converts Markdown documents to PDF or DOCX. See the module docstring."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser = BrowserManager(sandbox=settings.chromium_sandbox)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._waiting = 0
        self._mermaid = MermaidRenderer(self._browser, None)
        self._mermaid_version: str | None = None
        self._pandoc_version: str | None = None
        self._reference: Path | None = None
        self._cache_dir: Path | None = None
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Check pandoc, load Mermaid, prepare the Word reference and launch Chromium.

        Environment problems are logged (and reported by :meth:`health`) instead of
        raised, so the HTTP service can still start and explain what is missing.
        """
        settings = self._settings
        self._pandoc_version = await pandoc_version(settings.pandoc_path)
        if self._pandoc_version is None:
            logger.error("pandoc not found or not working: %s", settings.pandoc_path)
        else:
            logger.info("pandoc %s found", self._pandoc_version)
            self._reference = await asyncio.to_thread(self._prepare_reference)

        script = self._load_mermaid()
        self._mermaid = MermaidRenderer(self._browser, script)

        try:
            await self._browser.start()
        except ServiceUnavailableError:
            logger.error("Chromium is unavailable; conversions will fail until it starts")
        self._started = True

    async def stop(self) -> None:
        self._started = False
        await self._browser.stop()
        if self._cache_dir is not None:
            await asyncio.to_thread(shutil.rmtree, self._cache_dir, True)
            self._cache_dir = None

    def is_ready(self) -> bool:
        return self._started and self._pandoc_version is not None and self._browser.is_connected()

    async def health(self) -> dict[str, bool]:
        pandoc_ok = (
            self._pandoc_version is not None
            and shutil.which(self._settings.pandoc_path) is not None
        )
        if self._started and not self._browser.is_connected():
            with contextlib.suppress(ServiceUnavailableError):
                await self._browser.ensure()  # self-heal after a crash
        return {"pandoc": pandoc_ok, "browser": self._browser.is_connected()}

    async def versions(self) -> dict[str, str]:
        """Engine versions; an engine whose version is unknown (not found) is left out."""
        versions = {
            "pandoc": self._pandoc_version,
            "chromium": self._browser.version,
            "mermaid": self._mermaid_version,
        }
        return {name: version for name, version in versions.items() if version}

    def _load_mermaid(self) -> bytes | None:
        path = self._settings.mermaid_js_path
        try:
            script = path.read_bytes()
        except OSError:
            logger.error("Mermaid not found at %s; diagrams will be kept as code", path)
            return None
        version_file = path.parent / "VERSION"
        with contextlib.suppress(OSError):
            self._mermaid_version = version_file.read_text(encoding="utf-8").strip() or None
        return script

    def _prepare_reference(self) -> Path | None:
        """Locate or build the base Word reference document (blocking)."""
        configured = self._settings.reference_docx
        if configured is not None and _is_docx(configured):
            return configured
        if configured is not None and _is_writable_target(configured):
            target = configured
        else:
            self._cache_dir = Path(tempfile.mkdtemp(prefix="prelo-cache-"))
            target = self._cache_dir / "reference.docx"
        try:
            from app.converter.docx_reference import build_base_reference

            build_base_reference(self._settings.pandoc_path, target)
            if _is_docx(target):
                logger.info("Word reference document built at %s", target)
                return target
        except Exception:
            logger.exception("Could not build the styled Word reference; using pandoc's default")
        try:
            subprocess.run(
                [
                    self._settings.pandoc_path,
                    "-o",
                    str(target),
                    "--print-default-data-file",
                    "reference.docx",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not create a Word reference document")
            return None
        return target if _is_docx(target) else None

    # ----------------------------------------------------------------- conversion
    async def convert(
        self,
        *,
        data: bytes,
        filename: str,
        fmt: OutputFormat,
        options: ConversionOptions,
    ) -> ConversionResult:
        """Convert one upload. Raises :class:`ConversionError` subclasses on failure."""
        started = time.perf_counter()
        try:
            if not self._started or self._pandoc_version is None:
                raise ServiceUnavailableError()
            if len(data) > self._settings.max_upload_bytes:
                raise InputTooLargeError(
                    MSG_TOO_LARGE.format(limit=format_mb(self._settings.max_upload_mb))
                )
            async with self._slot():
                content, stem, warnings = await self._run_with_timeout(data, filename, fmt, options)
        except ConversionError as exc:
            logger.info(
                "Conversion failed: format=%s input=%d bytes code=%s duration=%dms",
                fmt.value,
                len(data),
                exc.code,
                _elapsed_ms(started),
            )
            raise
        duration_ms = _elapsed_ms(started)
        logger.info(
            "Converted: format=%s input=%d bytes output=%d bytes warnings=%d duration=%dms",
            fmt.value,
            len(data),
            len(content),
            warnings.total,
            duration_ms,
        )
        return ConversionResult(
            content=content,
            media_type=fmt.media_type,
            filename=f"{stem}{fmt.extension}",
            warnings=warnings.as_list(),
            duration_ms=duration_ms,
            warnings_total=warnings.total,
        )

    @contextlib.asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        """Concurrency slot. The queue is bounded: with every slot busy, at most
        ``QUEUE_PER_SLOT`` requests per slot wait, each for a bounded time; others get a
        "busy" answer at once instead of holding their upload in memory."""
        limit = QUEUE_PER_SLOT * self._settings.max_concurrent_jobs
        if self._semaphore.locked() and self._waiting >= limit:
            raise ServiceUnavailableError(MSG_BUSY)
        self._waiting += 1
        try:
            wait = min(self._settings.conversion_timeout, MAX_QUEUE_WAIT_SECONDS)
            async with asyncio.timeout(wait):
                await self._semaphore.acquire()
        except TimeoutError:
            raise ServiceUnavailableError(MSG_BUSY) from None
        finally:
            self._waiting -= 1
        try:
            yield
        finally:
            self._semaphore.release()

    async def _run_with_timeout(
        self, data: bytes, filename: str, fmt: OutputFormat, options: ConversionOptions
    ) -> tuple[bytes, str, WarningCollector]:
        deadline = asyncio.timeout(self._settings.conversion_timeout)
        try:
            async with deadline:
                for attempt in (1, 2):
                    try:
                        return await self._run_job(data, filename, fmt, options)
                    except BrowserCrashedError:
                        if attempt == 2:
                            raise ServiceUnavailableError() from None
                        logger.warning("Chromium crashed during a conversion; retrying once")
                    except RendererCrashedError as exc:
                        # Usually memory: a document too large or complex for the renderer.
                        if attempt == 2:
                            raise InputTooLargeError(MSG_TOO_COMPLEX) from exc
                        logger.warning("Renderer crashed during a conversion (%s); retrying", exc)
        except TimeoutError:
            if deadline.expired():
                raise ConversionTimeoutError() from None
            raise
        except RecursionError:
            raise InvalidInputError(MSG_TOO_DEEP) from None
        raise AssertionError("unreachable")  # pragma: no cover

    async def _run_job(
        self, data: bytes, filename: str, fmt: OutputFormat, options: ConversionOptions
    ) -> tuple[bytes, str, WarningCollector]:
        settings = self._settings
        warnings = WarningCollector()
        workdir = Path(tempfile.mkdtemp(prefix="prelo-"))
        try:
            source = await _in_thread(
                prepare_input, filename, data, workdir, settings, cancellable=True
            )
            for note in source.notes:
                warnings.add(note)
            filters = [RAW_HTML_FILTER] if fmt is OutputFormat.DOCX else []
            doc = await markdown_to_ast(
                settings.pandoc_path,
                source.markdown,
                lua_filters=filters,
                cwd=workdir,
                warn=warnings.add,
            )
            geometry = page_geometry(options)
            budget = ResourceBudget(
                max_bytes=settings.max_images_total_bytes, max_mb=settings.max_images_total_mb
            )
            fetcher = RemoteFetcher(settings, budget)
            page_stack = contextlib.AsyncExitStack()

            async def open_page() -> ScriptPage:
                return await page_stack.enter_async_context(
                    self._mermaid.page(
                        theme=options.mermaid_theme.value, width_px=geometry.content_width_px
                    )
                )

            job = _Job(
                fmt=fmt,
                options=options,
                geometry=geometry,
                workdir=workdir,
                source=source,
                fetcher=fetcher,
                budget=budget,
                warnings=warnings,
                open_page=open_page,
                lang=ast.meta_text(doc, "lang"),
            )
            try:
                async with page_stack:  # the script page is closed before printing
                    doc["blocks"] = await self._transform(job, doc["blocks"])
                _set_metadata(doc, source.stem)
                if fmt is OutputFormat.DOCX:
                    _set_docx_media(doc, job.resources)
                payload = encode_ast(doc)
                del doc
                if fmt is OutputFormat.PDF:
                    content = await self._write_pdf(job, payload)
                else:
                    content = await self._write_docx(job, payload)
            finally:
                await fetcher.aclose()
        except PlaywrightError as exc:
            logger.exception("Browser error during conversion")
            raise ConversionError() from exc
        finally:
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, workdir, True))
        return content, source.stem, warnings

    # ------------------------------------------------------------- AST transforms
    async def _transform(self, job: _Job, blocks: list[Node]) -> list[Node]:
        pdf = job.fmt is OutputFormat.PDF
        blocks = ast.convert_math_blocks(blocks)
        blocks = ast.normalize_table_widths(blocks)
        if _is_portuguese(job.lang):
            blocks = ast.localize_alert_titles(blocks, ast.ALERT_TITLES_PT)
        blocks = ast.ordered_task_lists(blocks, html=pdf)
        if not pdf:
            blocks = ast.alerts_to_blockquotes(blocks)
            blocks = ast.fit_tables(
                blocks, line_px=job.max_width_px, small_style=DOCX_SMALL_TABLE_STYLE
            )
        pictures = await self._resolve_images(job, blocks)
        diagrams = await self._render_mermaid(job, blocks)
        if pdf:
            blocks = self._place_pdf(job, blocks, pictures, diagrams)
            blocks = ast.prepare_raw_html_for_print(blocks)
        else:
            blocks = self._place_docx(job, blocks, pictures, diagrams)
            blocks = ast.heading_code_spans(blocks, DOCX_HEADING_CODE_STYLE)
            job.aligned_headers = ast.aligned_headers(blocks)
            blocks = ast.separate_adjacent_blocks(blocks, DOCX_SPACER)
            blocks = ast.box_quotes(blocks, self._layout(job), DOCX_SPACER)
        return ast.unwrap_nested_links(blocks)

    def _layout(self, job: _Job) -> ast.DocxLayout:
        return ast.DocxLayout(root_px=job.max_width_px)

    async def _resolve_images(self, job: _Job, blocks: list[Node]) -> dict[str, _Picture | None]:
        """Resolve every distinct image once and register it for the output."""
        sources = ast.image_sources(blocks)
        if not sources:
            return {}
        resolver = ImageResolver(
            base_dir=job.source.base_dir,
            root_dir=job.source.root_dir,
            fetcher=job.fetcher,
            budget=job.budget,
            max_bytes=self._settings.remote_image_max_bytes,
            max_mb=self._settings.remote_image_max_mb,
            warn=job.warnings.add,
        )
        resolved = await resolver.resolve_all(sources)
        prepared: dict[str, _Picture | None] = {}  # by content digest
        pictures: dict[str, _Picture | None] = {}
        for src, image in resolved.items():
            if image is None:
                pictures[src] = None
                continue
            if image.digest not in prepared:
                if job.fmt is OutputFormat.PDF:
                    job.register(image.media_type, image.data, image.digest)
                    size = image.size or (None, None)
                    prepared[image.digest] = _Picture(image.digest, *size)
                else:
                    prepared[image.digest] = await self._docx_image(job, src, image)
            pictures[src] = prepared[image.digest]
        return pictures

    async def _docx_image(self, job: _Job, src: str, image: ResolvedImage) -> _Picture | None:
        """Register a Word-compatible version of an image (PNG/JPEG/GIF)."""
        orientation = jpeg_orientation(image.data) if image.media_type == "image/jpeg" else None
        rotated_photo = orientation is not None and orientation != 1
        if image.media_type in DOCX_NATIVE_TYPES and not rotated_photo:
            digest = job.register(image.media_type, image.data, image.digest)
            return _Picture(digest, *(image.size or (None, None)))
        # Word cannot show this format (or ignores the EXIF rotation): let Chromium draw it.
        page = await job.script_page()
        vector = image.media_type == "image/svg+xml"
        raster = await page.rasterize(
            image.data_uri,
            scale=VECTOR_RASTER_SCALE if vector else 1.0,
            fit=(job.max_width_px, job.max_height_px) if vector else None,
            image_type="jpeg" if rotated_photo else "png",
        )
        if raster is None:
            job.warnings.add(f"Não foi possível converter a imagem para o Word: {display_url(src)}")
            return None
        digest = job.register(raster.media_type, raster.data)
        return _Picture(digest, raster.width, raster.height)

    async def _render_mermaid(self, job: _Job, blocks: list[Node]) -> dict[str, _Picture | str]:
        """Render every distinct diagram once: its picture, or the error message."""
        positions = ast.mermaid_positions(blocks)
        if not positions:
            return {}
        page = await job.script_page()
        results: dict[str, _Picture | str] = {}
        for code, numbers in positions.items():
            result = await page.render(code)
            picture = None
            if isinstance(result, MermaidDiagram):
                picture = await self._diagram_picture(job, page, result)
            if picture is not None:
                results[code] = picture
                continue
            message = (
                result.message
                if isinstance(result, MermaidFailure)
                else "não foi possível gerar a imagem do diagrama"
            )
            where = ", ".join(str(number) for number in numbers)  # position in the document
            job.warnings.add(f"Erro no diagrama Mermaid nº {where}: {message}")
            results[code] = message
        return results

    async def _diagram_picture(
        self, job: _Job, page: ScriptPage, diagram: MermaidDiagram
    ) -> _Picture | None:
        width, height = fit_within(
            diagram.width, diagram.height, job.max_width_px, job.max_height_px
        )
        svg = diagram.svg.encode("utf-8")
        if job.fmt is OutputFormat.PDF:
            return _Picture(job.register("image/svg+xml", svg), width, height)
        raster = await page.rasterize(
            to_data_uri("image/svg+xml", svg), scale=VECTOR_RASTER_SCALE, width=width, height=height
        )
        if raster is None:
            return None
        return _Picture(job.register(raster.media_type, raster.data), width, height)

    def _place_pdf(
        self,
        job: _Job,
        blocks: list[Node],
        pictures: dict[str, _Picture | None],
        diagrams: dict[str, _Picture | str],
    ) -> list[Node]:
        """Point images and diagrams at the resources served to the browser (once each)."""

        def fn(node: Node) -> Node | list[Node] | None:
            if node["t"] == "Image":
                picture = self._picture_for(job, node, pictures)
                if picture is None:
                    return _image_fallback(node)
                node["c"][2][0] = resource_url(picture.digest)
                return node
            if ast.is_code_block_with_class(node, "mermaid"):
                result = diagrams.get(node["c"][1])
                if isinstance(result, _Picture):
                    html = (
                        f'<figure class="mermaid-diagram"><img alt="{MERMAID_ALT}" '
                        f'width="{result.width}" height="{result.height}" '
                        f'src="{resource_url(result.digest)}"></figure>'
                    )
                    return [ast.raw_block("html", html)]
                return _mermaid_error(node, result)
            return None

        return ast.walk(blocks, fn)

    def _place_docx(
        self,
        job: _Job,
        blocks: list[Node],
        pictures: dict[str, _Picture | None],
        diagrams: dict[str, _Picture | str],
    ) -> list[Node]:
        """Point images and diagrams at the media of the document, each sized to the room
        it has where it appears (list levels, quotes and table cells are narrower)."""

        def fn(node: Node, room: ast.Room) -> Node | list[Node] | None:
            if node["t"] == "Image":
                picture = self._picture_for(job, node, pictures)
                if picture is None:
                    return _image_fallback(node)
                attr = node["c"][0]
                node["c"][2][0] = f"{MEDIA_SCHEME}{picture.digest}"
                has_size = ast.attr_value(attr, "width") or ast.attr_value(attr, "height")
                if not has_size and picture.width and picture.height:
                    width, _ = fit_within(
                        picture.width, picture.height, room.width, job.max_height_px
                    )
                    attr[2].append(["width", f"{width}px"])
                return node
            if ast.is_code_block_with_class(node, "mermaid"):
                result = diagrams.get(node["c"][1])
                if isinstance(result, _Picture):
                    assert result.width is not None and result.height is not None
                    width, _ = fit_within(
                        result.width, result.height, room.width, job.max_height_px
                    )
                    attr = ast.make_attr(width=f"{width}px")
                    alt = ast.text_inlines(MERMAID_DOCX_ALT)
                    image = ast.image(f"{MEDIA_SCHEME}{result.digest}", alt, attr=attr)
                    return [ast.para([image])]
                return _mermaid_error(node, result)
            return None

        return ast.walk_with_room(blocks, self._layout(job), fn)

    @staticmethod
    def _picture_for(
        job: _Job, node: Node, pictures: dict[str, _Picture | None]
    ) -> _Picture | None:
        if not job.budget.take_reference():
            job.warnings.add(job.budget.references_limit_message)
            return None
        return pictures.get(node["c"][2][0])

    # -------------------------------------------------------------------- writers
    async def _write_pdf(self, job: _Job, payload: bytes) -> bytes:
        html = await render_html(
            payload, pandoc_path=self._settings.pandoc_path, toc=job.options.toc, cwd=job.workdir
        )
        return await print_pdf(
            html,
            browser=self._browser,
            geometry=job.geometry,
            page_numbers=job.options.page_numbers,
            base_dir=job.source.base_dir,
            root_dir=job.source.root_dir,
            fetcher=job.fetcher,
            budget=job.budget,
            resources=job.resources,
            max_file_bytes=self._settings.remote_image_max_bytes,
            max_file_mb=self._settings.remote_image_max_mb,
            warn=job.warnings.add,
        )

    async def _write_docx(self, job: _Job, payload: bytes) -> bytes:
        if self._reference is None:
            raise ServiceUnavailableError()
        reference = job.workdir / "reference.docx"
        await _in_thread(
            build_job_reference,
            self._reference,
            reference,
            job.geometry,
            page_numbers=job.options.page_numbers,
        )
        return await render_docx(
            payload,
            pandoc_path=self._settings.pandoc_path,
            reference=reference,
            workdir=job.workdir,
            toc=job.options.toc,
            aligned_headers=job.aligned_headers,
        )


# ---------------------------------------------------------------------- helpers
def _set_metadata(doc: dict[str, Any], stem: str) -> None:
    ast.ensure_meta_string(doc, "toc-title", TOC_TITLE)
    if not ast.has_meta(doc, "title"):
        ast.ensure_meta_string(doc, "pagetitle", stem)


def _set_docx_media(doc: dict[str, Any], resources: dict[str, tuple[str, bytes]]) -> None:
    """Pass each image of the DOCX once, as metadata read by ``docx-media.lua`` (the
    images themselves only carry a short ``prelo-media:<key>`` reference)."""
    doc.setdefault("meta", {})[MEDIA_META_KEY] = {
        "t": "MetaMap",
        "c": {
            key: {"t": "MetaString", "c": to_data_uri(media_type, data)}
            for key, (media_type, data) in resources.items()
        },
    }


def _mermaid_error(node: Node, message: _Picture | str | None) -> list[Node]:
    """The diagram's code followed by its error message."""
    text = message if isinstance(message, str) else "erro desconhecido"
    error = ast.para(ast.text_inlines(MERMAID_ERROR_PREFIX + text))
    return [node, ast.div(["mermaid-error"], [error])]


def _is_portuguese(lang: str | None) -> bool:
    """True for BCP 47 tags of Portuguese (``pt``, ``pt-BR``, ``pt_PT``...)."""
    return bool(lang) and re.split(r"[-_]", lang.strip().lower(), maxsplit=1)[0] == "pt"


def _image_fallback(node: Node) -> list[Node]:
    """Inline replacement for an image that cannot be embedded."""
    alt: list[Node] = node["c"][1]
    src: str = node["c"][2][0]
    if is_remote(src):
        return [ast.link(alt or ast.text_inlines(display_url(src)), src)]
    if not alt:
        name = src.rsplit("/", 1)[-1] if not src.startswith("data:") else ""
        alt = ast.text_inlines(f"[imagem: {name}]" if name else "[imagem]")
    return [ast.emph(alt)]


def _is_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "word/document.xml" in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _is_writable_target(path: Path) -> bool:
    parent = path.parent
    return parent.is_dir() and not path.exists() and _can_write(parent)


def _can_write(directory: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=directory):
            return True
    except OSError:
        return False


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def _in_thread[T](
    fn: Callable[..., T], *args: Any, cancellable: bool = False, **kwargs: Any
) -> T:
    """Run blocking work in a thread. On cancellation, signal it (``cancel=`` event for
    ``cancellable`` functions) and wait for it to stop so the job's temp directory is
    never removed while the thread still writes into it."""
    cancel = threading.Event()
    if cancellable:
        kwargs["cancel"] = cancel
    future = asyncio.get_running_loop().run_in_executor(
        None, functools.partial(fn, *args, **kwargs)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancel.set()
        await asyncio.wait({future})
        raise
