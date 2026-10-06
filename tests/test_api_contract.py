"""HTTP contract: JSON text endpoint, validation errors, response headers, concurrency, timeouts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from docx.enum.section import WD_ORIENT
from fastapi.testclient import TestClient

from tests.conftest import ClientFactory
from tests.utils import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    ascii_filename,
    convert,
    docx_document,
    docx_paragraph_styles,
    docx_text,
    download_filename,
    expect_error,
    expect_ok,
    make_settings,
    pdf_text,
    warnings_of,
)

TEXT_MARKDOWN = """# Relatório trimestral

| Mês | Receita |
|:----|--------:|
| Janeiro | R$ 10.000,00 |
| Fevereiro | R$ 12.500,00 |

```mermaid
pie
    "Janeiro" : 10
    "Fevereiro" : 12.5
```
"""

# --------------------------------------------------------------------------- JSON text endpoint


def test_text_endpoint_to_pdf(client: TestClient) -> None:
    payload = {"markdown": TEXT_MARKDOWN, "filename": "Relatório Anual"}
    response = expect_ok(client.post("/api/v1/convert/pdf/text", json=payload), PDF_MEDIA_TYPE)
    assert download_filename(response) == "Relatório Anual.pdf"
    assert warnings_of(response) == []
    text = pdf_text(response.content)
    assert "Relatório trimestral" in text
    assert "Fevereiro" in text


def test_text_endpoint_to_docx_with_options(client: TestClient) -> None:
    payload = {
        "markdown": TEXT_MARKDOWN,
        "options": {"orientation": "landscape", "page_size": "A3", "page_numbers": False},
    }
    response = expect_ok(client.post("/api/v1/convert/docx/text", json=payload), DOCX_MEDIA_TYPE)
    assert download_filename(response) == "documento.docx"
    section = docx_document(response.content).sections[-1]
    assert section.orientation == WD_ORIENT.LANDSCAPE
    assert section.page_width.mm == pytest.approx(420, abs=1)
    assert docx_paragraph_styles(response.content).get("Heading 1") == ["Relatório trimestral"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"markdown": ""},
        {"markdown": 42},
        {"markdown": "# Oi", "options": {"page_size": "B5"}},
        {"markdown": "# Oi", "options": {"orientation": "diagonal"}},
        {"markdown": "# Oi", "options": {"cor": "azul"}},
    ],
    ids=["missing", "empty", "not-a-string", "page-size", "orientation", "unknown-option"],
)
def test_text_endpoint_validation(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.post("/api/v1/convert/pdf/text", json=payload)
    expect_error(response, 422, "validation_error")


def test_text_endpoint_malformed_json(client: TestClient) -> None:
    response = client.post(
        "/api/v1/convert/pdf/text",
        content=b'{"markdown": ',
        headers={"Content-Type": "application/json"},
    )
    expect_error(response, 422, "validation_error")


# --------------------------------------------------------------------------- multipart validation


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_size", "B5"),
        ("orientation", "diagonal"),
        ("margin", "enorme"),
        ("mermaid_theme", "rosa"),
        ("toc", "talvez"),
    ],
)
def test_multipart_option_validation(client: TestClient, field: str, value: str) -> None:
    response = convert(client, "pdf", "# Oi\n", **{field: value})
    expect_error(response, 422, "validation_error")


def test_unknown_output_format(client: TestClient) -> None:
    expect_error(convert(client, "odt", "# Oi\n"), 422, "validation_error")


def test_multipart_options_are_applied(client: TestClient) -> None:
    response = convert(
        client, "docx", "# Opções\n\nTexto.", toc=True, page_numbers=False, margin="narrow"
    )
    section = docx_document(expect_ok(response, DOCX_MEDIA_TYPE).content).sections[-1]
    assert section.left_margin.mm == pytest.approx(12.7, abs=0.5)
    assert section.footer.is_linked_to_previous


# --------------------------------------------------------------------------- response headers


def test_response_headers(client: TestClient) -> None:
    response = expect_ok(convert(client, "pdf", "# Cabeçalhos\n", "cabecalhos.md"), PDF_MEDIA_TYPE)
    assert response.headers["content-disposition"].startswith("attachment")
    assert download_filename(response) == "cabecalhos.pdf"
    assert int(response.headers["x-conversion-time-ms"]) >= 0
    assert warnings_of(response) == []


def test_cors_exposes_conversion_headers(client: TestClient) -> None:
    response = client.post(
        "/api/v1/convert/docx",
        files={"file": ("cors.md", b"# CORS\n", "text/markdown")},
        headers={"Origin": "https://exemplo.com.br"},
    )
    expect_ok(response, DOCX_MEDIA_TYPE)
    exposed = response.headers.get("access-control-expose-headers", "").lower()
    for header in ("content-disposition", "x-conversion-warnings", "x-conversion-time-ms"):
        assert header in exposed, exposed


def test_accented_filename(client: TestClient) -> None:
    response = convert(client, "pdf", "# Olá\n", "relatório técnico.md")
    expect_ok(response, PDF_MEDIA_TYPE)
    assert download_filename(response) == "relatório técnico.pdf"
    fallback = ascii_filename(response)
    if fallback is not None:
        assert fallback.isascii() and fallback.endswith(".pdf")


@pytest.mark.parametrize(
    "filename",
    ['aspas"e;ponto-e-vírgula.md', "../../etc/relatório.md", "barra\\invertida.md"],
)
def test_hostile_filenames_are_sanitized(client: TestClient, filename: str) -> None:
    response = expect_ok(convert(client, "docx", "# Oi\n", filename), DOCX_MEDIA_TYPE)
    name = download_filename(response)
    assert name.endswith(".docx")
    assert not any(char in name for char in '"/\\;\r\n'), name
    assert "\r" not in response.headers["content-disposition"]


def test_long_filename_is_truncated(client: TestClient) -> None:
    response = expect_ok(convert(client, "pdf", "# Oi\n", "a" * 300 + ".md"), PDF_MEDIA_TYPE)
    name = download_filename(response)
    assert name.endswith(".pdf")
    assert len(name) <= 100 + len(".pdf")


def test_unusable_filename_falls_back(client: TestClient) -> None:
    response = expect_ok(convert(client, "pdf", "# Oi\n", "???.md"), PDF_MEDIA_TYPE)
    assert download_filename(response) == "documento.pdf"


def test_warnings_header_stays_small_and_valid(client: TestClient) -> None:
    images = "\n\n".join(f"![img {n}](imagens/{'x' * 90}-{n}.png)" for n in range(80))
    response = expect_ok(convert(client, "docx", f"# Avisos\n\n{images}\n"), DOCX_MEDIA_TYPE)
    assert len(response.headers["x-conversion-warnings"].encode()) <= 4096
    warnings = warnings_of(response)
    assert warnings
    # The total counts every warning, also those that did not fit in the header.
    assert response.headers["x-conversion-warnings-count"] == "80"
    shown = len(warnings) - 1
    assert warnings[-1] == f"… e mais {80 - shown} aviso(s) omitido(s).", warnings[-1]


def test_warnings_count_header(client: TestClient) -> None:
    response = expect_ok(convert(client, "pdf", "# Ok\n\n![x](falta.png)\n"), PDF_MEDIA_TYPE)
    assert response.headers["x-conversion-warnings-count"] == "1"
    assert warnings_of(response) == ["Imagem não encontrada: falta.png"]


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ("Relatório %22final%22 (1).md", "Relatório final (1).pdf"),
        ("a%22b%22.md", "a b.pdf"),
        ("Q&A.md", "Q&A.pdf"),
        ("C++ guia.md", "C++ guia.pdf"),
        ("C# básico.md", "C# básico.pdf"),
        ("Notas 2024, v2.md", "Notas 2024, v2.pdf"),
        ("100% pronto.md", "100% pronto.pdf"),
        ("O'Reilly.md", "O'Reilly.pdf"),
        ("📘 Manual.md", "📘 Manual.pdf"),
        ("😀.md", "😀.pdf"),
        ("fatura\u202egnp.md", "fatura gnp.pdf"),  # no bidirectional override
    ],
)
def test_filenames_keep_their_characters(client: TestClient, sent: str, expected: str) -> None:
    response = expect_ok(convert(client, "pdf", "# Oi\n", sent), PDF_MEDIA_TYPE)
    assert download_filename(response) == expected


# --------------------------------------------------------------------------- concurrency / timeout


def test_concurrent_conversions_do_not_interfere(client: TestClient) -> None:
    jobs = [("pdf", "A"), ("docx", "B"), ("pdf", "C")]

    def run(job: tuple[str, str]) -> tuple[str, str, httpx.Response]:
        fmt, label = job
        markdown = (
            f"# Documento {label}\n\nConteúdo exclusivo {label}.\n\n"
            "```mermaid\nflowchart LR\n"
            f"    {label}1[Início {label}] --> {label}2[Fim {label}]\n"
            "```\n"
        )
        return fmt, label, convert(client, fmt, markdown, f"doc-{label}.md")

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(run, jobs))

    for fmt, label, response in results:
        expect_ok(response, PDF_MEDIA_TYPE if fmt == "pdf" else DOCX_MEDIA_TYPE)
        assert download_filename(response) == f"doc-{label}.{fmt}"
        assert warnings_of(response) == []
        text = pdf_text(response.content) if fmt == "pdf" else docx_text(response.content)
        assert f"Conteúdo exclusivo {label}." in text
        others = {other for _, other, _ in results} - {label}
        assert not any(f"Conteúdo exclusivo {other}." in text for other in others)


def test_conversion_timeout(client_factory: ClientFactory) -> None:
    slow = client_factory(conversion_timeout=0.01)
    markdown = "# Lento\n\n```mermaid\nflowchart LR\n    A --> B\n```\n"
    expect_error(convert(slow, "pdf", markdown), 504, "timeout")
    health = slow.get("/api/v1/health")
    assert health.status_code == 200, "the engine must stay healthy after a timeout"


async def test_client_disconnect_cancels_the_conversion() -> None:
    """A conversion whose client went away stops (and frees its slot) instead of running on."""
    from app.main import create_app

    started, cancelled = asyncio.Event(), asyncio.Event()

    class SlowService:
        async def convert(self, **_: Any) -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    app = create_app(make_settings())
    app.state.converter = SlowService()
    body = b'{"markdown": "# Oi"}'
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/convert/pdf/text",
        "raw_path": b"/api/v1/convert/pdf/text",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    gone = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if pending:
            return pending.pop(0)
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        pass

    request = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(started.wait(), 10)
    gone.set()
    await asyncio.wait_for(cancelled.wait(), 5)
    await asyncio.wait_for(request, 5)
