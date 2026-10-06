"""Helpers for pandoc's JSON AST: a generic traversal, node constructors and the
document transforms shared by the PDF and DOCX pipelines.

The AST is handled as plain JSON (``dict``/``list``) exactly as produced by
``pandoc -t json``. Every element is ``{"t": <type>, "c": <contents>}`` (``c`` is
absent for elements without contents such as ``Space``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

Node = dict[str, Any]
Attr = list[Any]  # [identifier, [classes], [[key, value], ...]]
Transform = Callable[[Node], "Node | list[Node] | None"]

ALERT_CLASSES: frozenset[str] = frozenset({"note", "tip", "important", "warning", "caution"})
# Titles of GitHub alerts for Portuguese documents (pandoc always generates English ones).
ALERT_TITLES_PT: dict[str, str] = {
    "note": "Nota",
    "tip": "Dica",
    "important": "Importante",
    "warning": "Atenção",
    "caution": "Cuidado",
}

# DOCX table width estimates, in CSS px of Word's body text (Calibri 11 pt): running text,
# characters of a word that must not be split (bold header words are the widest), cell
# margins (2 x 108 twips) plus slack, and the longest word kept whole (longer ones wrap
# anywhere in Word).
TABLE_PROSE_CHAR_PX = 7.0
TABLE_WORD_CHAR_PX = 8.0
TABLE_CELL_PADDING_PX = 16.0
TABLE_MAX_UNBREAKABLE_CHARS = 16
# Tables with this many columns use the small table font (8 pt, like the PDF's 0.78 em),
# with narrower cell margins (2 x 57 twips, see docx.SMALL_TABLE_CELL_MARGIN).
TABLE_SMALL_FONT_COLUMNS = 8
TABLE_SMALL_FONT_SCALE = 8 / 11
TABLE_SMALL_CELL_PADDING_PX = 10.0
# Blocks drawn with borders in the DOCX output, by group: adjacent blocks of the same
# group merge in Word (block quotes are written as one-cell tables).
BORDERED_BLOCKS: Mapping[str, str] = {"CodeBlock": "code", "BlockQuote": "table", "Table": "table"}

_DETAILS_OPEN_TAG = re.compile(r"<details\b(?![^>]*\bopen\b)", re.IGNORECASE)
_LAZY_LOADING_ATTR = re.compile(r"""\s+loading\s*=\s*(["']?)lazy\1""", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------
def walk(value: Any, fn: Transform) -> Any:
    """Apply ``fn`` to every element below ``value`` (post-order, children first).

    ``fn`` returns ``None`` to keep the element, a node to replace it, or a list of
    nodes to splice in its place (an empty list removes it). Nested structures are
    reached generically, so lists, block quotes, divs, table cells, footnotes and
    figures are all covered. Lists are rebuilt; nodes are updated in place.
    """
    if isinstance(value, list):
        result: list[Any] = []
        for item in value:
            new = walk(item, fn)
            if isinstance(item, dict) and "t" in item and isinstance(new, list):
                result.extend(new)
            else:
                result.append(new)
        return result
    if isinstance(value, dict):
        if "c" in value:
            value["c"] = walk(value["c"], fn)
        if "t" in value:
            replacement = fn(value)
            return value if replacement is None else replacement
    return value


def find_all(value: Any, predicate: Callable[[Node], bool]) -> list[Node]:
    """Return every element below ``value`` matching ``predicate`` in document order."""
    found: list[Node] = []

    def visit(node: Node) -> None:
        if predicate(node):
            found.append(node)

    walk(value, visit)
    return found


# ---------------------------------------------------------------------------
# Constructors and accessors
# ---------------------------------------------------------------------------
def make_attr(identifier: str = "", classes: Iterable[str] = (), **attributes: str) -> Attr:
    return [identifier, list(classes), [[k, v] for k, v in attributes.items()]]


def text_inlines(text: str) -> list[Node]:
    """Split plain text into ``Str``/``Space`` inlines."""
    inlines: list[Node] = []
    for word in text.split():
        if inlines:
            inlines.append({"t": "Space"})
        inlines.append({"t": "Str", "c": word})
    return inlines


def para(inlines: list[Node]) -> Node:
    return {"t": "Para", "c": inlines}


def div(classes: Iterable[str], blocks: list[Node]) -> Node:
    return {"t": "Div", "c": [make_attr("", classes), blocks]}


def raw_block(fmt: str, text: str) -> Node:
    return {"t": "RawBlock", "c": [fmt, text]}


def emph(inlines: list[Node]) -> Node:
    return {"t": "Emph", "c": inlines}


def span(classes: Iterable[str], inlines: list[Node], **attributes: str) -> Node:
    return {"t": "Span", "c": [make_attr("", classes, **attributes), inlines]}


def strong(inlines: list[Node]) -> Node:
    return {"t": "Strong", "c": inlines}


def link(inlines: list[Node], url: str, title: str = "") -> Node:
    return {"t": "Link", "c": [make_attr(), inlines, [url, title]]}


def image(src: str, alt: list[Node], *, title: str = "", attr: Attr | None = None) -> Node:
    return {"t": "Image", "c": [attr or make_attr(), alt, [src, title]]}


def display_math(tex: str) -> Node:
    return {"t": "Math", "c": [{"t": "DisplayMath"}, tex]}


def node_classes(node: Node) -> list[str]:
    """Classes of an element whose first content item is an ``Attr``."""
    return list(node["c"][0][1])


def attr_value(attr: Attr, key: str) -> str | None:
    for k, v in attr[2]:
        if k == key:
            return v
    return None


def stringify(value: Any) -> str:
    """Plain-text rendering of inlines/blocks (similar to ``pandoc.utils.stringify``)."""
    parts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        kind = item.get("t")
        contents = item.get("c")
        if kind in ("Str", "Code", "Math"):
            parts.append(contents if kind == "Str" else contents[1])
        elif kind in ("Space", "SoftBreak", "LineBreak"):
            parts.append(" ")
        elif kind in ("Link", "Image", "Span", "Quoted"):
            visit(contents[1])
        elif kind in ("Note", "RawInline", "RawBlock", "CodeBlock"):
            return
        else:
            visit(contents)

    visit(value)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


# ---------------------------------------------------------------------------
# Document metadata
# ---------------------------------------------------------------------------
def ensure_meta_string(doc: dict[str, Any], key: str, value: str) -> None:
    """Set ``meta[key]`` to a string unless the document already defines it."""
    meta = doc.setdefault("meta", {})
    if key not in meta:
        meta[key] = {"t": "MetaString", "c": value}


def has_meta(doc: dict[str, Any], key: str) -> bool:
    return key in doc.get("meta", {})


def meta_text(doc: dict[str, Any], key: str) -> str | None:
    """Plain-text value of a scalar metadata field (``None`` when absent or empty)."""
    value = doc.get("meta", {}).get(key)
    if not isinstance(value, dict):
        return None
    contents = value.get("c")
    if value.get("t") == "MetaString":
        text = contents if isinstance(contents, str) else ""
    else:
        text = stringify(contents)
    return text.strip() or None


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------
def is_code_block_with_class(node: Node, cls: str) -> bool:
    return node["t"] == "CodeBlock" and cls in node_classes(node)


def convert_math_blocks(blocks: list[Node]) -> list[Node]:
    """GitHub-style fenced ``math`` code blocks → display math paragraphs.

    pandoc 3.12's gfm reader already does this for plain fences; this also covers
    fences carrying extra attributes.
    """

    def fn(node: Node) -> Node | None:
        if is_code_block_with_class(node, "math"):
            return para([display_math(node["c"][1])])
        return None

    return walk(blocks, fn)


def mermaid_sources(blocks: list[Node]) -> list[str]:
    """Unique Mermaid diagram sources in document order."""
    return list(mermaid_positions(blocks))


def mermaid_positions(blocks: list[Node]) -> dict[str, list[int]]:
    """Position (1-based, in document order) of every Mermaid diagram, by source."""
    positions: dict[str, list[int]] = {}
    diagrams = find_all(blocks, lambda n: is_code_block_with_class(n, "mermaid"))
    for number, node in enumerate(diagrams, start=1):
        positions.setdefault(node["c"][1], []).append(number)
    return positions


def replace_mermaid(
    blocks: list[Node], replacement: Callable[[Node], list[Node] | None]
) -> list[Node]:
    """Replace each Mermaid code block by ``replacement(code_block)`` (``None`` keeps it)."""

    def fn(node: Node) -> list[Node] | None:
        if is_code_block_with_class(node, "mermaid"):
            return replacement(node)
        return None

    return walk(blocks, fn)


def image_sources(blocks: list[Node]) -> list[str]:
    """Unique ``src`` values of all ``Image`` elements in document order."""
    sources = [node["c"][2][0] for node in find_all(blocks, lambda n: n["t"] == "Image")]
    return list(dict.fromkeys(sources))


def replace_images(blocks: list[Node], fn: Callable[[Node], Node | None]) -> list[Node]:
    """Rewrite every ``Image`` element with ``fn`` (return ``None`` to keep it)."""
    return walk(blocks, lambda node: fn(node) if node["t"] == "Image" else None)


def prepare_raw_html_for_print(blocks: list[Node]) -> list[Node]:
    """Raw HTML tweaks for a static rendering: open ``<details>``, no lazy loading."""

    def fn(node: Node) -> None:
        if node["t"] in ("RawBlock", "RawInline") and node["c"][0] in ("html", "html5"):
            text = _DETAILS_OPEN_TAG.sub("<details open", node["c"][1])
            node["c"][1] = _LAZY_LOADING_ATTR.sub("", text)

    return walk(blocks, fn)


def localize_alert_titles(blocks: list[Node], titles: Mapping[str, str]) -> list[Node]:
    """Replace the title pandoc generates for each GitHub alert (``Div.title``) by
    ``titles[<alert kind>]``."""

    def fn(node: Node) -> None:
        if node["t"] != "Div":
            return
        kinds = ALERT_CLASSES.intersection(node_classes(node))
        title = titles.get(next(iter(kinds))) if len(kinds) == 1 else None
        if title is None:
            return
        for block in node["c"][1]:
            if block["t"] == "Div" and "title" in node_classes(block):
                block["c"][1] = [para(text_inlines(title))]
                return

    return walk(blocks, fn)


def alerts_to_blockquotes(blocks: list[Node]) -> list[Node]:
    """GitHub alerts (``Div.note`` etc. with a ``Div.title``) → block quote with a bold title.

    The title is wrapped in a ``Span.alert-<kind>`` so later passes know the alert kind
    (see :func:`alert_kind`).
    """

    def fn(node: Node) -> Node | None:
        if node["t"] != "Div":
            return None
        kinds = sorted(ALERT_CLASSES.intersection(node_classes(node)))
        if not kinds:
            return None
        content: list[Node] = node["c"][1]
        title: list[Node] = []
        body: list[Node] = []
        for block in content:
            if not title and block["t"] == "Div" and "title" in node_classes(block):
                title = text_inlines(stringify(block["c"][1]))
            else:
                body.append(block)
        if not title:
            return None
        marked = span([f"alert-{kinds[0]}"], title)
        return {"t": "BlockQuote", "c": [para([strong([marked])]), *body]}

    return walk(blocks, fn)


def alert_kind(quote: Node) -> str | None:
    """Kind (``note``, ``tip``...) of a block quote made by :func:`alerts_to_blockquotes`."""
    blocks = quote["c"]
    if not blocks or blocks[0]["t"] != "Para":
        return None
    inlines = blocks[0]["c"]
    if len(inlines) != 1 or inlines[0]["t"] != "Strong" or len(inlines[0]["c"]) != 1:
        return None
    inner = inlines[0]["c"][0]
    if inner["t"] != "Span":
        return None
    for cls in node_classes(inner):
        if cls.startswith("alert-") and cls[len("alert-") :] in ALERT_CLASSES:
            return cls[len("alert-") :]
    return None


def normalize_table_widths(blocks: list[Node]) -> list[Node]:
    """Stretch relative column widths that add up to less than the full line.

    pandoc's gfm reader derives relative widths from the separator dashes when a row
    is longer than 72 characters, so wide tables would only span part of the page.
    """

    def fn(node: Node) -> None:
        if node["t"] != "Table":
            return
        colspecs: list[list[Any]] = node["c"][2]
        widths = [spec[1] for spec in colspecs]
        if not widths or any(width.get("t") != "ColWidth" for width in widths):
            return
        total = sum(width["c"] for width in widths)
        if 0 < total < 1:
            for width in widths:
                width["c"] = width["c"] / total

    return walk(blocks, fn)


def _table_rows(table: Node) -> list[list[Any]]:
    """Every row of a ``Table`` (head, bodies' intermediate heads and rows, foot)."""
    _, _, _, head, bodies, foot = table["c"]
    rows: list[list[Any]] = [*head[1], *foot[1]]
    for body in bodies:
        rows += [*body[2], *body[3]]
    return rows


def _cell_estimate(contents: list[Node], line_px: float, *, header: bool) -> tuple[float, float]:
    """(natural, minimum) width in px of one cell at the body font size, padding included.

    Images and diagrams can shrink to any column, but want the whole line.
    """
    text = stringify(contents)
    words = [len(word) for word in text.split()]
    lines = [
        len(line)
        for node in find_all(contents, lambda n: n["t"] == "CodeBlock")
        for line in node["c"][1].splitlines()
    ]
    bold = 1 if header else 0  # header cells are bold (wider)
    natural = (len(text) + bold) * TABLE_PROSE_CHAR_PX
    natural = max(natural, max(lines, default=0) * TABLE_PROSE_CHAR_PX)
    longest = min(max(words, default=0), TABLE_MAX_UNBREAKABLE_CHARS)
    minimum = (longest + bold) * TABLE_WORD_CHAR_PX if longest else 0.0
    if find_all(contents, lambda n: n["t"] == "Image"):
        natural = line_px
    natural, minimum = natural + TABLE_CELL_PADDING_PX, minimum + TABLE_CELL_PADDING_PX
    return max(natural, minimum), minimum


def _column_estimates(
    table: Node, columns: int, line_px: float
) -> tuple[list[float], list[float]] | None:
    """Per-column (natural, minimum) widths in px at the body font size.

    ``None`` when a cell spans several rows or columns (no reliable per-column estimate).
    """
    natural = [TABLE_CELL_PADDING_PX] * columns
    minimum = [TABLE_CELL_PADDING_PX] * columns
    _, _, _, head, _, _ = table["c"]
    header_rows = len(head[1])
    for row_index, (_, cells) in enumerate(_table_rows(table)):
        if len(cells) > columns:
            return None
        for index, (_, _, rowspan, colspan, contents) in enumerate(cells):
            if rowspan != 1 or colspan != 1:
                return None
            cell_natural, cell_minimum = _cell_estimate(
                contents, line_px, header=row_index < header_rows
            )
            natural[index] = max(natural[index], cell_natural)
            minimum[index] = max(minimum[index], cell_minimum)
    return natural, minimum


def _distribute(natural: list[float], minimum: list[float], line_px: float) -> list[float]:
    """Column widths (px) filling ``line_px`` for a table whose natural width overflows."""
    if sum(minimum) <= line_px:
        # Every column gets its minimum, the rest is shared by how much more each wants.
        growth = (line_px - sum(minimum)) / (sum(natural) - sum(minimum))
        return [low + (high - low) * growth for high, low in zip(natural, minimum, strict=True)]
    # Even the minimums overflow: narrow columns (IDs, numbers, status) keep theirs and
    # the widest ones share what is left equally, so only the longest words get split.
    remaining = line_px
    ordered = sorted(minimum)
    for index, width in enumerate(ordered):
        columns_left = len(ordered) - index
        if width * columns_left > remaining:
            cap = remaining / columns_left
            return [min(width, cap) for width in minimum]
        remaining -= width
    return list(minimum)  # pragma: no cover - unreachable when the minimums overflow


def _use_small_font(table: Node, style: str) -> None:
    """Wrap every cell's contents in a ``custom-style`` Div (the small table font).

    pandoc gives plain cell text (``Plain``) its own "Compact" style, so it becomes a
    paragraph to take the custom style.
    """
    for _, cells in _table_rows(table):
        for cell in cells:
            blocks = [
                {"t": "Para", "c": block["c"]} if block["t"] == "Plain" else block
                for block in cell[4]
            ]
            cell[4] = [{"t": "Div", "c": [make_attr(**{"custom-style": style}), blocks]}]


def _smaller(width: float, line_px: float) -> float:
    """A width estimated for the body font, for the small table font and cell margins."""
    if width >= line_px:  # image cells still want the whole line
        return line_px
    text = width - TABLE_CELL_PADDING_PX
    return TABLE_SMALL_CELL_PADDING_PX + text * TABLE_SMALL_FONT_SCALE


def fit_tables(blocks: list[Node], *, line_px: float, small_style: str) -> list[Node]:
    """Give tables without explicit widths relative column widths sized by their content.

    Meant for DOCX: pandoc writes such tables with equal columns spanning 5.5 inches,
    which Word autofits but LibreOffice and Google Docs render cramped (short columns as
    wide as long ones, needless wrapping). Tables that fit get their natural width (like
    a browser's auto layout); wider ones span the line, never squeezing a column below
    its longest word unless even the minimums overflow. Tables with many columns, or too
    wide for the body font, use the small table font (``small_style``), as in the PDF;
    when even their longest words cannot all fit, only the widest columns are narrowed.
    Tables that already have widths, or cells spanning rows or columns, are left alone.
    """

    def fn(node: Node) -> None:
        if node["t"] != "Table":
            return
        colspecs: list[list[Any]] = node["c"][2]
        if not colspecs or any(spec[1].get("t") != "ColWidthDefault" for spec in colspecs):
            return
        estimates = _column_estimates(node, len(colspecs), line_px)
        if estimates is None:
            return
        natural, minimum = estimates
        if len(colspecs) >= TABLE_SMALL_FONT_COLUMNS or sum(minimum) > line_px:
            _use_small_font(node, small_style)
            natural = [_smaller(width, line_px) for width in natural]
            minimum = [_smaller(width, line_px) for width in minimum]
        widths = natural if sum(natural) <= line_px else _distribute(natural, minimum, line_px)
        for spec, width in zip(colspecs, widths, strict=True):
            spec[1] = {"t": "ColWidth", "c": width / line_px}

    return walk(blocks, fn)


def unwrap_nested_links(blocks: list[Node]) -> list[Node]:
    """Replace links nested inside other links by their text (invalid in HTML and DOCX).

    Happens when an image inside a link (e.g. a README badge) falls back to a link.
    """

    def strip_links(inlines: list[Node]) -> list[Node]:
        return walk(inlines, lambda node: node["c"][1] if node["t"] == "Link" else None)

    def fn(node: Node) -> None:
        if node["t"] == "Link":
            node["c"][1] = strip_links(node["c"][1])

    return walk(blocks, fn)


def separate_adjacent_blocks(
    blocks: list[Node], spacer: Node, kinds: Mapping[str, str] | Iterable[str] = BORDERED_BLOCKS
) -> list[Node]:
    """Insert ``spacer`` between consecutive blocks of the same group listed in ``kinds``.

    Word and LibreOffice draw adjacent paragraphs that share a border as one box (and
    join adjacent tables into one table), so back-to-back code blocks or quotes would
    merge in the DOCX output. ``kinds`` maps block types to groups (a plain list of
    types makes each type its own group). Every block list of the document is processed.
    """
    groups = dict(kinds) if isinstance(kinds, Mapping) else {kind: kind for kind in kinds}

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            if "c" in value:
                value["c"] = visit(value["c"])
            return value
        if not isinstance(value, list):
            return value
        result: list[Any] = []
        previous: Any = None
        for item in value:
            item = visit(item)
            if (
                isinstance(item, dict)
                and isinstance(previous, dict)
                and groups.get(item.get("t"))
                and groups.get(item.get("t")) == groups.get(previous.get("t"))
            ):
                result.append(dict(spacer))
            result.append(item)
            previous = item
        return result

    return visit(blocks)


# ---------------------------------------------------------------------------
# DOCX layout: available width per element, quotes as boxes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DocxLayout:
    """Horizontal space taken by DOCX containers, in CSS px (1 px = 15 twips).

    ``root_px`` is the text width of the page; pandoc sizes table columns from it.
    """

    root_px: float
    list_indent_px: float = 48.0  # pandoc's list numbering: 720 twips per level
    quote_inset_px: float = 26.0  # quote box: 3 pt bar plus 200/120 twips cell margins
    cell_padding_px: float = 14.4  # 2 x 108 twips (reference table style)
    definition_indent_px: float = 24.0  # "Definition" style: 360 twips
    default_table_px: float = 528.0  # pandoc's width for tables without widths (5.5 in)


@dataclass(frozen=True)
class Room:
    """Space available to a block: its width, and how far list levels indent it from
    the left edge of its container (page or table cell), both in CSS px."""

    width: float
    indent: float = 0.0


PlacedTransform = Callable[[Node, Room], "Node | list[Node] | None"]


def walk_with_room(blocks: list[Node], layout: DocxLayout, fn: PlacedTransform) -> list[Node]:
    """Like :func:`walk`, but ``fn`` also gets the :class:`Room` of each element.

    Lists indent their items, block quotes and definitions take their inset, table
    cells get their column's width and footnotes the whole page width.
    """

    def visit_list(items: list[Any], room: Room) -> list[Any]:
        result: list[Any] = []
        for item in items:
            new = visit(item, room)
            if isinstance(item, dict) and "t" in item and isinstance(new, list):
                result.extend(new)
            else:
                result.append(new)
        return result

    def visit(value: Any, room: Room) -> Any:
        if isinstance(value, list):
            return visit_list(value, room)
        if not isinstance(value, dict) or "t" not in value:
            return value
        kind, contents = value["t"], value.get("c")
        nested = Room(room.width - layout.list_indent_px, room.indent + layout.list_indent_px)
        if kind == "BulletList":
            value["c"] = [visit_list(item, nested) for item in contents]
        elif kind == "OrderedList":
            contents[1] = [visit_list(item, nested) for item in contents[1]]
        elif kind == "BlockQuote":
            value["c"] = visit_list(contents, Room(room.width - layout.quote_inset_px))
        elif kind == "DefinitionList":
            inset = Room(room.width - layout.definition_indent_px, room.indent)
            value["c"] = [
                [visit_list(term, room), [visit_list(definition, inset) for definition in defs]]
                for term, defs in contents
            ]
        elif kind == "Note":
            value["c"] = visit_list(contents, Room(layout.root_px))
        elif kind == "Table":
            _visit_table(value, room)
        elif "c" in value:
            value["c"] = visit(contents, room)
        replacement = fn(value, room)
        return value if replacement is None else replacement

    def _visit_table(table: Node, room: Room) -> None:
        _, caption, colspecs, _, _, _ = table["c"]
        caption[1] = visit_list(caption[1], room)
        columns: list[float] = []
        for spec in colspecs:
            if spec[1].get("t") == "ColWidth":
                columns.append(spec[1]["c"] * layout.root_px)
            else:
                columns.append(min(layout.root_px, layout.default_table_px) / len(colspecs))
        for _, cells in _table_rows(table):
            column = 0
            for cell in cells:
                span_width = sum(columns[column : column + cell[3]]) or columns[-1]
                column += cell[3]
                cell[4] = visit_list(cell[4], Room(max(span_width - layout.cell_padding_px, 1)))

    return visit_list(blocks, Room(layout.root_px))


# Quote boxes: a one-cell table with a colored bar keeps lists, code and tables inside
# the quote (Word applies the "Block Text" style to paragraphs only) and nests properly.
QUOTE_BAR_COLOR = "D0D7DE"
ALERT_COLORS: dict[str, str] = {
    "note": "0969DA",
    "tip": "1A7F37",
    "important": "8250DF",
    "warning": "9A6700",
    "caution": "D1242F",
}
QUOTE_TEXT_STYLE = "Block Text"
_BOX_CLOSE = "</w:tc></w:tr></w:tbl>"
_TWIPS_PER_PX = 15


def _tint(color: str, amount: float = 0.05) -> str:
    """``color`` mixed with white (``amount`` of the color), as hex."""
    channels = (int(color[index : index + 2], 16) for index in (0, 2, 4))
    return "".join(f"{round(c * amount + 255 * (1 - amount)):02X}" for c in channels)


def _box_open(room: Room, color: str, fill: str | None) -> Node:
    width = max(round(room.width * _TWIPS_PER_PX), 720)
    indent = round(room.indent * _TWIPS_PER_PX)
    shading = f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else ""
    return raw_block(
        "openxml",
        "<w:tbl><w:tblPr>"
        f'<w:tblW w:w="{width}" w:type="dxa"/><w:tblInd w:w="{indent}" w:type="dxa"/>'
        '<w:tblBorders><w:top w:val="nil"/>'
        f'<w:left w:val="single" w:sz="24" w:space="0" w:color="{color}"/>'
        '<w:bottom w:val="nil"/><w:right w:val="nil"/><w:insideH w:val="nil"/>'
        '<w:insideV w:val="nil"/></w:tblBorders><w:tblLayout w:type="fixed"/>'
        '<w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="200" w:type="dxa"/>'
        '<w:bottom w:w="40" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tblCellMar>'
        '<w:tblLook w:val="0000" w:firstRow="0" w:lastRow="0" w:firstColumn="0" '
        'w:lastColumn="0" w:noHBand="1" w:noVBand="1"/></w:tblPr>'
        f'<w:tblGrid><w:gridCol w:w="{width}"/></w:tblGrid>'
        f'<w:tr><w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shading}</w:tcPr>',
    )


def box_quotes(blocks: list[Node], layout: DocxLayout, spacer: Node) -> list[Node]:
    """DOCX: write block quotes and alerts as one-cell tables with a left bar.

    Plain quotes get a grey bar and the muted ``Block Text`` paragraph style; alerts a
    bar in their color and a light background. A cell (and so a box) must end with a
    paragraph, so ``spacer`` is added where a box or table would be the last block.
    """

    def fn(node: Node, room: Room) -> list[Node] | None:
        if node["t"] != "BlockQuote":
            return None
        kind = alert_kind(node)
        content: list[Node] = node["c"]
        if not content or content[-1]["t"] in ("Table", "RawBlock"):
            content = [*content, dict(spacer)]
        if kind is None:
            color, fill = QUOTE_BAR_COLOR, None
            content = [
                {"t": "Div", "c": [make_attr(**{"custom-style": QUOTE_TEXT_STYLE}), content]}
            ]
        else:
            color = ALERT_COLORS[kind]
            fill = _tint(color)
        return [_box_open(room, color, fill), *content, raw_block("openxml", _BOX_CLOSE)]

    boxed = walk_with_room(blocks, layout, fn)
    return _end_cells_with_paragraphs(boxed, spacer)


def _end_cells_with_paragraphs(blocks: list[Node], spacer: Node) -> list[Node]:
    """Append ``spacer`` to nested block lists that end with a quote box."""

    def is_box_end(item: Any) -> bool:
        return (
            isinstance(item, dict)
            and item.get("t") == "RawBlock"
            and item["c"] == ["openxml", _BOX_CLOSE]
        )

    def visit(value: Any, top: bool) -> Any:
        if isinstance(value, dict):
            if "c" in value:
                value["c"] = visit(value["c"], False)
            return value
        if not isinstance(value, list):
            return value
        result = [visit(item, False) for item in value]
        if not top and result and is_box_end(result[-1]):
            result.append(dict(spacer))
        return result

    return visit(blocks, True)


# ---------------------------------------------------------------------------
# Smaller fixes
# ---------------------------------------------------------------------------
TASK_CHECKBOX_HTML = '<input type="checkbox" class="task-list-item-checkbox" disabled=""{} />'
CHECKBOX_SYMBOLS = {False: "☐", True: "☒"}  # what pandoc uses for bullet task lists


_SPACE: Node = {"t": "Space"}
_UNCHECKED_TASK = [{"t": "Str", "c": "["}, _SPACE, {"t": "Str", "c": "]"}, _SPACE]
_CHECKED_TASKS = ({"t": "Str", "c": "[x]"}, {"t": "Str", "c": "[X]"})


def _task_marker(inlines: list[Node]) -> tuple[bool, int] | None:
    """(checked, number of inlines to drop) when ``inlines`` start with ``[ ]``/``[x]``."""
    if inlines[:4] == _UNCHECKED_TASK:
        return False, 4
    if len(inlines) > 1 and inlines[0] in _CHECKED_TASKS and inlines[1] == _SPACE:
        return True, 2
    return None


def ordered_task_lists(blocks: list[Node], *, html: bool) -> list[Node]:
    """Task items (``1. [ ] step``) in ordered lists, which pandoc only handles in bullet lists.

    ``html``: a checkbox like pandoc's bullet task lists (PDF); otherwise the ☐/☒
    symbols pandoc uses in Word.
    """

    def fn(node: Node) -> None:
        if node["t"] != "OrderedList":
            return
        for item in node["c"][1]:
            if not item or item[0]["t"] not in ("Plain", "Para"):
                continue
            inlines: list[Node] = item[0]["c"]
            marker = _task_marker(inlines)
            if marker is None:
                continue
            checked, size = marker
            if html:
                box = TASK_CHECKBOX_HTML.format(' checked=""' if checked else "")
                item[0]["c"] = [{"t": "RawInline", "c": ["html", box]}, *inlines[size:]]
            else:
                symbol = {"t": "Str", "c": CHECKBOX_SYMBOLS[checked]}
                item[0]["c"] = [symbol, {"t": "Space"}, *inlines[size:]]

    return walk(blocks, fn)


def heading_code_spans(blocks: list[Node], style: str) -> list[Node]:
    """DOCX: inline code in headings as a ``custom-style`` span (``style`` keeps the code
    font without the fixed size of body inline code, so it scales with the heading)."""

    def code_to_span(node: Node) -> Node | None:
        if node["t"] != "Code":
            return None
        return span([], [{"t": "Str", "c": node["c"][1]}], **{"custom-style": style})

    def fn(node: Node) -> None:
        if node["t"] == "Header":
            node["c"][2] = walk(node["c"][2], code_to_span)

    return walk(blocks, fn)


def aligned_headers(blocks: list[Node]) -> dict[str, str]:
    """DOCX: identifiers of headings with an HTML ``align`` (``center``/``right``), which
    Word only gets from a paragraph property; headings without one get an identifier."""
    aligned: dict[str, str] = {}
    used = {node["c"][1][0] for node in find_all(blocks, lambda n: n["t"] == "Header")}

    def fn(node: Node) -> None:
        if node["t"] != "Header":
            return
        attr = node["c"][1]
        align = (attr_value(attr, "align") or "").strip().lower()
        if align not in ("center", "right"):
            return
        if not attr[0]:
            number = len(aligned) + 1
            while f"aligned-heading-{number}" in used:
                number += 1
            attr[0] = f"aligned-heading-{number}"
            used.add(attr[0])
        aligned[attr[0]] = align

    walk(blocks, fn)
    return aligned
