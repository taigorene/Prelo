"""Mermaid diagrams: errors never fail a job, and diagrams are found at any nesting depth."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.utils import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    convert,
    docx_drawing_count,
    docx_text,
    docx_xml,
    expect_ok,
    pdf_text,
    warnings_of,
)

INVALID_DIAGRAM = "isto não é um diagrama válido"

MIXED_DIAGRAMS = f"""# Diagramas

```mermaid
flowchart TD
    A[Válido] --> B[Também válido]
```

```mermaid
{INVALID_DIAGRAM} -->
```

Texto depois dos diagramas.
"""

NESTED_DIAGRAMS = """# Diagramas aninhados

- Item com diagrama:

  ```mermaid
  flowchart LR
      A[Lista] --> B[Item]
  ```

> Citação com diagrama:
>
> ```mermaid
> pie
>     "Sim" : 70
>     "Não" : 30
> ```

> [!NOTE]
> ```mermaid
> sequenceDiagram
>     Alice->>Bruno: Olá!
> ```

Texto com nota de rodapé[^diagrama].

[^diagrama]: Nota com diagrama:

    ```mermaid
    flowchart LR
        X[Nota] --> Y[Rodapé]
    ```
"""


def _mermaid_warnings(warnings: list[str]) -> list[str]:
    return [warning for warning in warnings if "mermaid" in warning.lower()]


def test_invalid_diagram_in_docx_keeps_the_source(client: TestClient) -> None:
    response = expect_ok(convert(client, "docx", MIXED_DIAGRAMS), DOCX_MEDIA_TYPE)
    assert len(_mermaid_warnings(warnings_of(response))) == 1
    text = docx_text(response.content)
    assert INVALID_DIAGRAM in text  # the original code block is kept
    assert "Erro no diagrama Mermaid" in text
    assert "Texto depois dos diagramas." in text
    assert docx_drawing_count(response.content) == 1  # the valid diagram is still rendered


def test_invalid_diagram_in_pdf_keeps_the_source(client: TestClient) -> None:
    response = expect_ok(convert(client, "pdf", MIXED_DIAGRAMS), PDF_MEDIA_TYPE)
    assert len(_mermaid_warnings(warnings_of(response))) == 1
    text = pdf_text(response.content)
    assert INVALID_DIAGRAM in text
    assert "Erro no diagrama Mermaid" in text
    assert "Texto depois dos diagramas." in text


def test_diagrams_nested_in_lists_quotes_alerts_and_footnotes(client: TestClient) -> None:
    response = expect_ok(convert(client, "docx", NESTED_DIAGRAMS), DOCX_MEDIA_TYPE)
    assert warnings_of(response) == []
    assert docx_drawing_count(response.content) == 3
    assert "<w:drawing" in docx_xml(response.content, "word/footnotes.xml")
    text = docx_text(response.content) + docx_text(response.content, "word/footnotes.xml")
    assert "flowchart" not in text and "sequenceDiagram" not in text


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_document_without_diagrams_is_unaffected(client: TestClient, fmt: str) -> None:
    markdown = "# Sem diagramas\n\n```python\nprint('mermaid')\n```\n"
    media_type = PDF_MEDIA_TYPE if fmt == "pdf" else DOCX_MEDIA_TYPE
    response = expect_ok(convert(client, fmt, markdown), media_type)
    assert warnings_of(response) == []
