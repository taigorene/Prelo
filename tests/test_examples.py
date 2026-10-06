"""The showcase document in ``examples/`` must convert cleanly to both formats."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.utils import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    convert,
    docx_drawing_count,
    docx_paragraph_styles,
    expect_ok,
    pdf_outline_titles,
    pdf_reader,
    pdf_text,
    warnings_of,
)


def _mermaid_blocks(markdown: str) -> int:
    return len(re.findall(r"^```mermaid\s*$", markdown, flags=re.MULTILINE))


def test_example_to_pdf(client: TestClient, example_path: Path) -> None:
    response = convert(client, "pdf", example_path.read_bytes(), example_path.name, toc=True)
    expect_ok(response, PDF_MEDIA_TYPE)
    assert warnings_of(response) == []
    assert len(pdf_reader(response.content).pages) >= 5
    text = pdf_text(response.content)
    for expected in ("Guia completo do Prelo", "Diagramas Mermaid", "Notas de rodapé"):
        assert expected in text, expected
    assert any("Matemática" in title for title in pdf_outline_titles(response.content))


def test_example_to_docx(client: TestClient, example_path: Path) -> None:
    markdown = example_path.read_text(encoding="utf-8")
    response = convert(client, "docx", markdown, example_path.name)
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert warnings_of(response) == []
    # Every diagram plus the banner and the centred icon.
    assert docx_drawing_count(response.content) >= _mermaid_blocks(markdown) + 2
    headings = docx_paragraph_styles(response.content).get("Heading 1", [])
    assert "7. Diagramas Mermaid" in headings
