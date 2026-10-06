"""The engine's public interface (``ConversionService``), used directly without HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.converter import (
    ConversionOptions,
    InputTooLargeError,
    InvalidInputError,
    OutputFormat,
    ServiceUnavailableError,
    UnsupportedFileError,
)
from app.converter.browser import RendererCrashedError
from app.converter.service import (
    QUEUE_PER_SLOT,
    ConversionResult,
    ConversionService,
    WarningCollector,
)
from tests.utils import docx_document, make_settings, pdf_text

UPLOAD_LIMIT_MB = 0.05


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def service() -> AsyncIterator[ConversionService]:
    engine = ConversionService(make_settings(max_upload_mb=UPLOAD_LIMIT_MB))
    await engine.start()
    try:
        yield engine
    finally:
        await engine.stop()


async def test_not_started_service_is_unavailable() -> None:
    engine = ConversionService(make_settings())
    assert not engine.is_ready()
    with pytest.raises(ServiceUnavailableError):
        await engine.convert(
            data=b"# Oi\n", filename="oi.md", fmt=OutputFormat.PDF, options=ConversionOptions()
        )


async def test_health_and_versions(service: ConversionService) -> None:
    assert service.is_ready()
    assert await service.health() == {"pandoc": True, "browser": True}
    versions = await service.versions()
    assert {"pandoc", "chromium", "mermaid"} <= set(versions)
    assert versions["pandoc"].startswith("3.")
    assert all(isinstance(value, str) and value for value in versions.values())


async def test_pdf_result_contract(service: ConversionService) -> None:
    result = await service.convert(
        data="# Nota rápida\n\nTexto.".encode(),
        filename="nota.md",
        fmt=OutputFormat.PDF,
        options=ConversionOptions(),
    )
    assert isinstance(result, ConversionResult)
    assert result.content.startswith(b"%PDF-")
    assert result.media_type == OutputFormat.PDF.media_type
    assert result.filename == "nota.pdf"
    assert result.warnings == []
    assert isinstance(result.duration_ms, int) and result.duration_ms >= 0
    assert "Nota rápida" in pdf_text(result.content)


async def test_docx_result_contract(service: ConversionService) -> None:
    result = await service.convert(
        data="# Relatório\n\n![falta](imagens/falta.png)\n".encode(),
        filename="relatório.markdown",
        fmt=OutputFormat.DOCX,
        options=ConversionOptions(orientation="landscape"),
    )
    assert result.media_type == OutputFormat.DOCX.media_type
    assert result.filename == "relatório.docx"
    assert len(result.warnings) == 1
    assert "falta.png" in result.warnings[0]
    assert docx_document(result.content).sections[-1].page_width.mm > 280


@pytest.mark.parametrize(
    ("data", "filename", "error"),
    [
        (b"", "vazio.md", InvalidInputError),
        (b"\x00\x01\x02\x03" * 64, "binario.md", InvalidInputError),
        (b"PK\x03\x04quebrado", "quebrado.zip", InvalidInputError),
        (b"# Oi\n", "programa.exe", UnsupportedFileError),
        (b"a" * int(UPLOAD_LIMIT_MB * 1024 * 1024 + 1), "grande.md", InputTooLargeError),
    ],
    ids=["empty", "binary", "bad-zip", "extension", "too-large"],
)
async def test_expected_failures_raise_conversion_errors(
    service: ConversionService, data: bytes, filename: str, error: type[Exception]
) -> None:
    with pytest.raises(error) as excinfo:
        await service.convert(
            data=data, filename=filename, fmt=OutputFormat.PDF, options=ConversionOptions()
        )
    assert excinfo.value.message  # user-facing pt-BR message
    assert service.is_ready()  # a failed job never takes the engine down


async def test_lifecycle() -> None:
    engine = ConversionService(make_settings())
    await engine.start()
    try:
        assert engine.is_ready()
    finally:
        await engine.stop()
    assert not engine.is_ready()


async def test_waiting_queue_is_bounded() -> None:
    engine = ConversionService(make_settings(max_concurrent_jobs=1))
    entered: list[int] = []

    async def wait_for_slot(number: int) -> None:
        async with engine._slot():
            entered.append(number)

    async with engine._slot():  # the only slot is busy
        waiting = [asyncio.create_task(wait_for_slot(n)) for n in range(QUEUE_PER_SLOT)]
        await asyncio.sleep(0)
        with pytest.raises(ServiceUnavailableError):  # the queue is full: fail fast
            async with engine._slot():
                pass
    await asyncio.gather(*waiting)
    assert entered == list(range(QUEUE_PER_SLOT))


async def test_renderer_crash_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ConversionService(make_settings())
    attempts: list[int] = []

    async def flaky_job(*_: object) -> tuple[bytes, str, WarningCollector]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RendererCrashedError("Page.pdf: Target crashed")
        return b"%PDF-", "doc", WarningCollector()

    monkeypatch.setattr(engine, "_run_job", flaky_job)
    content, _, _ = await engine._run_with_timeout(
        b"# x", "x.md", OutputFormat.PDF, ConversionOptions()
    )
    assert content == b"%PDF-" and len(attempts) == 2


async def test_repeated_renderer_crash_means_too_complex(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ConversionService(make_settings())

    async def crashing_job(*_: object) -> tuple[bytes, str, WarningCollector]:
        raise RendererCrashedError("Page.pdf: Printing failed")

    monkeypatch.setattr(engine, "_run_job", crashing_job)
    with pytest.raises(InputTooLargeError):
        await engine._run_with_timeout(b"# x", "x.md", OutputFormat.PDF, ConversionOptions())


async def test_mermaid_page_has_no_network(service: ConversionService) -> None:
    """Even with script execution in the Mermaid page, no socket reaches the network."""
    connections: list[object] = []

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer.get_extra_info("peername"))
        writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    probe = f"""async () => {{
        const results = [];
        try {{ await fetch('http://127.0.0.1:{port}/'); results.push('fetch'); }}
        catch (error) {{ results.push('fetch-blocked'); }}
        await new Promise((done) => {{
            let socket;
            try {{ socket = new WebSocket('ws://127.0.0.1:{port}/'); }}
            catch (error) {{ results.push('ws-blocked'); done(); return; }}
            socket.onopen = () => {{ results.push('ws-open'); done(); }};
            socket.onerror = () => {{ results.push('ws-error'); done(); }};
            setTimeout(done, 2000);
        }});
        return results;
    }}"""
    try:
        # The Mermaid page: CSP, request interception and WebSocket routing.
        async with service._mermaid.page(theme="default", width_px=600) as page:
            results = await page._page.evaluate(probe)
        assert "fetch" not in results, results
        # Any page of the browser: the launch flags alone keep sockets off the network.
        async with service._browser.context() as context:
            bare = await context.new_page()
            results = await bare.evaluate(probe)
        assert "fetch" not in results and "ws-open" not in results, results
        await asyncio.sleep(0.2)
    finally:
        server.close()
        await server.wait_closed()
    assert connections == []
