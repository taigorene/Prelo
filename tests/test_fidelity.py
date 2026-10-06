"""Layout fidelity: wide content, links, alignment, raw HTML, front matter, task lists."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ClientFactory
from tests.utils import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    convert,
    docx_paragraph_styles,
    docx_text,
    docx_xml,
    expect_ok,
    pdf_text,
    pdf_text_sizes,
    warnings_of,
)

# --------------------------------------------------------------------------- PDF: wide content


def _body_size(client: TestClient, extra: str) -> float:
    markdown = f"# Título\n\nParágrafo de controle Referencia.\n\n{extra}\n"
    content = expect_ok(convert(client, "pdf", markdown), PDF_MEDIA_TYPE).content
    sizes = pdf_text_sizes(content, "Referencia")
    assert sizes, pdf_text(content)
    return sizes[0]


@pytest.mark.parametrize(
    "wide",
    [
        "| "
        + " | ".join(f"Coluna {n}" for n in range(16))
        + " |\n|"
        + "---|" * 16
        + "\n| "
        + " | ".join("valor" for _ in range(16))
        + " |",
        "| Caminho | Uso |\n|---|---|\n| /var/lib/aplicacao/dados/" + "x" * 120 + " | log |",
        "$$" + " + ".join(f"\\frac{{a_{{{n}}}}}{{b_{{{n}}}}}" for n in range(40)) + "$$",
    ],
    ids=["16-columns", "long-path", "long-equation"],
)
def test_one_wide_element_does_not_shrink_the_whole_pdf(client: TestClient, wide: str) -> None:
    control = _body_size(client, "Sem nada largo.")
    assert _body_size(client, wide) == pytest.approx(control, rel=0.01)


# --------------------------------------------------------------------------- PDF: links


def test_pdf_relative_links_stay_relative(client: TestClient) -> None:
    markdown = (
        "# Links\n\n[Guia](docs/guia.md), [Licença](LICENSE), [raiz](/docs/x.md), "
        '[seção](#links), <a href="CONTRIBUTING.md">contribuir</a> e '
        "[web](https://example.com/a).\n"
    )
    content = expect_ok(convert(client, "pdf", markdown), PDF_MEDIA_TYPE).content
    assert b"document.local" not in content
    uris = set(re.findall(rb"/URI \(([^)]*)\)", content))
    assert {b"docs/guia.md", b"LICENSE", b"docs/x.md", b"CONTRIBUTING.md"} <= uris, uris
    assert b"https://example.com/a" in uris


# --------------------------------------------------------------------------- front matter


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
@pytest.mark.parametrize(
    "front_matter",
    [
        "---\ntitle: Parte 1: Introdução\n---\n",
        '---\ntitle: "sem fim\n---\n',
        "---\n**Atenção**: leia isto antes de começar\n---\n",
    ],
    ids=["unquoted-colon", "unterminated-quote", "bold-line"],
)
def test_invalid_front_matter_is_kept_as_text(
    client: TestClient, fmt: str, front_matter: str
) -> None:
    response = convert(client, fmt, f"{front_matter}\n# Manual\n\nCorpo do texto.\n")
    assert response.status_code == 200, response.text
    assert any("YAML" in warning for warning in warnings_of(response)), warnings_of(response)
    text = pdf_text(response.content) if fmt == "pdf" else docx_text(response.content)
    assert "Corpo do texto." in text


def test_front_matter_error_names_the_line(client: TestClient) -> None:
    response = convert(client, "pdf", "---\ntitle: a: b: c\n---\n\n# Corpo\n")
    assert any("linha 2, coluna 9" in warning for warning in warnings_of(response))


# --------------------------------------------------------------------------- task lists


def test_ordered_task_lists(client: TestClient) -> None:
    markdown = "# Passos\n\n1. [ ] primeiro passo\n2. [x] segundo passo\n3. terceiro\n"
    pdf = expect_ok(convert(client, "pdf", markdown), PDF_MEDIA_TYPE).content
    assert "[ ]" not in pdf_text(pdf) and "[x]" not in pdf_text(pdf)
    docx = expect_ok(convert(client, "docx", markdown), DOCX_MEDIA_TYPE).content
    text = docx_text(docx)
    assert "☐ primeiro passo" in text and "☒ segundo passo" in text


# --------------------------------------------------------------------------- DOCX: raw HTML


def _paragraph_alignment(xml: str, text: str) -> str | None:
    for paragraph in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.DOTALL):
        if text in "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", paragraph)):
            match = re.search(r'<w:jc w:val="(\w+)"', paragraph)
            return match.group(1) if match else None
    raise AssertionError(f"paragraph not found: {text}")


def test_docx_keeps_html_alignment(client: TestClient) -> None:
    markdown = """<h1 align="center">Projeto Centralizado</h1>

