"""Pandoc AST transforms (pure functions, no engine needed)."""

from __future__ import annotations

from typing import Any

import pytest

from app.converter import ast

NO_ATTR: list[Any] = ["", [], []]


def _cell(text: str, *, colspan: int = 1) -> list[Any]:
    return [
        NO_ATTR,
        {"t": "AlignDefault"},
        1,
        colspan,
        [{"t": "Plain", "c": ast.text_inlines(text)}],
    ]


def _table(header: list[str], *rows: list[str]) -> dict[str, Any]:
    def row(cells: list[str]) -> list[Any]:
        return [NO_ATTR, [_cell(text) for text in cells]]

    colspecs = [[{"t": "AlignDefault"}, {"t": "ColWidthDefault"}] for _ in header]
    return {
        "t": "Table",
        "c": [
            NO_ATTR,
            [None, []],
            colspecs,
            [NO_ATTR, [row(header)]],
            [[NO_ATTR, 0, [], [row(cells) for cells in rows]]],
            [NO_ATTR, []],
        ],
    }


def _widths(table: dict[str, Any]) -> list[Any]:
    return [spec[1] for spec in table["c"][2]]


def _alert(kind: str, title: str, text: str) -> dict[str, Any]:
    title_div = ast.div(["title"], [ast.para(ast.text_inlines(title))])
    return ast.div([kind], [title_div, ast.para(ast.text_inlines(text))])


LINE_PX = 640.0
SMALL = "Compact Small"


def _cell_styles(table: dict[str, Any]) -> set[str | None]:
    """custom-style of the Div wrapping each cell's contents (None when not wrapped)."""
    styles: set[str | None] = set()
    for row in [*table["c"][3][1], *table["c"][4][0][3]]:
        for cell in row[1]:
            blocks = cell[4]
            wrapped = len(blocks) == 1 and blocks[0]["t"] == "Div"
            styles.add(ast.attr_value(blocks[0]["c"][0], "custom-style") if wrapped else None)
    return styles


def test_tables_that_fit_get_their_natural_width() -> None:
    table = _table(["Nome", "Descrição"], ["Ana", "Coordena o time de vendas"])
    ast.fit_tables([table], line_px=LINE_PX, small_style=SMALL)
    widths = [width["c"] for width in _widths(table)]
    assert all(width["t"] == "ColWidth" for width in _widths(table))
    assert sum(widths) < 0.6  # narrower than the line, like a browser's auto layout
    assert widths[0] < widths[1]  # each column by its content
    assert _cell_styles(table) == {None}


def test_wide_tables_get_relative_widths_by_content() -> None:
    long_text = "uma observação bem longa que ocupa espaço " * 3
    table = _table(["ID", "Cliente", "Observações"], ["1", "Ana Beatriz", long_text])
    ast.fit_tables([table], line_px=LINE_PX, small_style=SMALL)
    widths = [width["c"] for width in _widths(table)]
    assert all(width["t"] == "ColWidth" for width in _widths(table))
    assert sum(widths) == pytest.approx(1.0)
    assert widths[0] < widths[1] < widths[2]


def test_columns_never_split_short_words() -> None:
    # The bold header "Column" needs about 47 px plus cell margins.
    table = _table(["Column", "Text"], ["1", "palavra " * 40])
    ast.fit_tables([table], line_px=LINE_PX, small_style=SMALL)
    first = _widths(table)[0]["c"] * LINE_PX
    assert first >= len("Column") * ast.TABLE_WORD_CHAR_PX + ast.TABLE_CELL_PADDING_PX


def test_many_columns_use_the_small_font() -> None:
    header = [
        "ID",
        "Name",
        "Department",
        "Location",
        "Manager",
        "Status",
        "Active",
        "Salary",
        "Start date",
        "Notes",
        "Extra",
    ]
    row = [
        "1",
        "Ana",
        "Engenharia",
        "São Paulo",
        "Bruno",
        "Ativo",
        "Sim",
        "12,500.00",
        "2024-01-01",
        "observação longa " * 6,
        "x",
    ]
    table = _table(header, row)
    ast.fit_tables([table], line_px=LINE_PX, small_style=SMALL)
    assert _cell_styles(table) == {SMALL}
    widths = [width["c"] * LINE_PX for width in _widths(table)]
    assert sum(widths) == pytest.approx(LINE_PX)
    # In the small font every header word fits whole.
    scale = ast.TABLE_SMALL_FONT_SCALE
    for name, width in zip(header, widths, strict=True):
        longest = max(len(word) for word in name.split())
        needed = ast.TABLE_SMALL_CELL_PADDING_PX + (longest + 1) * ast.TABLE_WORD_CHAR_PX * scale
        assert width >= needed - 0.01, (name, width, needed)
    assert widths[0] < widths[2]  # "ID" stays narrower than "Department"


