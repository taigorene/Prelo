"""Upload handling: file-type checks, text decoding and safe ``.zip`` extraction."""

from __future__ import annotations

import codecs
import contextlib
import io
import re
import stat
import threading
import unicodedata
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from app.converter.errors import (
    ConversionTimeoutError,
    InputTooLargeError,
    InvalidInputError,
    UnsupportedFileError,
)
from app.converter.options import ACCEPTED_EXTENSIONS

if TYPE_CHECKING:
    from app.config import Settings

MARKDOWN_EXTENSIONS: tuple[str, ...] = (".md", ".markdown", ".mdown", ".mkd", ".mkdn")
TEXT_EXTENSIONS: tuple[str, ...] = (".txt",)
ZIP_EXTENSION = ".zip"
ZIP_MAGIC = b"PK\x03\x04"
DEFAULT_STEM = "documento"
MAX_STEM_LENGTH = 100

# Preference order for the main document inside a .zip (lower is better).
_MAIN_FILE_RANK = {"readme": 0, "index": 1}
_ZIP_FLAG_ENCRYPTED = 0x1
_ZIP_FLAG_UTF8 = 0x800
_COPY_CHUNK = 64 * 1024
# Share of control characters above which decoded text is considered binary.
_BINARY_CONTROL_RATIO = 0.05
_BINARY_SAMPLE_CHARS = 64 * 1024
_ALLOWED_CONTROL_CHARS = frozenset("\t\n\r\f\v")
# Path separators, characters Windows forbids in file names and the header delimiter ";".
_FORBIDDEN_NAME_CHARS = frozenset('<>:"/\\|?*;')
# Zero-width (non-)joiners are format characters that emoji sequences need.
_KEPT_FORMAT_CHARS = frozenset("\u200c\u200d")
# Invalid UTF-8 bytes appear as lone surrogates when decoding with "surrogateescape".
_ESCAPED_BYTE = re.compile("[\udc80-\udcff]")
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),  # must precede UTF-16 LE (same prefix)
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

MSG_EMPTY = "O arquivo Markdown está vazio."
MSG_BINARY = "O arquivo não parece ser um texto Markdown válido."
MSG_BAD_ZIP = "O arquivo .zip está corrompido ou não é um .zip válido."
MSG_ZIP_ENCRYPTED = "Arquivos .zip protegidos por senha não são suportados."
MSG_ZIP_SYMLINK = "O arquivo .zip contém links simbólicos, que não são permitidos."
MSG_ZIP_UNSAFE = "O arquivo .zip contém um caminho inválido ou inseguro: {name}"
MSG_ZIP_EXTRACT = "Não foi possível extrair o conteúdo do arquivo .zip."
MSG_ZIP_NO_MARKDOWN = "Nenhum arquivo Markdown (.md) foi encontrado dentro do .zip."
MSG_ZIP_TOO_MANY = "O arquivo .zip contém itens demais (máximo: {limit})."
MSG_ZIP_TOO_BIG = "O conteúdo descompactado do .zip excede o limite de {limit} MB."
MSG_ZIP_MAIN_TOO_BIG = "O documento Markdown {name} excede o limite de {limit} MB."
MSG_MIXED_ENCODING = (
    "O arquivo não está totalmente em UTF-8: {count} caractere(s) inválido(s) foram lidos "
    "como Windows-1252 (Latin-1)."
)


@dataclass(frozen=True)
class PreparedInput:
    markdown: str
    base_dir: Path  # directory of the main document (relative links resolve here)
    root_dir: Path  # extraction root ("/"-prefixed links resolve here)
    stem: str  # sanitized output file name without extension
    notes: tuple[str, ...] = ()  # user-facing warnings (e.g. about the text encoding)