<p align="center">Uma frase de efeito.</p>

<p align="right">Alinhado à direita</p>

<div align="center">

**Markdown dentro de uma div**

</div>

Parágrafo normal.
"""
    content = expect_ok(convert(client, "docx", markdown), DOCX_MEDIA_TYPE).content
    styles = docx_paragraph_styles(content)
    assert "Uma frase de efeito." in styles.get("Centered", []), styles
    assert "Alinhado à direita" in styles.get("Right Aligned", []), styles
    assert "Markdown dentro de uma div" in styles.get("Centered", []), styles
    xml = docx_xml(content)
    assert _paragraph_alignment(xml, "Projeto Centralizado") == "center"
    assert _paragraph_alignment(xml, "Parágrafo normal.") is None


def test_docx_pre_becomes_a_code_block(client: TestClient) -> None:
    markdown = "# Árvore\n\n<pre>\nprojeto/\n├── src/\n│   └── main.py\n</pre>\n"
    content = expect_ok(convert(client, "docx", markdown), DOCX_MEDIA_TYPE).content
    code = docx_paragraph_styles(content).get("Source Code", [])
    assert any("│   └── main.py" in text for text in code), code


def test_docx_br_at_line_end_adds_no_leading_space(client: TestClient) -> None:
    markdown = "# FAQ\n\nP: Funciona offline?<br>\nR: Sim, desde a versão 0.4.\n"
    xml = docx_xml(expect_ok(convert(client, "docx", markdown), DOCX_MEDIA_TYPE).content)
    assert re.search(r"<w:br\s*/>", xml)
    assert not re.search(r'<w:br\s*/></w:r><w:r><w:t xml:space="preserve"> ', xml)


def test_docx_heading_code_scales_with_the_heading(client: TestClient) -> None:
    markdown = "# Uso\n\n## A função `converter()`\n\nTexto com `código` comum.\n"
    xml = docx_xml(expect_ok(convert(client, "docx", markdown), DOCX_MEDIA_TYPE).content)
    assert re.search(r'w:val="VerbatimHeadingChar".*?converter\(\)', xml, flags=re.DOTALL)
    assert re.search(r'w:val="VerbatimChar".*?código', xml, flags=re.DOTALL)


@pytest.mark.parametrize(
    "hostile",
    [
        "<details>\n" + "<summary>x" * 36_000,
        "<div>\n" + "<!--a" * 60_000,
        "<b>x" * 20_000 + "</i>" * 20_000,
    ],
    ids=["unclosed-summaries", "unclosed-comments", "deep-inline-tags"],
)
def test_docx_hostile_raw_html_converts_quickly(
    client_factory: ClientFactory, hostile: str
) -> None:
    # Took 5-50 s with backtracking patterns; now well under a second.
    client = client_factory(conversion_timeout=20)
    response = convert(client, "docx", f"# Hostil\n\n{hostile}\n\nFim.\n")
    assert response.status_code == 200, response.text


def test_mermaid_error_names_the_diagram_position(client: TestClient) -> None:
    good = "```mermaid\nflowchart LR\n  A --> B\n```\n\n"
    bad = "```mermaid\nisto não é um diagrama\n```\n"
    response = expect_ok(convert(client, "pdf", f"# D\n\n{good * 3}{bad}"), PDF_MEDIA_TYPE)
    (warning,) = [w for w in warnings_of(response) if "Mermaid" in w]
    assert "nº 4:" in warning, warning
