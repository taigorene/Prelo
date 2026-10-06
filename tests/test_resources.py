"""Image resources: deduplication, per-document limits, decoded-size limits and DOCX sizing."""

from __future__ import annotations

import base64
import re
import struct

import pytest
from fastapi.testclient import TestClient

from app.converter.resources import (
    FetchError,
    ResourceBudget,
    ResourceLimitError,
    image_dimensions,
)
from tests.conftest import ClientFactory, ImageServer
from tests.utils import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    build_zip,
    convert,
    docx_media,
    docx_xml,
    expect_ok,
    make_png,
    make_png_bomb,
    pdf_image_count,
    warnings_of,
)

EMU_PER_PX = 9525
PNG = make_png(1200, 700, (37, 99, 235))

# --------------------------------------------------------------------------- unit: headers


def _webp_vp8x(width: int, height: int) -> bytes:
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4 + canvas
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def _bmp(width: int, height: int) -> bytes:
    info = struct.pack("<IiiHH", 40, width, -height, 1, 24) + b"\x00" * 24
    return b"BM" + struct.pack("<IHHI", 54, 0, 0, 54) + info


def _avif(width: int, height: int) -> bytes:
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    ispe = box(b"ispe", b"\x00" * 4 + struct.pack(">II", width, height))
    meta = box(b"meta", b"\x00" * 4 + box(b"iprp", box(b"ipco", ispe)))
    return box(b"ftyp", b"avif" + b"\x00" * 4 + b"mif1avif") + meta


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (make_png(7, 3), (7, 3)),
        (make_png_bomb(20_000, 20_000), (20_000, 20_000)),
        (_webp_vp8x(16_000, 9_000), (16_000, 9_000)),
        (_bmp(300, 200), (300, 200)),
        (_avif(4_000, 3_000), (4_000, 3_000)),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None),
    ],
    ids=["png", "png-bomb", "webp", "bmp", "avif", "svg"],
)
def test_image_dimensions_of_every_raster_format(data: bytes, size: tuple[int, int]) -> None:
    assert image_dimensions(data) == size


def test_budget_limits() -> None:
    budget = ResourceBudget(max_bytes=100, max_mb=0.0001, max_references=2)
    budget.reserve(60, "a.png")
    with pytest.raises(ResourceLimitError, match="Limite de"):
        budget.reserve(60, "b.png")
    budget.reserve(40, "c.png")
    assert budget.check_pixels("image/png", make_png(10, 10), "d.png") == (10, 10)
    with pytest.raises(ResourceLimitError, match="grande demais"):
        budget.check_pixels("image/png", make_png_bomb(), "bomb.png")
    with pytest.raises(FetchError, match="inválida"):
        budget.check_pixels("image/png", b"\x89PNG\r\n\x1a\nquebrado", "bad.png")
    assert budget.check_pixels("image/svg+xml", b"<svg/>", "x.svg") is None
    assert [budget.take_reference() for _ in range(3)] == [True, True, False]


# --------------------------------------------------------------------------- one copy per image


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_an_image_used_many_times_is_embedded_once(client: TestClient, fmt: str) -> None:
    spellings = ["big.png", "./big.png", "big.png?v=2", "big.png#x", "img/../big.png"]
    references = "\n\n".join(f"![i{n}]({spellings[n % len(spellings)]})" for n in range(300))
    upload = build_zip([("README.md", f"# Muitas\n\n{references}\n"), ("big.png", PNG)])
    response = convert(client, fmt, upload, "muitas.zip")
    if fmt == "pdf":
        assert pdf_image_count(expect_ok(response, PDF_MEDIA_TYPE).content) >= 1
    else:
        content = expect_ok(response, DOCX_MEDIA_TYPE).content
        assert len(docx_media(content)) == 1
        assert docx_xml(content).count("<pic:pic") == 300
    assert warnings_of(response) == []


def test_identical_mermaid_diagrams_are_rendered_once(client: TestClient) -> None:
    diagram = "```mermaid\nflowchart LR\n  A --> B\n```\n\n"
    response = expect_ok(convert(client, "docx", "# D\n\n" + diagram * 40), DOCX_MEDIA_TYPE)
    assert len(docx_media(response.content)) == 1


# --------------------------------------------------------------------------- per-document limits


def test_total_image_budget(client_factory: ClientFactory) -> None:
    client = client_factory(max_images_total_mb=0.3)
    images = [(f"img/{n}.png", make_png(200, 200 + n) + b"\0" * 120_000) for n in range(4)]
    references = "\n\n".join(f"![i]({name})" for name, _ in images)
    upload = build_zip([("README.md", f"# Orçamento\n\n{references}\n"), *images])
    response = expect_ok(convert(client, "docx", upload, "orcamento.zip"), DOCX_MEDIA_TYPE)
    assert len(docx_media(response.content)) == 2
    assert any("Limite de 0,3 MB de imagens" in warning for warning in warnings_of(response))


