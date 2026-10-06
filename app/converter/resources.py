"""Image resources: local files, remote URLs (SSRF-guarded) and ``data:`` URIs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import logging
import os
import re
import socket
import struct
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, unquote_to_bytes

import httpx

from app import __version__
from app.converter.inputs import format_mb

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger("prelo.converter")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Warn = Callable[[str], None]

MAX_REDIRECTS = 5
REMOTE_CONCURRENCY = 4
# Decoded size limits: a small, highly compressed file can declare a huge bitmap.
MAX_IMAGE_PIXELS = 50_000_000  # one image (e.g. 8160 x 6120, a 50 MP photo)
MAX_DOCUMENT_PIXELS = 200_000_000  # all distinct images of one document
# Image elements (Markdown images, raw HTML resources) handled per document.
MAX_IMAGE_REFERENCES = 2000
USER_AGENT = f"prelo/{__version__} (image fetcher)"
DOCX_NATIVE_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/gif"})

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_SVG_HEAD = re.compile(
    rb"^\s*(?:<\?xml[^>]*>\s*)?(?:(?:<!--.*?-->|<!doctype[^>]*>)\s*)*<svg[\s>/]",
    re.IGNORECASE | re.DOTALL,
)
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_DISPLAY_URL_CHARS = 150


# ---------------------------------------------------------------------------
# Image type detection
# ---------------------------------------------------------------------------
def sniff_image_type(data: bytes) -> str | None:
    """Media type of an image detected from its leading bytes (``None`` if unknown)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and _is_avif(data):
        return "image/avif"
    if data[:2] == b"BM" and len(data) >= 26 and data[14] in (12, 40, 52, 56, 64, 108, 124):
        return "image/bmp"
    if data[:4] == b"\x00\x00\x01\x00" and len(data) >= 6 and data[4:6] != b"\x00\x00":
        return "image/x-icon"
    head = data[:4096].removeprefix(b"\xef\xbb\xbf")
    if _SVG_HEAD.match(head):
        return "image/svg+xml"
    return None