def test_image_cells_get_room() -> None:
    table = _table(["Antes", "Depois"], ["x", "y"])
    for column, src in enumerate(("a.png", "b.png")):
        cell = table["c"][4][0][3][0][1][column]
        cell[4] = [{"t": "Plain", "c": [ast.image(src, ast.text_inlines("tela"))]}]
    ast.fit_tables([table], line_px=LINE_PX, small_style=SMALL)
    widths = [width["c"] for width in _widths(table)]
    assert widths == pytest.approx([0.5, 0.5], abs=0.02)


def test_tables_with_spanning_cells_are_left_alone() -> None:
    table = _table(["A", "B"], ["x" * 80, "y" * 80])
    table["c"][4][0][3][0][1][0][3] = 2  # first body cell spans two columns
    ast.fit_tables([table], line_px=300, small_style=SMALL)
    assert all(width == {"t": "ColWidthDefault"} for width in _widths(table))


def test_adjacent_bordered_blocks_are_separated() -> None:
    spacer = ast.raw_block("openxml", "<w:p/>")
    code = {"t": "CodeBlock", "c": [NO_ATTR, "x = 1"]}
    quote = {"t": "BlockQuote", "c": [ast.para(ast.text_inlines("citação"))]}
    blocks = [code, dict(code), ast.para(ast.text_inlines("texto")), dict(code)]
    blocks.append({"t": "BlockQuote", "c": [quote, dict(quote)]})
    result = ast.separate_adjacent_blocks(blocks, spacer)
    kinds = [block["t"] for block in result]
    assert kinds == ["CodeBlock", "RawBlock", "CodeBlock", "Para", "CodeBlock", "BlockQuote"]
    assert [block["t"] for block in result[-1]["c"]] == ["BlockQuote", "RawBlock", "BlockQuote"]


def test_alert_titles_are_localized() -> None:
    blocks = [_alert("warning", "Warning", "Cuidado."), _alert("note", "Note", "Texto.")]
    result = ast.localize_alert_titles(blocks, ast.ALERT_TITLES_PT)
    titles = [ast.stringify(block["c"][1][0]["c"][1]) for block in result]
    assert titles == ["Atenção", "Nota"]
    assert ast.stringify(result[0]["c"][1][1]) == "Cuidado."


def test_alerts_become_block_quotes_with_bold_title() -> None:
    (quote,) = ast.alerts_to_blockquotes([_alert("tip", "Dica", "Use atalhos.")])
    assert quote["t"] == "BlockQuote"
    title, body = quote["c"]
    assert title["c"][0]["t"] == "Strong"
    assert ast.stringify(title) == "Dica"
    assert ast.stringify(body) == "Use atalhos."
    assert ast.alert_kind(quote) == "tip"
    assert ast.alert_kind({"t": "BlockQuote", "c": [body]}) is None


def test_quote_followed_by_table_is_separated() -> None:
    spacer = ast.raw_block("openxml", "<w:p/>")
    quote = {"t": "BlockQuote", "c": [ast.para(ast.text_inlines("citação"))]}
    result = ast.separate_adjacent_blocks([quote, _table(["A"], ["1"])], spacer)
    assert [block["t"] for block in result] == ["BlockQuote", "RawBlock", "Table"]


def _image_widths(blocks: list[Any]) -> dict[str, float]:
    found: dict[str, float] = {}

    def fn(node: dict[str, Any], room: ast.Room) -> None:
        if node["t"] == "Image":
            found[node["c"][2][0]] = room.width

    ast.walk_with_room(blocks, ast.DocxLayout(root_px=LINE_PX), fn)
    return found


def test_room_shrinks_in_lists_quotes_and_cells() -> None:
    def image_para(src: str) -> dict[str, Any]:
        return ast.para([ast.image(src, [])])

    table = _table(["A", "B"], ["x", "y"])
    table["c"][2][0][1] = {"t": "ColWidth", "c": 0.25}
    table["c"][2][1][1] = {"t": "ColWidth", "c": 0.75}
    table["c"][4][0][3][0][1][0][4] = [image_para("cell.png")]
    nested_list = {"t": "BulletList", "c": [[image_para("level2.png")]]}
    blocks = [
        image_para("top.png"),
        {"t": "BulletList", "c": [[image_para("level1.png"), nested_list]]},
        {"t": "BlockQuote", "c": [image_para("quote.png")]},
        table,
    ]
    widths = _image_widths(blocks)
    layout = ast.DocxLayout(root_px=LINE_PX)
    assert widths["top.png"] == LINE_PX
    assert widths["level1.png"] == LINE_PX - layout.list_indent_px
    assert widths["level2.png"] == LINE_PX - 2 * layout.list_indent_px
    assert widths["quote.png"] == LINE_PX - layout.quote_inset_px
    assert widths["cell.png"] == pytest.approx(0.25 * LINE_PX - layout.cell_padding_px)