def test_raw_html_resources_count_against_the_budget(client_factory: ClientFactory) -> None:
    client = client_factory(max_images_total_mb=0.3)
    files = [(f"big{n}.png", make_png(10, 10 + n) + b"\0" * 120_000) for n in range(4)]
    # Query strings do not make a file count (or get sent to the browser) twice.
    tags = "\n".join(f'<img src="{name}?{n}" alt="">' for name, _ in files for n in range(50))
    upload = build_zip([("README.md", f"# HTML\n\n<p>\n{tags}\n</p>\n"), *files])
    response = expect_ok(convert(client, "pdf", upload, "html.zip"), PDF_MEDIA_TYPE)
    warnings = warnings_of(response)
    assert any("Limite de 0,3 MB de imagens" in warning for warning in warnings), warnings
    assert sum("Limite de" in warning for warning in warnings) == 2  # big2.png, big3.png


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_decompression_bombs_are_refused(client: TestClient, fmt: str) -> None:
    bomb = base64.b64encode(make_png_bomb()).decode()
    markdown = f"# Bomba\n\n![bomba](data:image/png;base64,{bomb})\n\nTexto.\n"
    response = client.post(f"/api/v1/convert/{fmt}/text", json={"markdown": markdown})
    assert response.status_code == 200, response.text
    assert any("20000 × 20000" in warning for warning in warnings_of(response))


def test_raw_html_decompression_bomb_is_refused(client: TestClient) -> None:
    upload = build_zip(
        [
            ("README.md", '# Bomba\n\n<img src="bomba.png" alt="b">\n'),
            ("bomba.png", make_png_bomb()),
        ]
    )
    response = expect_ok(convert(client, "pdf", upload, "bomba.zip"), PDF_MEDIA_TYPE)
    assert any("grande demais" in warning for warning in warnings_of(response))


def test_remote_downloads_share_the_budget(
    client_factory: ClientFactory, image_server: ImageServer
) -> None:
    client = client_factory(
        allow_private_networks=True, remote_image_max_mb=1, max_images_total_mb=0.3
    )
    urls = [image_server.url(f"/big.png?{n}") for n in range(3)]
    urls += [image_server.url("/big.png?0#a"), image_server.url("/big.png?0#b")]
    markdown = "# Remotas\n\n" + "\n\n".join(f"![r]({url})" for url in urls)
    response = expect_ok(convert(client, "docx", markdown), DOCX_MEDIA_TYPE)
    assert len(docx_media(response.content)) == 1
    assert any("Limite de 0,3 MB" in warning for warning in warnings_of(response))
    # A fragment never causes another download.
    assert image_server.hits.count("/big.png?0") == 1


# --------------------------------------------------------------------------- odd local paths


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
@pytest.mark.parametrize(
    "reference",
    [
        "![alt](" + "a" * 300 + ".png)",
        "![alt](" + ("d" * 200 + "/") * 30 + "a.png)",
        '<img src="' + "ção" * 90 + '.png" alt="x">',
    ],
    ids=["long-name", "deep-path", "long-raw-html"],
)
def test_overlong_image_paths_are_reported_as_missing(
    client: TestClient, fmt: str, reference: str
) -> None:
    response = convert(client, fmt, f"# Caminho\n\n{reference}\n\nTexto.\n")
    assert response.status_code == 200, response.text
    assert any("Imagem não encontrada" in warning for warning in warnings_of(response))


# --------------------------------------------------------------------------- DOCX sizing


def _extents_px(xml: str) -> list[float]:
    return [int(cx) / EMU_PER_PX for cx in re.findall(r'<wp:extent cx="(\d+)"', xml)]


def test_docx_images_fit_list_levels_quotes_and_table_cells(client: TestClient) -> None:
    markdown = """# Telas

![topo](tela.png)

1. Passo um:

   ![nivel1](tela.png)

   - Detalhe:

     ![nivel2](tela.png)

> ![citacao](tela.png)

| Antes | Depois |
|---|---|
| ![a](tela.png) | ![b](tela.png) |
"""
    upload = build_zip([("README.md", markdown), ("tela.png", PNG)])
    content = expect_ok(convert(client, "docx", upload, "telas.zip"), DOCX_MEDIA_TYPE).content
    xml = docx_xml(content)
    top, level1, level2, quote, cell_a, cell_b = _extents_px(xml)
    assert top == pytest.approx(642, abs=2)  # A4, normal margins: the full text width
    assert level1 <= top - 47 and level2 <= top - 95
    assert quote <= top - 25
    # The table spans tblW (pct of the text width); its columns split it like the grid.
    grid = [int(width) for width in re.findall(r'<w:gridCol w:w="(\d+)"', xml)[-2:]]
    table_pct = int(re.findall(r'<w:tblW w:type="pct" w:w="(\d+)"', xml)[-1]) / 5000
    for extent, column in zip((cell_a, cell_b), grid, strict=True):
        column_px = column / sum(grid) * table_pct * top
        assert extent <= column_px - 12, (extent, column_px)
    # Pictures sit flush with the text margin (LibreOffice applies 0.125 in otherwise).
    assert "<wp:inline>" not in xml
    assert '<wp:inline distT="0" distB="0" distL="0" distR="0">' in xml


def test_docx_mermaid_in_list_fits_the_item(client: TestClient) -> None:
    chain = " --> ".join(f"N{n}[Etapa número {n}]" for n in range(8))
    diagram = f"```mermaid\nflowchart LR\n  {chain}\n```"
    indented = "\n".join("      " + line for line in diagram.splitlines())
    markdown = f"# D\n\n{diagram}\n\n- a\n  - b\n    - c\n\n{indented}\n"
    content = expect_ok(convert(client, "docx", markdown), DOCX_MEDIA_TYPE).content
    top, nested = _extents_px(docx_xml(content))
    assert nested <= top - 3 * 47, (top, nested)