def _is_avif(data: bytes) -> bool:
    box_size = int.from_bytes(data[0:4], "big")
    brands = data[8 : min(max(box_size, 16), 64)]
    return any(brands[i : i + 4] in (b"avif", b"avis") for i in range(0, len(brands) - 3, 4))


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Pixel size of a raster image read from its header (``None`` for SVG/unknown/invalid).

    Covers every raster format :func:`sniff_image_type` detects: PNG, GIF, JPEG, WebP,
    AVIF, BMP and ICO (the largest entry).
    """
    parsers: dict[str | None, Callable[[bytes], tuple[int, int] | None]] = {
        "image/png": _png_dimensions,
        "image/gif": lambda d: struct.unpack("<HH", d[6:10]),
        "image/jpeg": _jpeg_dimensions,
        "image/webp": _webp_dimensions,
        "image/avif": _avif_dimensions,
        "image/bmp": _bmp_dimensions,
        "image/x-icon": _ico_dimensions,
    }
    parser = parsers.get(sniff_image_type(data))
    if parser is None:
        return None
    try:
        size = parser(data)
    except (struct.error, IndexError, ValueError):
        return None
    if size is None:
        return None
    width, height = size
    return (width, height) if width > 0 and height > 0 else None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X":  # extended: 24-bit canvas size minus one
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":  # lossy key frame
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and data[20] == 0x2F:  # lossless: 14-bit sizes minus one
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _avif_dimensions(data: bytes) -> tuple[int, int] | None:
    """Largest ``ispe`` (image spatial extents) property of an AVIF/HEIF file."""
    best: tuple[int, int] | None = None
    # Boxes that only contain other boxes (``meta`` also has a 4-byte version/flags header).
    containers = {b"meta": 4, b"iprp": 0, b"ipco": 0}

    def scan(start: int, end: int, depth: int) -> None:
        nonlocal best
        index = start
        while index + 8 <= end and depth < 8:
            size = int.from_bytes(data[index : index + 4], "big")
            kind = data[index + 4 : index + 8]
            header = 8
            if size == 1:
                size = int.from_bytes(data[index + 8 : index + 16], "big")
                header = 16
            elif size == 0:
                size = end - index
            if size < header or index + size > end:
                return
            if kind == b"ispe":
                width, height = struct.unpack(">II", data[index + 12 : index + 20])
                if best is None or width * height > best[0] * best[1]:
                    best = (width, height)
            elif kind in containers:
                scan(index + header + containers[kind], index + size, depth + 1)
            index += size

    scan(0, len(data), 0)
    return best


def _bmp_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[14] == 12:  # BITMAPCOREHEADER: 16-bit sizes
        return struct.unpack("<HH", data[18:22])
    width, height = struct.unpack("<ii", data[18:26])
    return abs(width), abs(height)  # negative height: top-down bitmap


def _ico_dimensions(data: bytes) -> tuple[int, int] | None:
    """Largest image of an icon file (PNG entries carry their real size in their header)."""
    (count,) = struct.unpack("<H", data[4:6])
    best: tuple[int, int] | None = None
    for entry in range(count):
        offset = 6 + entry * 16
        if offset + 16 > len(data):
            break
        width, height = data[offset] or 256, data[offset + 1] or 256
        (image_offset,) = struct.unpack("<I", data[offset + 12 : offset + 16])
        header = data[image_offset : image_offset + 24]
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            embedded = _png_dimensions(header)
            if embedded is not None:
                width, height = embedded
        if best is None or width * height > best[0] * best[1]:
            best = (width, height)
    return best


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    # Start-of-frame markers carrying the image size (excludes DHT/JPG/DAC).
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD9)):
            index += 2
            continue
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        if marker in sof_markers:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height) if width > 0 and height > 0 else None
        index += 2 + length
    return None


def jpeg_orientation(data: bytes) -> int | None:
    """EXIF orientation (1-8) of a JPEG image, ``None`` when absent or unreadable."""
    index = 2
    while index + 4 <= len(data) and data[index] == 0xFF:
        marker = data[index + 1]
        if marker in (0xD9, 0xDA):  # end of image / start of scan: no more metadata
            return None
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        segment = data[index + 4 : index + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return _exif_orientation(segment[6:])
        index += 2 + length
    return None


def _exif_orientation(tiff: bytes) -> int | None:
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None or len(tiff) < 8:
        return None
    try:
        (ifd,) = struct.unpack(f"{order}I", tiff[4:8])
        (count,) = struct.unpack(f"{order}H", tiff[ifd : ifd + 2])
        for entry in range(count):
            offset = ifd + 2 + entry * 12
            tag, kind = struct.unpack(f"{order}HH", tiff[offset : offset + 4])
            if tag == 0x0112 and kind == 3:  # Orientation, SHORT
                (value,) = struct.unpack(f"{order}H", tiff[offset + 8 : offset + 10])
                return value if 1 <= value <= 8 else None
    except struct.error:
        return None
    return None


def parse_data_uri(uri: str) -> tuple[str, bytes] | None:
    """Split a ``data:`` URI into (media type, payload); ``None`` when malformed."""
    if uri[:5].lower() != "data:":
        return None
    header, sep, payload = uri[5:].partition(",")
    if not sep:
        return None
    params = [p.strip().lower() for p in header.split(";")]
    media_type = params[0] or "text/plain"
    try:
        if "base64" in params[1:]:
            compact = re.sub(r"\s+", "", unquote(payload))
            return media_type, base64.b64decode(compact + "=" * (-len(compact) % 4))
        return media_type, unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return None


def to_data_uri(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


# ---------------------------------------------------------------------------
# Network address policy
# ---------------------------------------------------------------------------
def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses.

    Rejects private, loopback, link-local, multicast, reserved, unspecified,
    shared (CGNAT) and documentation ranges, and checks the IPv4 address embedded
    in IPv4-mapped, 6to4, Teredo and NAT64 IPv6 addresses.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: ipaddress.IPv4Address | None = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip.teredo:
            embedded = ip.teredo[1]
        if embedded is None and any(ip in net for net in _NAT64_NETWORKS):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_ip(embedded)
        if ip.is_site_local:
            return False
    return ip.is_global and not (
        ip.is_multicast
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


def parse_ip(value: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


def display_url(url: str) -> str:
    if url[:5].lower() == "data:":
        return "data:…"
    return url if len(url) <= _DISPLAY_URL_CHARS else url[:_DISPLAY_URL_CHARS] + "…"


def url_scheme(src: str) -> str | None:
    """Lower-case URL scheme (``"https"`` for protocol-relative ``//host/...``)."""
    if src.startswith("//"):
        return "https"
    match = _SCHEME.match(src)
    if not match or len(match.group(1)) == 1:  # "C:/..." is a Windows path, not a scheme
        return None
    return match.group(1).lower()


def is_remote(src: str) -> bool:
    return url_scheme(src) in ("http", "https")


def cache_key(url: str) -> str:
    """A URL without its ``#fragment``, which never changes the downloaded resource."""
    return url.split("#", 1)[0]


# ---------------------------------------------------------------------------
# Remote fetching
# ---------------------------------------------------------------------------
class FetchError(Exception):
    """A resource could not be used; ``message`` is a user-facing warning."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ResourceLimitError(FetchError):
    """A per-document resource limit was reached."""


class ResourceBudget:
    """Memory bounds for the images and other resources of one document.

    Every byte read from the upload, downloaded or decoded from a ``data:`` URI is
    charged (before it is read, or while it streams in) against ``max_bytes``; the
    decoded size of each distinct raster image is checked against a per-image and a
    per-document pixel limit, and the number of image elements is capped. Without
    these, a tiny upload could reference one large image thousands of times.
    """

    def __init__(
        self,
        *,
        max_bytes: int,
        max_mb: float,
        max_image_pixels: int = MAX_IMAGE_PIXELS,
        max_pixels: int = MAX_DOCUMENT_PIXELS,
        max_references: int = MAX_IMAGE_REFERENCES,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_mb = max_mb
        self.max_image_pixels = max_image_pixels
        self.max_pixels = max_pixels
        self.max_references = max_references
        self.used_bytes = 0
        self.used_pixels = 0
        self.references = 0

    def reserve(self, size: int, shown: str) -> None:
        """Charge ``size`` bytes, or raise :class:`ResourceLimitError` (nothing charged)."""
        if self.used_bytes + size > self.max_bytes:
            raise ResourceLimitError(
                f"Limite de {format_mb(self.max_mb)} MB de imagens por documento atingido; "
                f"ignorada: {shown}"
            )
        self.used_bytes += size

    def release(self, size: int) -> None:
        self.used_bytes = max(0, self.used_bytes - size)

    def check_pixels(self, media_type: str, data: bytes, shown: str) -> tuple[int, int] | None:
        """Charge the decoded size of a raster image (SVG is drawn at its displayed size).

        Returns the pixel size (``None`` for SVG); raises :class:`FetchError` when the
        size is unreadable or over a limit.
        """
        if media_type == "image/svg+xml":
            return None
        size = image_dimensions(data)
        if size is None:
            raise FetchError(f"Imagem inválida ou corrompida: {shown}")
        width, height = size
        pixels = width * height
        if pixels > self.max_image_pixels:
            raise ResourceLimitError(
                f"Imagem grande demais ({width} × {height} pixels; máximo de "
                f"{self.max_image_pixels // 1_000_000} megapixels): {shown}"
            )
        if self.used_pixels + pixels > self.max_pixels:
            raise ResourceLimitError(
                f"Limite de {self.max_pixels // 1_000_000} megapixels de imagens por documento "
                f"atingido; ignorada: {shown}"
            )
        self.used_pixels += pixels
        return size

    def take_reference(self) -> bool:
        """Count one image element; ``False`` once the per-document limit is reached."""
        self.references += 1
        return self.references <= self.max_references

    @property
    def references_limit_message(self) -> str:
        return (
            f"O documento tem mais de {self.max_references} imagens; as excedentes foram "
            "substituídas pelo texto alternativo."
        )


@dataclass(frozen=True)
class FetchedResource:
    data: bytes
    content_type: str  # media type from the response header, lower-case, no params


class RemoteFetcher:
    """Downloads remote resources for one job.

    Guarantees: only http(s); every hop of a redirect chain is re-validated; when
    private networks are not allowed, the host is resolved, *all* its addresses must
    be public and the connection is pinned to a validated address (no DNS-rebinding
    window), with TLS still verified against the original host name (SNI). Size,
    time and count limits come from the settings, and every downloaded byte is charged
    to the job's :class:`ResourceBudget`. Results are cached by URL (without fragment).
    """

    def __init__(self, settings: Settings, budget: ResourceBudget | None = None) -> None:
        self._settings = settings
        self._budget = budget or ResourceBudget(
            max_bytes=settings.max_images_total_bytes, max_mb=settings.max_images_total_mb
        )
        self._client: httpx.AsyncClient | None = None
        self._tasks: dict[str, asyncio.Task[FetchedResource]] = {}

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> FetchedResource:
        """Download ``url`` (any content type). Raises :class:`FetchError`."""
        url = cache_key(url)
        task = self._tasks.get(url)
        if task is None:
            shown = display_url(url)
            if not self._settings.allow_remote_images:
                raise FetchError(f"Download de imagens remotas desativado; ignorada: {shown}")
            if len(self._tasks) >= self._settings.max_remote_images:
                raise FetchError(
                    f"Limite de {self._settings.max_remote_images} recursos remotos por "
                    f"documento atingido; ignorado: {shown}"
                )
            task = asyncio.ensure_future(self._download(url))
            self._tasks[url] = task
        return await asyncio.shield(task)

    async def fetch_image(self, url: str) -> tuple[str, bytes]:
        """Download an image, returning (media type, bytes). Raises :class:`FetchError`."""
        resource = await self.fetch(url)
        media_type = sniff_image_type(resource.data)
        if media_type is None and resource.content_type.startswith("image/"):
            media_type = resource.content_type
        if media_type is None:
            raise FetchError(f"O endereço não contém uma imagem válida: {display_url(url)}")
        return media_type, resource.data

    async def _download(self, url: str) -> FetchedResource:
        shown = display_url(url)
        try:
            async with asyncio.timeout(self._settings.remote_timeout):
                return await self._follow(url)
        except FetchError:
            raise
        except TimeoutError:
            raise FetchError(f"Tempo esgotado ao baixar a imagem: {shown}") from None
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
            logger.info("Remote fetch failed: %s", type(exc).__name__)  # URL may hold secrets
            raise FetchError(f"Não foi possível baixar a imagem: {shown}") from None

    async def _follow(self, url: str) -> FetchedResource:
        current = httpx.URL(url)
        for _ in range(MAX_REDIRECTS + 1):
            response_or_location = await self._get(current, display_url(url))
            if isinstance(response_or_location, FetchedResource):
                return response_or_location
            current = current.join(response_or_location)
        raise FetchError(f"Redirecionamentos demais ao baixar a imagem: {display_url(url)}")

    async def _get(self, url: httpx.URL, shown: str) -> FetchedResource | str:
        """One request: returns the resource, or the redirect location to follow."""
        if url.scheme not in ("http", "https"):
            raise FetchError(f"Endereço de imagem não suportado: {shown}")
        if url.userinfo:
            raise FetchError(f"Endereços com credenciais não são permitidos: {shown}")
        host = url.raw_host.decode("ascii")
        if not host:
            raise FetchError(f"Endereço de imagem inválido: {shown}")
        port = url.port or (443 if url.scheme == "https" else 80)
        addresses = await self._resolve(host, port, shown)

        host_header = f"[{host}]" if ":" in host else host
        if url.port is not None:
            host_header += f":{url.port}"
        extensions = {"sni_hostname": host} if url.scheme == "https" else {}
        client = self._get_client()

        last_error: Exception | None = None
        for address in addresses:
            request = client.build_request(
                "GET",
                url.copy_with(host=str(address)),
                headers={"Host": host_header},
                extensions=extensions,
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.ConnectError as exc:
                last_error = exc
                continue
            try:
                return await self._read(response, shown)
            finally:
                await response.aclose()
        raise last_error or FetchError(f"Não foi possível baixar a imagem: {shown}")

    async def _read(self, response: httpx.Response, shown: str) -> FetchedResource | str:
        if response.is_redirect:
            return response.headers["location"]
        if response.status_code != 200:
            raise FetchError(f"A imagem retornou HTTP {response.status_code}: {shown}")
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise FetchError(f"Codificação de conteúdo não suportada para a imagem: {shown}")
        limit = self._settings.remote_image_max_bytes
        too_big = FetchError(
            f"Imagem maior que o limite de {format_mb(self._settings.remote_image_max_mb)} MB: "
            f"{shown}"
        )
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise too_big
        # Reserve the declared size up front (so concurrent downloads cannot starve each
        # other half-way), then charge whatever arrives beyond it.
        charged = int(declared) if declared.isdigit() else 0
        self._budget.reserve(charged, shown)
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > limit:
                    raise too_big
                if size > charged:
                    self._budget.reserve(size - charged, shown)
                    charged = size
                chunks.append(chunk)
        except BaseException:
            self._budget.release(charged)
            raise
        self._budget.release(charged - size)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return FetchedResource(data=b"".join(chunks), content_type=content_type)

    async def _resolve(self, host: str, port: int, shown: str) -> list[IPAddress]:
        literal = parse_ip(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
            except (socket.gaierror, UnicodeError):
                raise FetchError(
                    f"Não foi possível resolver o endereço da imagem: {shown}"
                ) from None
            parsed = (parse_ip(str(info[4][0])) for info in infos)
            addresses = list(dict.fromkeys(ip for ip in parsed if ip is not None))
        if not addresses:
            raise FetchError(f"Não foi possível resolver o endereço da imagem: {shown}")
        if not self._settings.allow_private_networks and not all(map(is_public_ip, addresses)):
            raise FetchError(f"Imagem remota bloqueada (endereço de rede interna): {shown}")
        return sorted(addresses, key=lambda ip: ip.version)  # IPv4 first

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,  # never route through environment proxies
                timeout=httpx.Timeout(self._settings.remote_timeout),
                # No keep-alive: a pooled connection is bound to one SNI host name.
                limits=httpx.Limits(
                    max_connections=REMOTE_CONCURRENCY * 2, max_keepalive_connections=0
                ),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "image/avif,image/webp,image/svg+xml,image/*;q=0.8,*/*;q=0.5",
                    "Accept-Encoding": "identity",
                },
            )
        return self._client


# ---------------------------------------------------------------------------
# Local files
# ---------------------------------------------------------------------------
def resolve_local_path(src: str, base_dir: Path, root_dir: Path) -> Path | None:
    """Map a relative image reference to a file path confined to ``root_dir``.

    URL-decodes, drops ``?query``/``#fragment``; a leading ``/`` is relative to
    ``root_dir`` (repository-root semantics), anything else to ``base_dir``.
    Returns ``None`` when the path escapes ``root_dir`` or is malformed.
    """
    reference = unquote(src.split("#", 1)[0].split("?", 1)[0])
    reference = unicodedata.normalize("NFC", reference).replace("\\", "/").strip()
    if not reference or "\x00" in reference:
        return None
    if reference.startswith("/"):
        candidate = root_dir / reference.lstrip("/")
    else:
        candidate = base_dir / reference
    try:
        resolved = candidate.resolve()
        root = root_dir.resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_relative_to(root) else None


# ---------------------------------------------------------------------------
# Image resolution for the AST
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedImage:
    media_type: str
    data: bytes
    source_uri: str | None = None  # original data: URI, reused when unchanged
    size: tuple[int, int] | None = None  # pixel size (raster images)

    @cached_property
    def data_uri(self) -> str:
        return self.source_uri or to_data_uri(self.media_type, self.data)

    @cached_property
    def digest(self) -> str:
        """Content hash: identical images share one copy in the output."""
        return hashlib.sha256(self.data).hexdigest()


class ImageResolver:
    """Turns ``Image`` sources into image bytes, emitting warnings on failure.

    Different spellings of one image (``a.png``, ``./a.png``, ``a.png?v=2``) resolve to
    the same file and are read once; all reads are charged to the job's budget.
    """

    def __init__(
        self,
        *,
        base_dir: Path,
        root_dir: Path,
        fetcher: RemoteFetcher,
        budget: ResourceBudget,
        max_bytes: int,
        max_mb: float,
        warn: Warn,
    ) -> None:
        self._base_dir = base_dir
        self._root_dir = root_dir
        self._fetcher = fetcher
        self._budget = budget
        self._max_bytes = max_bytes
        self._max_mb = max_mb
        self._warn = warn

    async def resolve_all(self, sources: Sequence[str]) -> dict[str, ResolvedImage | None]:
        """Resolve every source (remote downloads run concurrently, bounded)."""
        keys = await asyncio.to_thread(lambda: {src: self._canonical(src) for src in sources})
        semaphore = asyncio.Semaphore(REMOTE_CONCURRENCY)
        by_key: dict[str, asyncio.Future[ResolvedImage | None]] = {}

        async def one(src: str) -> ResolvedImage | None:
            if is_remote(src):
                async with semaphore:
                    return await self.resolve(src)
            return await self.resolve(src)

        for src in sources:
            if keys[src] not in by_key:
                by_key[keys[src]] = asyncio.ensure_future(one(src))
        try:
            await asyncio.gather(*by_key.values())
        finally:
            for task in by_key.values():
                task.cancel()
        return {src: by_key[keys[src]].result() for src in sources}

    def _canonical(self, src: str) -> str:
        """Key shared by every spelling of the same image (blocking: resolves paths)."""
        scheme = url_scheme(src)
        if scheme in ("http", "https"):
            return "url:" + cache_key(f"https:{src}" if src.startswith("//") else src)
        if scheme is None:
            path = resolve_local_path(src, self._base_dir, self._root_dir)
            if path is not None:
                return f"file:{path}"
        return "src:" + src

    async def resolve(self, src: str) -> ResolvedImage | None:
        try:
            return await self._resolve(src)
        except FetchError as exc:
            self._warn(exc.message)
            return None

    async def _resolve(self, src: str) -> ResolvedImage | None:
        shown = display_url(src)
        scheme = url_scheme(src)
        if scheme == "data":
            image = self._from_data_uri(src)
        elif scheme in ("http", "https"):
            url = f"https:{src}" if src.startswith("//") else src
            media_type, data = await self._fetcher.fetch_image(url)
            image = ResolvedImage(media_type, data)
        elif scheme is not None:
            raise FetchError(f"Endereço de imagem não suportado: {shown}")
        else:
            image = await asyncio.to_thread(self._from_local_file, src)
        if image is None:
            return None
        size = self._budget.check_pixels(image.media_type, image.data, shown)
        return ResolvedImage(image.media_type, image.data, image.source_uri, size)

    def _from_data_uri(self, src: str) -> ResolvedImage | None:
        parsed = parse_data_uri(src)
        if parsed is None or not parsed[0].startswith("image/") or not parsed[1]:
            self._warn("Imagem embutida (data URI) inválida foi ignorada.")
            return None
        declared, data = parsed
        self._budget.reserve(len(data), display_url(src))
        detected = sniff_image_type(data)
        if detected is None or detected == declared:
            return ResolvedImage(declared, data, source_uri=src)
        return ResolvedImage(detected, data)

    def _from_local_file(self, src: str) -> ResolvedImage | None:
        shown = display_url(src)
        path = resolve_local_path(src, self._base_dir, self._root_dir)
        # os.path.isfile is False on any OSError (e.g. a name longer than the OS allows).
        if path is None or not os.path.isfile(path):
            raise FetchError(f"Imagem não encontrada: {shown}")
        try:
            size = path.stat().st_size
            if size > self._max_bytes:
                raise FetchError(
                    f"Imagem maior que o limite de {format_mb(self._max_mb)} MB: {shown}"
                )
            self._budget.reserve(size, shown)
            data = path.read_bytes()
        except OSError:
            raise FetchError(f"Imagem não encontrada: {shown}") from None
        media_type = sniff_image_type(data)
        if media_type is None:
            raise FetchError(f"Formato de imagem não reconhecido: {shown}")
        return ResolvedImage(media_type, data)