def test_quotes_become_boxes_that_end_with_a_paragraph() -> None:
    spacer = ast.raw_block("openxml", "<w:p/>")
    inner = {"t": "BlockQuote", "c": [ast.para(ast.text_inlines("nível 2"))]}
    outer = {"t": "BlockQuote", "c": [ast.para(ast.text_inlines("nível 1")), inner]}
    (alert,) = ast.alerts_to_blockquotes([_alert("warning", "Atenção", "Cuidado.")])
    result = ast.box_quotes([outer, ast.para([]), alert], ast.DocxLayout(LINE_PX), spacer)
    xml = "".join(block["c"][1] for block in ast.find_all(result, lambda n: n["t"] == "RawBlock"))
    assert xml.count("<w:tbl>") == xml.count("</w:tbl>") == 3
    assert 'w:color="9A6700"' in xml  # the alert keeps its color
    assert not any(block["t"] == "BlockQuote" for block in ast.find_all(result, lambda n: True))
    # The outer box's cell ends with a paragraph after the nested box closes.
    outer_div = result[1]
    assert outer_div["t"] == "Div"
    assert outer_div["c"][1][-1] == spacer


def test_meta_text() -> None:
    doc = {
        "meta": {
            "lang": {"t": "MetaInlines", "c": [{"t": "Str", "c": "pt-BR"}]},
            "pagetitle": {"t": "MetaString", "c": " Relatório "},
            "toc": {"t": "MetaBool", "c": True},
        }
    }
    assert ast.meta_text(doc, "lang") == "pt-BR"
    assert ast.meta_text(doc, "pagetitle") == "Relatório"
    assert ast.meta_text(doc, "toc") is None
    assert ast.meta_text(doc, "missing") is None


def test_ordered_task_lists() -> None:
    def ordered(*texts: str) -> dict[str, Any]:
        items = [[{"t": "Plain", "c": _inlines(text)}] for text in texts]
        return {"t": "OrderedList", "c": [[1, {"t": "Decimal"}, {"t": "Period"}], items]}

    def _inlines(text: str) -> list[Any]:
        if text.startswith("[ ] "):
            rest = text[4:]
            marker = [{"t": "Str", "c": "["}, {"t": "Space"}, {"t": "Str", "c": "]"}]
            return [*marker, {"t": "Space"}, *ast.text_inlines(rest)]
        return ast.text_inlines(text)

    docx = ast.ordered_task_lists([ordered("[ ] passo", "[x] feito", "normal")], html=False)
    texts = [ast.stringify(item) for item in docx[0]["c"][1]]
    assert texts == ["☐ passo", "☒ feito", "normal"]
    pdf = ast.ordered_task_lists([ordered("[x] feito")], html=True)
    first = pdf[0]["c"][1][0][0]["c"][0]
    assert first["t"] == "RawInline" and 'checked=""' in first["c"][1]


def test_heading_code_spans() -> None:
    header = {
        "t": "Header",
        "c": [2, ["h", [], []], [{"t": "Code", "c": [NO_ATTR, "convert()"]}]],
    }
    (result,) = ast.heading_code_spans([header], "Verbatim Heading Char")
    (span,) = result["c"][2]
    assert span["t"] == "Span"
    assert ast.attr_value(span["c"][0], "custom-style") == "Verbatim Heading Char"
    assert ast.stringify(span) == "convert()"


def test_aligned_headers_get_identifiers() -> None:
    headers = [
        {"t": "Header", "c": [1, ["", [], [["align", "center"]]], ast.text_inlines("Logo")]},
        {"t": "Header", "c": [2, ["uso", [], [["align", "right"]]], ast.text_inlines("Uso")]},
        {"t": "Header", "c": [2, ["outro", [], []], ast.text_inlines("Outro")]},
    ]
    aligned = ast.aligned_headers(headers)
    assert aligned == {"aligned-heading-1": "center", "uso": "right"}
    assert headers[0]["c"][1][0] == "aligned-heading-1"


def test_mermaid_positions_follow_the_document() -> None:
    def diagram(code: str) -> dict[str, Any]:
        return {"t": "CodeBlock", "c": [["", ["mermaid"], []], code]}

    blocks = [diagram("A"), diagram("B"), diagram("A"), diagram("bad")]
    assert ast.mermaid_positions(blocks) == {"A": [1, 3], "B": [2], "bad": [4]}


def test_tables_too_wide_even_for_the_small_font_narrow_the_widest_columns() -> None:
    header = [f"Coluna{n}" for n in range(6)] + ["ID", "OK"]
    row = ["palavrasemfimnenhum" * 2] * 6 + ["1", "x"]
    table = _table(header, row)
    ast.fit_tables([table], line_px=300, small_style=SMALL)
    widths = [width["c"] * 300 for width in _widths(table)]
    assert sum(widths) == pytest.approx(300)
    # The short columns keep their (small) minimum; the long ones share the rest equally.
    scale = ast.TABLE_SMALL_FONT_SCALE
    assert widths[6] >= ast.TABLE_SMALL_CELL_PADDING_PX + 3 * ast.TABLE_WORD_CHAR_PX * scale
    assert widths[0] == pytest.approx(widths[5])
