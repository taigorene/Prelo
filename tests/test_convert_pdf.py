"""Markdown -> PDF through the multipart endpoint."""

from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.utils import (
    PDF_MEDIA_TYPE,
    convert,
    expect_ok,
    pdf_image_count,
    pdf_outline_titles,
    pdf_page_size,
    pdf_page_texts,
    pdf_reader,
    pdf_text,
    warnings_of,
)

MM_TO_PT = 72 / 25.4


@pytest.fixture(scope="module")
def complete_pdf(client: TestClient, complete_md: bytes) -> httpx.Response:
    return expect_ok(convert(client, "pdf", complete_md, "complete.md"), PDF_MEDIA_TYPE)


def test_complete_sample_keeps_the_content(complete_pdf: httpx.Response) -> None:
    text = pdf_text(complete_pdf.content)
    expected = [
        "Relatório de Testes",  # YAML front matter title
        "Visão geral",
        "coração",
        "informação",
        "Nível três",
        "Ordenado no quarto nível",
        "calcular_total",
        "somarValores",
        "codigo_inline",
        "Tarefa concluída",
        "Alfa",
        "Beta",
        "Gama",
        "Conversão",
        "Texto do alerta de nota",
        "Conteúdo oculto do bloco details",  # <details> is forced open for print
        "Texto da nota de rodapé",
    ]
    missing = [item for item in expected if item not in text]
    assert not missing, f"missing from the PDF text: {missing}"
    assert "segredo-do-comentario" not in text  # HTML comments are never rendered


def test_alert_titles_follow_the_document_language(
    client: TestClient, complete_pdf: httpx.Response
) -> None:
    # complete.md declares ``lang: pt-BR``: pandoc's English alert titles are translated.
    assert "Nota" in pdf_text(complete_pdf.content)
    english = b"> [!WARNING]\n> Cuidado com o degrau.\n"
    text = pdf_text(expect_ok(convert(client, "pdf", english, "alerta.md"), PDF_MEDIA_TYPE).content)
    assert "Warning" in text  # no ``lang``: the GitHub (English) title is kept


def test_complete_sample_has_no_warnings(complete_pdf: httpx.Response) -> None:
    assert warnings_of(complete_pdf) == []


def test_complete_sample_embeds_raster_image(complete_pdf: httpx.Response) -> None:
    assert pdf_image_count(complete_pdf.content) >= 1


def test_default_page_is_a4_portrait(complete_pdf: httpx.Response) -> None:
    width, height = pdf_page_size(complete_pdf.content)
    assert width == pytest.approx(210 * MM_TO_PT, abs=2)
    assert height == pytest.approx(297 * MM_TO_PT, abs=2)


def test_outline_lists_headings(complete_pdf: httpx.Response) -> None:
    titles = pdf_outline_titles(complete_pdf.content)
    assert any("Visão geral" in title for title in titles), titles


@pytest.mark.parametrize(
    ("page_size", "orientation", "expected_mm"),
    [
        ("Letter", "portrait", (215.9, 279.4)),
        ("A4", "landscape", (297, 210)),
        ("A5", "portrait", (148, 210)),
    ],
)
def test_page_size_and_orientation(
    client: TestClient, page_size: str, orientation: str, expected_mm: tuple[float, float]
) -> None:
    response = convert(
        client, "pdf", "# Página\n\nTexto.", page_size=page_size, orientation=orientation
    )
    width, height = pdf_page_size(expect_ok(response, PDF_MEDIA_TYPE).content)
    assert width == pytest.approx(expected_mm[0] * MM_TO_PT, abs=2)
    assert height == pytest.approx(expected_mm[1] * MM_TO_PT, abs=2)


@pytest.mark.parametrize("page_numbers", [True, False])
def test_page_number_footer(client: TestClient, page_numbers: bool) -> None:
    markdown = "# Paginação\n\n" + "\n\n".join(f"Parágrafo número {n}." for n in range(1, 120))
    response = convert(client, "pdf", markdown, page_numbers=page_numbers)
    content = expect_ok(response, PDF_MEDIA_TYPE).content
    pages = pdf_page_texts(content)
    assert len(pages) >= 2
    footer = re.compile(rf"\b1\s*/\s*{len(pages)}\b")
    assert bool(footer.search(pages[0])) is page_numbers, pages[0][-200:]


def test_table_of_contents(client: TestClient, complete_md: bytes) -> None:
    response = convert(client, "pdf", complete_md, toc=True)
    text = pdf_text(expect_ok(response, PDF_MEDIA_TYPE).content)
    assert text.count("Lista aninhada") >= 2  # once in the TOC, once as the heading
    assert text.count("Matemática") >= 2


def test_latin1_input_is_decoded(client: TestClient, latin1_md: bytes) -> None:
    response = convert(client, "pdf", latin1_md, "latin1.md")
    text = pdf_text(expect_ok(response, PDF_MEDIA_TYPE).content)
    assert "Relatório em Latin-1" in text
    assert "Coração, ação, informação" in text


@pytest.mark.parametrize("theme", ["default", "neutral", "dark", "forest"])
def test_mermaid_themes(client: TestClient, theme: str) -> None:
    markdown = "# Tema\n\n```mermaid\nflowchart LR\n    A[Início] --> B[Fim]\n```\n"
    response = expect_ok(convert(client, "pdf", markdown, mermaid_theme=theme), PDF_MEDIA_TYPE)
    assert warnings_of(response) == []
    assert len(pdf_reader(response.content).pages) == 1
