"""Running pandoc as a sandboxed, cancellable subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.converter.errors import ConversionError, InputTooLargeError, ServiceUnavailableError

logger = logging.getLogger("prelo.converter")

# Upper bound for pandoc's heap: a pathological document fails instead of exhausting
# the container's memory (size the container for max_concurrent_jobs times this).
RTS_OPTIONS: tuple[str, ...] = ("+RTS", "-M1g", "-RTS")
READER = "gfm"
# Largest JSON AST accepted from the Markdown reader (token-dense input makes an AST
# about 17 times its size, which Python then loads entirely).
MAX_AST_BYTES = 64 * 1024 * 1024
_HEAP_EXHAUSTED = re.compile(r"heap (exhausted|overflow)", re.IGNORECASE)
_YAML_ERROR = "Error parsing YAML metadata"
_YAML_POSITION = re.compile(r"YAML parse exception at line (\d+), column (\d+)")
_LOG_EXCERPT_CHARS = 1500
_READ_CHUNK = 1024 * 1024

MSG_TOO_COMPLEX = "O documento é grande ou complexo demais para ser convertido."
MSG_BAD_FRONT_MATTER = (
    "O cabeçalho YAML (front matter) do início do documento é inválido{where} e foi "
    "convertido como texto comum. Dica: coloque entre aspas os valores que contêm “: ”."
)


class PandocNotFoundError(ServiceUnavailableError):
    """The pandoc executable is missing or cannot be executed."""


class PandocFailedError(ConversionError):
    """pandoc exited with an error (``stderr`` is for logs, never shown to users)."""

    def __init__(self, returncode: int | None, stderr: str) -> None:
        super().__init__()
        self.returncode = returncode
        self.stderr = stderr


class _OutputTooLarge(Exception):
    pass


async def run_pandoc(
    pandoc_path: str,
    args: Sequence[str],
    *,
    stdin: bytes | None = None,
    cwd: Path | None = None,
    max_output_bytes: int | None = None,
) -> bytes:
    """Run ``pandoc --sandbox <args>`` and return its stdout.

    The process is killed when the awaiting task is cancelled (e.g. on timeout) or
    when its output exceeds ``max_output_bytes`` (:class:`InputTooLargeError`).
    Failures are logged with a stderr excerpt; users only get a generic message.
    """
    command = [pandoc_path, *RTS_OPTIONS, "--sandbox", *args]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_subprocess_env(cwd),
        )
    except OSError as exc:
        logger.error("Could not start pandoc (%s): %s", pandoc_path, exc)
        raise PandocNotFoundError() from exc

    try:
        stdout, stderr = await _communicate(process, stdin, max_output_bytes)
    except _OutputTooLarge:
        await _kill(process)
        logger.warning("pandoc output exceeded %d bytes; process killed", max_output_bytes)
        raise InputTooLargeError(MSG_TOO_COMPLEX) from None
    except BaseException:
        await _kill(process)
        raise

    messages = stderr.decode("utf-8", "replace").strip()
    if process.returncode != 0:
        logger.warning("pandoc exited with status %s: %s", process.returncode, _excerpt(messages))
        # Out of heap, or killed by the kernel's OOM killer (SIGKILL): too big a document.
        if _HEAP_EXHAUSTED.search(messages) or process.returncode == -signal.SIGKILL:
            raise InputTooLargeError(MSG_TOO_COMPLEX)
        raise PandocFailedError(process.returncode, messages)
    if messages:
        logger.debug("pandoc messages: %s", _excerpt(messages))
    return stdout


async def _communicate(
    process: asyncio.subprocess.Process, stdin: bytes | None, limit: int | None
) -> tuple[bytes, bytes]:
    """Like ``Process.communicate``, but stops reading once stdout passes ``limit``."""

    async def feed() -> None:
        if process.stdin is None:
            return
        try:
            if stdin:
                process.stdin.write(stdin)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # pandoc exited early; its status tells why
        finally:
            process.stdin.close()

    async def read_stdout() -> bytes:
        assert process.stdout is not None
        chunks: list[bytes] = []
        size = 0
        while chunk := await process.stdout.read(_READ_CHUNK):
            size += len(chunk)
            if limit is not None and size > limit:
                raise _OutputTooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    async def read_stderr() -> bytes:
        assert process.stderr is not None
        return await process.stderr.read()

    tasks = [asyncio.ensure_future(job()) for job in (read_stdout, read_stderr, feed)]
    try:
        stdout, stderr, _ = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
    await process.wait()
    return stdout, stderr


async def pandoc_version(pandoc_path: str) -> str | None:
    """Version reported by ``pandoc --version`` (``None`` when unavailable)."""
    try:
        output = await run_pandoc(pandoc_path, ["--version"])
    except ConversionError:
        return None
    first_line = output.decode("utf-8", "replace").splitlines()[0] if output else ""
    match = re.match(r"pandoc(?:\.exe)?\s+(\S+)", first_line)
    return match.group(1) if match else None


async def markdown_to_ast(
    pandoc_path: str,
    markdown: str,
    *,
    lua_filters: Sequence[Path] = (),
    cwd: Path | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Parse GitHub-flavored Markdown into pandoc's JSON AST.

    A leading ``---`` block that is not valid YAML (e.g. ``title: Parte 1: Introdução``)
    makes pandoc fail; the document is then read again with front matter disabled, as
    GitHub shows it, and ``warn`` explains what happened.
    """
    filters = [f"--lua-filter={path}" for path in lua_filters]
    data = markdown.encode("utf-8")

    async def read(reader: str) -> bytes:
        args = [f"--from={reader}", "--to=json", *filters]
        return await run_pandoc(
            pandoc_path, args, stdin=data, cwd=cwd, max_output_bytes=MAX_AST_BYTES
        )

    try:
        output = await read(READER)
    except PandocFailedError as exc:
        if _YAML_ERROR not in exc.stderr:
            raise
        output = await read(f"{READER}-yaml_metadata_block")
        if warn is not None:
            position = _YAML_POSITION.search(exc.stderr)
            where = (
                f" (linha {int(position[1]) + 1}, coluna {int(position[2]) + 1})"
                if position
                else ""
            )
            warn(MSG_BAD_FRONT_MATTER.format(where=where))
    return json.loads(output)


def encode_ast(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.shield(process.wait())


def _subprocess_env(cwd: Path | None) -> dict[str, str]:
    """Minimal environment: no proxies or user data directories leak into pandoc."""
    home = str(cwd or tempfile.gettempdir())
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": home,
        "TMPDIR": home,
    }


def _excerpt(text: str) -> str:
    text = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    return text if len(text) <= _LOG_EXCERPT_CHARS else text[:_LOG_EXCERPT_CHARS] + "…"