def prepare_input(
    filename: str | None,
    data: bytes,
    workdir: Path,
    settings: Settings,
    *,
    cancel: threading.Event | None = None,
) -> PreparedInput:
    """Validate an upload and turn it into Markdown text plus its resource directories.

    Blocking (filesystem + decompression): run it in a worker thread. ``cancel`` lets
    the caller abort a running extraction.
    """
    name = base_name(filename)
    extension = file_extension(name)
    src_dir = workdir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    if extension == ZIP_EXTENSION or (not extension and data.startswith(ZIP_MAGIC)):
        return _prepare_zip(data, name, src_dir, settings, cancel)
    if extension and extension not in ACCEPTED_EXTENSIONS:
        raise UnsupportedFileError(unsupported_message(extension))
    notes: list[str] = []
    return PreparedInput(
        markdown=decode_text(data, notes),
        base_dir=src_dir,
        root_dir=src_dir,
        stem=sanitize_stem(file_stem(name)),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def base_name(filename: str | None) -> str:
    """Last path component of a client-supplied file name (handles ``\\`` too)."""
    if not filename:
        return ""
    return filename.replace("\\", "/").rsplit("/", 1)[-1].strip()


def file_extension(name: str) -> str:
    """Lower-case extension including the dot (``""`` when there is none)."""
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot and ext else ""


def file_stem(name: str) -> str:
    stem, dot, _ = name.rpartition(".")
    return stem if dot and stem else name


def sanitize_stem(stem: str) -> str:
    """Make a safe output file stem that stays close to the original name.

    Replaces control/format characters (including bidirectional overrides), path
    separators, the characters Windows forbids in file names (``<>:"/\\|?*``) and ``;``
    by spaces; keeps everything else (accents, punctuation, symbols, emoji). Collapses
    whitespace, trims spaces and dots and limits the length. Falls back to
    ``documento`` when no letter, digit or symbol is left.
    """
    normalized = unicodedata.normalize("NFC", stem)
    chars = (
        " "
        if ch in _FORBIDDEN_NAME_CHARS
        or (unicodedata.category(ch)[0] in "CZ" and ch not in _KEPT_FORMAT_CHARS)
        else ch
        for ch in normalized
    )
    cleaned = " ".join("".join(chars).split()).strip(" .")
    cleaned = cleaned[:MAX_STEM_LENGTH].strip(" .")
    return cleaned if any(ch.isalnum() or not ch.isascii() for ch in cleaned) else DEFAULT_STEM


def unsupported_message(extension: str) -> str:
    accepted = ", ".join(ACCEPTED_EXTENSIONS[:-1]) + f" ou {ACCEPTED_EXTENSIONS[-1]}"
    return (
        f"Tipo de arquivo não suportado ({_display_name(extension, 16)}). "
        f"Envie um arquivo {accepted}."
    )


# ---------------------------------------------------------------------------
# Text decoding
# ---------------------------------------------------------------------------
def decode_text(data: bytes, notes: list[str] | None = None) -> str:
    """Decode an uploaded text file and normalize line endings.

    BOM-aware (UTF-8/16/32), then UTF-8, Windows-1252 and Latin-1 (see
    :func:`_decode_bytes`). Warnings for the user are appended to ``notes``. Raises
    :class:`InvalidInputError` for binary or empty content.
    """
    text = _decode_bytes(data, notes if notes is not None else [])
    if looks_binary(text):
        raise InvalidInputError(MSG_BINARY)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise InvalidInputError(MSG_EMPTY)
    return text


def _decode_bytes(data: bytes, notes: list[str]) -> str:
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                return data[len(bom) :].decode(encoding)
            except UnicodeDecodeError:
                raise InvalidInputError(MSG_BINARY) from None
    utf16 = guess_utf16_encoding(data)
    if utf16:
        try:
            return data.decode(utf16)
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Mostly UTF-8 with a few stray legacy bytes (e.g. one line pasted from a Latin-1
    # file): keep the valid UTF-8 text and read only the invalid bytes as Windows-1252.
    text = data.decode("utf-8", "surrogateescape")
    invalid = len(_ESCAPED_BYTE.findall(text))
    valid_multibyte = sum(1 for ch in text if ord(ch) > 0x7F) - invalid
    if valid_multibyte * 4 >= invalid > 0:
        notes.append(MSG_MIXED_ENCODING.format(count=invalid))
        return _ESCAPED_BYTE.sub(lambda match: _legacy_char(ord(match[0]) - 0xDC00), text)
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _legacy_char(byte: int) -> str:
    try:
        return bytes([byte]).decode("cp1252")
    except UnicodeDecodeError:
        return chr(byte)  # Latin-1 (C1 control characters undefined in Windows-1252)


def guess_utf16_encoding(data: bytes) -> str | None:
    """Detect BOM-less UTF-16 by the distribution of NUL bytes (mostly-ASCII text)."""
    sample = data[:4096]
    pairs = len(sample) // 2
    if pairs < 2:
        return None
    even_nuls = sample[0 : pairs * 2 : 2].count(0)
    odd_nuls = sample[1 : pairs * 2 : 2].count(0)
    if odd_nuls >= pairs * 0.4 and even_nuls <= pairs * 0.05:
        return "utf-16-le"
    if even_nuls >= pairs * 0.4 and odd_nuls <= pairs * 0.05:
        return "utf-16-be"
    return None


def looks_binary(text: str) -> bool:
    """True when decoded text contains NUL or is mostly control characters."""
    if "\x00" in text:
        return True
    sample = text[:_BINARY_SAMPLE_CHARS]
    if not sample:
        return False
    controls = sum(
        1 for ch in sample if ch not in _ALLOWED_CONTROL_CHARS and unicodedata.category(ch) == "Cc"
    )
    return controls / len(sample) > _BINARY_CONTROL_RATIO


# ---------------------------------------------------------------------------
# .zip handling
# ---------------------------------------------------------------------------
def _prepare_zip(
    data: bytes,
    name: str,
    src_dir: Path,
    settings: Settings,
    cancel: threading.Event | None,
) -> PreparedInput:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError):
        raise InvalidInputError(MSG_BAD_ZIP) from None
    with archive:
        members = select_zip_members(
            archive.infolist(),
            max_files=settings.zip_max_files,
            max_total_bytes=settings.zip_max_uncompressed_bytes,
            max_total_mb=settings.zip_max_uncompressed_mb,
        )
        main = choose_main_markdown([path for _, path in members])
        if main is None:
            raise InvalidInputError(MSG_ZIP_NO_MARKDOWN)
        extract_members(
            archive,
            members,
            src_dir,
            max_total_bytes=settings.zip_max_uncompressed_bytes,
            max_total_mb=settings.zip_max_uncompressed_mb,
            cancel=cancel,
        )

    common = common_top_directory([path for _, path in members])
    root_dir = src_dir / common if common else src_dir
    main_stem = file_stem(main.name)
    zip_stem = file_stem(name)
    use_zip_name = main_stem.lower() in _MAIN_FILE_RANK and zip_stem
    main_path = src_dir.joinpath(*main.parts)
    # The upload limit also applies to the Markdown itself, not only to the .zip.
    if main_path.stat().st_size > settings.max_upload_bytes:
        raise InputTooLargeError(
            MSG_ZIP_MAIN_TOO_BIG.format(
                name=_display_name(main.as_posix()), limit=format_mb(settings.max_upload_mb)
            )
        )
    notes: list[str] = []
    return PreparedInput(
        markdown=decode_text(main_path.read_bytes(), notes),
        base_dir=src_dir / main.parent,
        root_dir=root_dir,
        stem=sanitize_stem(zip_stem if use_zip_name else main_stem),
        notes=tuple(notes),
    )


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """Entry name, NFC-normalized.

    Names stored without the UTF-8 flag are decoded by ``zipfile`` as CP437; they are
    re-read as UTF-8 when valid (macOS, Linux and recent tools), otherwise as CP850,
    the Western European OEM code page Windows archivers use (CP437 lacks letters such
    as ã, õ, Á and Ê).
    """
    name = info.filename
    if not info.flag_bits & _ZIP_FLAG_UTF8:
        with contextlib.suppress(UnicodeEncodeError):
            raw = name.encode("cp437")
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                name = raw.decode("cp850")
    return unicodedata.normalize("NFC", name)


def zip_member_path(info: zipfile.ZipInfo) -> PurePosixPath | None:
    """Validated relative path of a zip entry, or ``None`` for entries to skip.

    Skips directories and macOS metadata (``__MACOSX/``, ``.DS_Store``, ``._*``).
    Raises :class:`InvalidInputError` for absolute paths, drive letters, ``..``
    components, control characters, symbolic links and encrypted entries.
    """
    name = decode_zip_name(info)
    normalized = name.replace("\\", "/")
    if (
        normalized.startswith("/")
        or _DRIVE_LETTER.match(normalized)
        or any(unicodedata.category(ch) == "Cc" for ch in normalized)
    ):
        raise InvalidInputError(MSG_ZIP_UNSAFE.format(name=_display_name(name)))
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise InvalidInputError(MSG_ZIP_UNSAFE.format(name=_display_name(name)))
    if not parts or normalized.endswith("/"):
        return None
    if parts[0] == "__MACOSX" or parts[-1] == ".DS_Store" or parts[-1].startswith("._"):
        return None
    if stat.S_ISLNK(info.external_attr >> 16):
        raise InvalidInputError(MSG_ZIP_SYMLINK)
    if info.flag_bits & _ZIP_FLAG_ENCRYPTED:
        raise InvalidInputError(MSG_ZIP_ENCRYPTED)
    return PurePosixPath(*parts)


def _display_name(name: str, limit: int = 120) -> str:
    shown = "".join("?" if unicodedata.category(ch) == "Cc" else ch for ch in name)
    return shown if len(shown) <= limit else shown[:limit] + "…"


def select_zip_members(
    infos: Sequence[zipfile.ZipInfo],
    *,
    max_files: int,
    max_total_bytes: int,
    max_total_mb: float,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Validate all entries (count, declared size, paths) and return those to extract."""
    if len(infos) > max_files:
        raise InputTooLargeError(MSG_ZIP_TOO_MANY.format(limit=max_files))
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    declared = 0
    for info in infos:
        path = zip_member_path(info)
        if path is None:
            continue
        declared += info.file_size
        if declared > max_total_bytes:
            raise InputTooLargeError(MSG_ZIP_TOO_BIG.format(limit=format_mb(max_total_mb)))
        members.append((info, path))
    return members


def extract_members(
    archive: zipfile.ZipFile,
    members: Sequence[tuple[zipfile.ZipInfo, PurePosixPath]],
    dest: Path,
    *,
    max_total_bytes: int,
    max_total_mb: float,
    cancel: threading.Event | None = None,
) -> None:
    """Stream entries to ``dest`` counting the real decompressed size (zip-bomb guard)."""
    dest_resolved = dest.resolve()
    total = 0
    try:
        for info, rel in members:
            target = dest.joinpath(*rel.parts)
            if not target.resolve().is_relative_to(dest_resolved):
                raise InvalidInputError(MSG_ZIP_UNSAFE.format(name=rel.as_posix()))
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as sink:
                while chunk := source.read(_COPY_CHUNK):
                    total += len(chunk)
                    if total > max_total_bytes:
                        raise InputTooLargeError(
                            MSG_ZIP_TOO_BIG.format(limit=format_mb(max_total_mb))
                        )
                    if cancel is not None and cancel.is_set():
                        raise ConversionTimeoutError()
                    sink.write(chunk)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error, OSError):
        raise InvalidInputError(MSG_ZIP_EXTRACT) from None


def choose_main_markdown(paths: Sequence[PurePosixPath]) -> PurePosixPath | None:
    """Pick the main document: the only Markdown file, else the shallowest README/index,
    else the shallowest (then alphabetically first) one. ``.txt`` files are only used
    when there is no Markdown file at all.
    """
    candidates = [p for p in paths if p.suffix.lower() in MARKDOWN_EXTENSIONS]
    if not candidates:
        candidates = [p for p in paths if p.suffix.lower() in TEXT_EXTENSIONS]
    if len(candidates) <= 1:
        return candidates[0] if candidates else None

    def depth_key(path: PurePosixPath) -> tuple[int, str]:
        return len(path.parts), path.as_posix().lower()

    preferred = [p for p in candidates if p.stem.lower() in _MAIN_FILE_RANK]
    if preferred:
        return min(
            preferred,
            key=lambda p: (len(p.parts), _MAIN_FILE_RANK[p.stem.lower()], p.as_posix().lower()),
        )
    return min(candidates, key=depth_key)


def common_top_directory(paths: Sequence[PurePosixPath]) -> str | None:
    """Name of the single top-level folder wrapping every entry, if there is one."""
    tops = {path.parts[0] for path in paths}
    if len(tops) == 1 and all(len(path.parts) > 1 for path in paths):
        return tops.pop()
    return None


def format_mb(value: float) -> str:
    """``20`` → ``"20"``, ``0.5`` → ``"0,5"`` (pt-BR decimal comma)."""
    return f"{value:g}".replace(".", ",")
