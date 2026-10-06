"""Base Word reference document (``reference.docx``) used for DOCX output.

pandoc copies the styles, theme, font table and some settings of a *reference document* into
every DOCX it writes. :func:`build_base_reference` starts from pandoc's own default reference
and restyles it with lxml, so the generated files look clean in Microsoft Word, LibreOffice and
Google Docs: Calibri body text, a clear heading hierarchy, Consolas code blocks on a light gray
panel, bordered tables with a shaded bold header row, quote bars and blue links (a GitHub-like
light palette, in line with the PDF output).

Syntax-highlighting token styles (``KeywordTok``, ``StringTok``...) are deliberately *not*
defined here: pandoc only generates them from ``--syntax-highlighting`` when the reference
lacks them, so leaving them out keeps the DOCX colors in sync with the PDF.

Page setup (size, orientation, margins) and the page-number footer are applied per job by
:mod:`app.converter.docx`, which patches the single final ``w:sectPr`` of ``word/document.xml``;
this module leaves that part untouched.

CLI (used by the Dockerfile)::

    python -m app.converter.docx_reference OUT_PATH [--pandoc PATH]
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from app.config import get_settings

__all__ = ["ReferenceDocError", "build_base_reference", "main", "restyle_reference"]

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"w": W_NS, "a": A_NS, "ct": CT_NS}

STYLES_PART = "word/styles.xml"
THEME_PART = "word/theme/theme1.xml"
FONT_TABLE_PART = "word/fontTable.xml"
SETTINGS_PART = "word/settings.xml"
DOCUMENT_PART = "word/document.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"

PANDOC_TIMEOUT_SECONDS = 60

# Typography.
BODY_FONT = "Calibri"
CODE_FONT = "Consolas"
BODY_SIZE = 11.0
CODE_SIZE = 9.5
SMALL_SIZE = 9.0
TABLE_SMALL_SIZE = 8.0  # wide tables (see ast.TABLE_SMALL_FONT_SCALE)
DEFAULT_LANGUAGE = "pt-BR"

# GitHub-like light palette (hex RGB).
TEXT = "1F2328"
MUTED = "59636E"
HEADING = "1F3864"
LINK = "0969DA"
BORDER = "D0D7DE"
RULE = "D8DEE4"
CODE_BG = "F6F8FA"
TABLE_HEADER_BG = "F6F8FA"
TABLE_STRIPE_BG = "FAFBFC"

# Measurements are in twips (1/20 pt) unless stated otherwise. Word adds the space after a
# paragraph to the space before the next one; the "before" part also separates text from a
# table above it.
BODY_BEFORE = 80
BODY_AFTER = 100
LINE_SINGLE = 240
LINE_BODY = 276  # 1.15 lines
CELL_PADDING_X = 108
CELL_PADDING_Y = 43

# Font metadata registered in word/fontTable.xml: Word uses it to substitute a font of the same
# kind (monospaced, here) on systems where the font is not installed.
_FONT_TABLE_ENTRIES: dict[str, dict[str, str | dict[str, str]]] = {
    CODE_FONT: {
        "panose1": "020B0609020204030204",
        "charset": "00",
        "family": "modern",
        "pitch": "fixed",
        "sig": {
            "usb0": "E00006FF",
            "usb1": "0000FCFF",
            "usb2": "00000001",
            "usb3": "00000000",
            "csb0": "0000019F",
            "csb1": "00000000",
        },
    },
}
_THEME_FONT_PANOSE = "020F0502020204030204"  # Calibri
# Fixed timestamp for the zip entries so that rebuilding produces identical bytes.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Word opens documents without these settings in "Compatibility Mode" (Word 2007 layout rules).
_COMPAT_SETTINGS = (
    ("compatibilityMode", "15"),
    ("overrideTableStyleFontSizeAndJustification", "1"),
    ("enableOpenTypeFeatures", "1"),
    ("doNotFlipMirrorIndents", "1"),
    ("differentiateMultirowTableHeaders", "1"),
)
_COMPAT_URI = "http://schemas.microsoft.com/office/word"
# CT_Settings children that must come after w:compat (schema order).
_SETTINGS_AFTER_COMPAT = (
    "docVars",
    "rsids",
    "mathPr",
    "attachedSchema",
    "themeFontLang",
    "clrSchemeMapping",
    "doNotIncludeSubdocsInStats",
    "doNotAutoCompressPictures",
    "forceUpgrade",
    "captions",
    "readModeInkLockDown",
    "smartTagType",
    "schemaLibrary",
    "shapeDefaults",
    "doNotEmbedSmartTags",
    "decimalSymbol",
    "listSeparator",
)
_THEME_FONT_ATTRS = ("asciiTheme", "hAnsiTheme", "cstheme")
_THEME_COLOR_ATTRS = ("themeColor", "themeShade", "themeTint")


class ReferenceDocError(RuntimeError):
    """The reference document could not be generated or failed validation."""


# --------------------------------------------------------------------------------------------
# Declarative style model
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Border:
    size: int  # eighths of a point
    color: str
    space: int = 0  # points between the border and the text


@dataclass(frozen=True)
class RunProps:
    font: str | None = None
    size: float | None = None  # points
    bold: bool | None = None
    italic: bool | None = None
    color: str | None = None
    underline: bool = False
    shading: str | None = None
    no_proof: bool = False
    superscript: bool = False


@dataclass(frozen=True)
class ParaProps:
    keep_next: bool = False
    keep_lines: bool = False
    borders: tuple[tuple[str, Border], ...] = ()  # (side, border); side in top/left/bottom/right
    shading: str | None = None
    allow_word_break: bool = False
    before: int | None = None
    after: int | None = None
    line: int | None = None  # in 240ths of a line
    indent_left: int | None = None
    indent_right: int | None = None
    align: str | None = None
    outline_level: int | None = None


@dataclass(frozen=True)
class Style:
    style_id: str
    name: str
    kind: str = "paragraph"
    based_on: str | None = None
    next_style: str | None = None
    link: str | None = None
    custom: bool = False
    ui_priority: int | None = None
    quick: bool = True
    para: ParaProps | None = None
    run: RunProps | None = None


def _box(border: Border) -> tuple[tuple[str, Border], ...]:
    return tuple((side, border) for side in ("top", "left", "bottom", "right"))


# (level, size pt, color, space before, space after, bottom rule)
_HEADING_SPECS: tuple[tuple[int, float, str, int, int, bool], ...] = (
    (1, 20, HEADING, 360, 120, True),
    (2, 16, HEADING, 320, 100, True),
    (3, 14, HEADING, 280, 80, False),
    (4, 12, HEADING, 240, 60, False),
    (5, 11, TEXT, 220, 40, False),
    (6, 11, MUTED, 220, 40, False),
    (7, 11, MUTED, 200, 40, False),
    (8, 11, MUTED, 200, 40, False),
    (9, 11, MUTED, 200, 40, False),
)


def _heading_styles() -> list[Style]:
    styles: list[Style] = []
    for level, size, color, before, after, rule in _HEADING_SPECS:
        run = RunProps(font=BODY_FONT, size=size, bold=True, italic=False, color=color)
        para = ParaProps(
            keep_next=True,
            keep_lines=True,
            borders=(("bottom", Border(6, RULE, 4)),) if rule else (),
            before=before,
            after=after,
            line=LINE_SINGLE,
            outline_level=level - 1,
        )
        styles.append(
            Style(
                f"Heading{level}",
                f"heading {level}",
                based_on="Normal",
                next_style="BodyText",
                link=f"Heading{level}Char",
                ui_priority=9,
                para=para,
                run=run,
            )
        )
        styles.append(
            Style(
                f"Heading{level}Char",
                f"Heading {level} Char",
                kind="character",
                based_on="DefaultParagraphFont",
                link=f"Heading{level}",
                custom=True,
                ui_priority=9,
                quick=False,
                run=run,
            )
        )
    return styles


def _managed_styles() -> list[Style]:
    """Every style this module (re)defines, keyed by ``styleId`` when applied."""
    title_run = RunProps(font=BODY_FONT, size=26, bold=True, color=HEADING)
    subtitle_run = RunProps(size=14, bold=False, color=MUTED)
    byline_run = RunProps(size=BODY_SIZE, bold=False, color=MUTED)
    byline_para = ParaProps(keep_next=True, keep_lines=True, before=0, after=40)
    caption_run = RunProps(size=CODE_SIZE, italic=True, color=MUTED)
    return [
        Style(
            "BodyText",
            "Body Text",
            based_on="Normal",
            link="BodyTextChar",
            para=ParaProps(before=BODY_BEFORE, after=BODY_AFTER, line=LINE_BODY),
        ),
        Style(
            "FirstParagraph",
            "First Paragraph",
            based_on="BodyText",
            next_style="BodyText",
            custom=True,
        ),
        Style(
            "Compact",
            "Compact",
            based_on="BodyText",
            custom=True,
            para=ParaProps(before=36, after=36),
        ),
        # Cells of tables too wide for the body font (custom-style "Compact Small").
        Style(
            "CompactSmall",
            "Compact Small",
            based_on="Compact",
            custom=True,
            quick=False,
            para=ParaProps(line=LINE_SINGLE),
            run=RunProps(size=TABLE_SMALL_SIZE),
        ),
        # Paragraphs of HTML blocks with align="center" / align="right".
        Style(
            "Centered",
            "Centered",
            based_on="BodyText",
            custom=True,
            quick=False,
            para=ParaProps(align="center"),
        ),
        Style(
            "RightAligned",
            "Right Aligned",
            based_on="BodyText",
            custom=True,
            quick=False,
            para=ParaProps(align="right"),
        ),
        Style(
            "Title",
            "Title",
            based_on="Normal",
            next_style="BodyText",
            link="TitleChar",
            ui_priority=10,
            para=ParaProps(keep_next=True, before=0, after=120, line=LINE_SINGLE, align="center"),
            run=title_run,
        ),
        Style(
            "TitleChar",
            "Title Char",
            kind="character",
            based_on="DefaultParagraphFont",
            link="Title",
            custom=True,
            ui_priority=10,
            quick=False,
            run=title_run,
        ),
        Style(
            "Subtitle",
            "Subtitle",
            based_on="Title",
            next_style="BodyText",
            link="SubtitleChar",
            ui_priority=11,
            para=ParaProps(keep_next=True, before=0, after=160),
            run=subtitle_run,
        ),
        Style(
            "SubtitleChar",
            "Subtitle Char",
            kind="character",
            based_on="DefaultParagraphFont",
            link="Subtitle",
            custom=True,
            ui_priority=11,
            quick=False,
            run=subtitle_run,
        ),
        Style(
            "Author",
            "Author",
            based_on="Title",
            next_style="BodyText",
            custom=True,
            para=byline_para,
            run=byline_run,
        ),
        Style(
            "Date",
            "Date",
            based_on="Title",
            next_style="BodyText",
            para=byline_para,
            run=byline_run,
        ),
        *_heading_styles(),
        # Paragraphs of block quotes; the bar and indent come from the one-cell table the
        # quote is written as (see ast.box_quotes).
        Style(
            "BlockText",
            "Block Text",
            based_on="BodyText",
            next_style="BodyText",
            ui_priority=9,
            para=ParaProps(before=60, after=60, indent_left=0, indent_right=0),
            run=RunProps(color=MUTED),
        ),
        Style(
            "SourceCode",
            "Source Code",
            based_on="Normal",
            custom=True,
            para=ParaProps(
                borders=_box(Border(4, BORDER, 4)),
                shading=CODE_BG,
                allow_word_break=True,
                before=120,
                after=160,
                line=LINE_SINGLE,
                indent_left=115,
                indent_right=115,
            ),
            run=RunProps(font=CODE_FONT, size=CODE_SIZE, no_proof=True),
        ),
        Style(
            "VerbatimChar",
            "Verbatim Char",
            kind="character",
            based_on="BodyTextChar",
            custom=True,
            quick=False,
            run=RunProps(font=CODE_FONT, size=CODE_SIZE, shading=CODE_BG, no_proof=True),
        ),
        # Inline code in headings: no fixed size, so it follows the heading's size.
        Style(
            "VerbatimHeadingChar",
            "Verbatim Heading Char",
            kind="character",
            based_on="DefaultParagraphFont",
            custom=True,
            quick=False,
            run=RunProps(font=CODE_FONT, shading=CODE_BG, no_proof=True),
        ),
        Style(
            "Hyperlink",
            "Hyperlink",
            kind="character",
            based_on="BodyTextChar",
            ui_priority=99,
            quick=False,
            run=RunProps(color=LINK, underline=True),
        ),
        Style(
            "FootnoteText",
            "Footnote Text",
            based_on="Normal",
            next_style="FootnoteText",
            ui_priority=9,
            para=ParaProps(before=0, after=40, line=LINE_SINGLE),
            run=RunProps(size=SMALL_SIZE),
        ),
        Style(
            "FootnoteReference",
            "Footnote Reference",
            kind="character",
            based_on="BodyTextChar",
            quick=False,
            run=RunProps(superscript=True),
        ),
        Style(
            "Caption",
            "Caption",
            based_on="Normal",
            para=ParaProps(before=60, after=200, line=LINE_SINGLE),
            run=caption_run,
        ),
        Style(
            "TableCaption",
            "Table Caption",
            based_on="Caption",
            custom=True,
            para=ParaProps(keep_next=True, before=120, after=60),
        ),
        Style(
            "ImageCaption",
            "Image Caption",
            based_on="Caption",
            custom=True,
            para=ParaProps(align="center"),
        ),
        Style(
            "Figure",
            "Figure",
            based_on="Normal",
            custom=True,
            para=ParaProps(before=120, after=120, align="center"),
        ),
        Style(
            "CaptionedFigure",
            "Captioned Figure",
            based_on="Figure",
            custom=True,
            para=ParaProps(keep_next=True, after=0),
        ),
        Style(
            "DefinitionTerm",
            "Definition Term",
            based_on="Normal",
            next_style="Definition",
            custom=True,
            para=ParaProps(keep_next=True, keep_lines=True, before=120, after=0),
            run=RunProps(bold=True),
        ),
        Style(
            "Definition",
            "Definition",
            based_on="Normal",
            custom=True,
            para=ParaProps(indent_left=360),
        ),
        Style(
            "TOCHeading",
            "TOC Heading",
            based_on="Heading1",
            next_style="BodyText",
            ui_priority=39,
            para=ParaProps(before=240, after=160, outline_level=9),
        ),
        *(
            Style(
                f"TOC{level}",
                f"toc {level}",
                based_on="Normal",
                next_style="Normal",
                ui_priority=39,
                quick=False,
                para=ParaProps(before=40, after=40, indent_left=(level - 1) * 240),
            )
            for level in (1, 2, 3)
        ),
    ]


# --------------------------------------------------------------------------------------------
# lxml helpers (always create elements inside the target tree so existing prefixes are reused)
# --------------------------------------------------------------------------------------------


def _w(local: str) -> str:
    return f"{{{W_NS}}}{local}"


def _sub(parent: etree._Element, tag: str, **attrs: str | int) -> etree._Element:
    return etree.SubElement(parent, _w(tag), {_w(k): str(v) for k, v in attrs.items()})


def _half_points(size: float) -> str:
    return str(round(size * 2))


def _on_off(parent: etree._Element, tag: str, value: bool | None) -> None:
    if value is None:
        return
    if value:
        _sub(parent, tag)
    else:
        _sub(parent, tag, val="0")


def _write_border(parent: etree._Element, side: str, border: Border) -> None:
    _sub(parent, side, val="single", sz=border.size, space=border.space, color=border.color)


def _write_shading(parent: etree._Element, fill: str) -> None:
    _sub(parent, "shd", val="clear", color="auto", fill=fill)


def _write_rpr(parent: etree._Element, run: RunProps) -> None:
    """Append ``w:rPr`` with children in CT_RPr schema order."""
    rpr = _sub(parent, "rPr")
    if run.font:
        _sub(rpr, "rFonts", ascii=run.font, hAnsi=run.font, cs=run.font)
    _on_off(rpr, "b", run.bold)
    _on_off(rpr, "bCs", run.bold)
    _on_off(rpr, "i", run.italic)
    _on_off(rpr, "iCs", run.italic)
    if run.no_proof:
        _sub(rpr, "noProof")
    if run.color:
        _sub(rpr, "color", val=run.color)
    if run.size is not None:
        _sub(rpr, "sz", val=_half_points(run.size))
        _sub(rpr, "szCs", val=_half_points(run.size))
    if run.underline:
        _sub(rpr, "u", val="single")
    if run.shading:
        _write_shading(rpr, run.shading)
    if run.superscript:
        _sub(rpr, "vertAlign", val="superscript")


def _write_ppr(parent: etree._Element, para: ParaProps) -> None:
    """Append ``w:pPr`` with children in CT_PPrBase schema order."""
    ppr = _sub(parent, "pPr")
    if para.keep_next:
        _sub(ppr, "keepNext")
    if para.keep_lines:
        _sub(ppr, "keepLines")
    if para.borders:
        pbdr = _sub(ppr, "pBdr")
        sides = dict(para.borders)
        for side in ("top", "left", "bottom", "right"):
            if side in sides:
                _write_border(pbdr, side, sides[side])
    if para.shading:
        _write_shading(ppr, para.shading)
    if para.allow_word_break:
        # "Allow Latin text to wrap in the middle of a word": long tokens never overflow.
        _sub(ppr, "wordWrap", val="0")
    spacing = {
        key: value
        for key, value in (("before", para.before), ("after", para.after), ("line", para.line))
        if value is not None
    }
    if para.line is not None:
        spacing["lineRule"] = "auto"
    if spacing:
        _sub(ppr, "spacing", **spacing)
    indent = {
        key: value
        for key, value in (("left", para.indent_left), ("right", para.indent_right))
        if value is not None
    }
    if indent:
        _sub(ppr, "ind", **indent)
    if para.align:
        _sub(ppr, "jc", val=para.align)
    if para.outline_level is not None:
        _sub(ppr, "outlineLvl", val=para.outline_level)


def _new_style(
    root: etree._Element, style_type: str, style_id: str, **attrs: str
) -> etree._Element:
    """Create ``w:style`` at the position of the existing definition (or at the end)."""
    existing = root.find(f"w:style[@w:styleId='{style_id}']", NS)
    element = _sub(root, "style", type=style_type, **attrs, styleId=style_id)
    if existing is not None:
        existing.addprevious(element)
        root.remove(existing)
    return element


def _apply_style(root: etree._Element, style: Style) -> None:
    attrs = {"customStyle": "1"} if style.custom else {}
    element = _new_style(root, style.kind, style.style_id, **attrs)
    _sub(element, "name", val=style.name)
    if style.based_on:
        _sub(element, "basedOn", val=style.based_on)
    if style.next_style:
        _sub(element, "next", val=style.next_style)
    if style.link:
        _sub(element, "link", val=style.link)
    if style.ui_priority is not None:
        _sub(element, "uiPriority", val=style.ui_priority)
    if style.quick:
        _sub(element, "qFormat")
    if style.para:
        _write_ppr(element, style.para)
    if style.run:
        _write_rpr(element, style.run)


def _apply_table_style(root: etree._Element) -> None:
    """Default table style: thin gray grid, roomy cells, bold shaded header, subtle stripes."""
    style = _new_style(root, "table", "Table", default="1")
    _sub(style, "name", val="Table")
    _sub(style, "uiPriority", val=59)
    _sub(style, "qFormat")
    tbl_pr = _sub(style, "tblPr")
    _sub(tbl_pr, "tblStyleRowBandSize", val=1)
    _sub(tbl_pr, "tblInd", w=0, type="dxa")
    borders = _sub(tbl_pr, "tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        _write_border(borders, side, Border(4, BORDER))
    margins = _sub(tbl_pr, "tblCellMar")
    for side, width in (
        ("top", CELL_PADDING_Y),
        ("left", CELL_PADDING_X),
        ("bottom", CELL_PADDING_Y),
        ("right", CELL_PADDING_X),
    ):
        _sub(margins, side, w=width, type="dxa")

    header = _sub(style, "tblStylePr", type="firstRow")
    _write_ppr(header, ParaProps(keep_next=True))
    _write_rpr(header, RunProps(bold=True, color=TEXT))
    header_trpr = _sub(header, "trPr")
    _sub(header_trpr, "tblHeader")
    header_tcpr = _sub(header, "tcPr")
    _write_shading(header_tcpr, TABLE_HEADER_BG)
    _sub(header_tcpr, "vAlign", val="bottom")

    stripe = _sub(style, "tblStylePr", type="band2Horz")
    _write_shading(_sub(stripe, "tcPr"), TABLE_STRIPE_BG)


def _apply_doc_defaults(root: etree._Element) -> None:
    defaults = root.find("w:docDefaults", NS)
    if defaults is None:
        defaults = _sub(root, "docDefaults")
        root.insert(0, defaults)
    for child in list(defaults):
        defaults.remove(child)
    rpr = _sub(_sub(defaults, "rPrDefault"), "rPr")
    # East Asian text keeps the theme's script-specific fonts as its fallback.
    _sub(
        rpr, "rFonts", ascii=BODY_FONT, eastAsiaTheme="minorEastAsia", hAnsi=BODY_FONT, cs=BODY_FONT
    )
    _sub(rpr, "color", val=TEXT)
    _sub(rpr, "sz", val=_half_points(BODY_SIZE))
    _sub(rpr, "szCs", val=_half_points(BODY_SIZE))
    _sub(rpr, "lang", val=DEFAULT_LANGUAGE, eastAsia="zh-CN", bidi="ar-SA")
    ppr = _sub(_sub(defaults, "pPrDefault"), "pPr")
    _sub(ppr, "spacing", after=BODY_AFTER, line=LINE_BODY, lineRule="auto")


def _drop_theme_references(root: etree._Element) -> None:
    """Make remaining styles use explicit fonts/colors (theme lookups win over explicit values)."""
    for fonts in root.iter(_w("rFonts")):
        for attr in _THEME_FONT_ATTRS:
            fonts.attrib.pop(_w(attr), None)
        if not fonts.attrib:
            fonts.getparent().remove(fonts)
    for color in root.iter(_w("color")):
        for attr in _THEME_COLOR_ATTRS:
            color.attrib.pop(_w(attr), None)


def _restyle_styles(root: etree._Element) -> None:
    _apply_doc_defaults(root)
    for style in _managed_styles():
        _apply_style(root, style)
    _apply_table_style(root)
    _drop_theme_references(root)


def _restyle_theme(root: etree._Element) -> None:
    """Point the theme's heading/body fonts at Calibri ("Body"/"Headings" in Word's font menu)."""
    scheme = root.find("a:themeElements/a:fontScheme", NS)
    if scheme is None:
        raise ReferenceDocError("theme1.xml has no font scheme")
    scheme.set("name", BODY_FONT)
    for kind in ("majorFont", "minorFont"):
        latin = scheme.find(f"a:{kind}/a:latin", NS)
        if latin is None:
            raise ReferenceDocError(f"theme1.xml has no {kind}/latin font")
        latin.set("typeface", BODY_FONT)
        latin.set("panose", _THEME_FONT_PANOSE)


def _restyle_font_table(root: etree._Element) -> None:
    for name, entry in _FONT_TABLE_ENTRIES.items():
        if root.find(f"w:font[@w:name='{name}']", NS) is not None:
            continue
        font = _sub(root, "font", name=name)
        for key, value in entry.items():
            if isinstance(value, dict):
                _sub(font, key, **value)
            else:
                _sub(font, key, val=value)


def _restyle_settings(root: etree._Element) -> None:
    compat = root.find("w:compat", NS)
    if compat is None:
        compat = _sub(root, "compat")
        successor = next(
            (el for el in root if etree.QName(el).localname in _SETTINGS_AFTER_COMPAT), None
        )
        if successor is not None:
            successor.addprevious(compat)
    for name, value in _COMPAT_SETTINGS:
        for old in compat.findall(f"w:compatSetting[@w:name='{name}']", NS):
            compat.remove(old)
        _sub(compat, "compatSetting", name=name, uri=_COMPAT_URI, val=value)


_PART_PATCHERS = {
    STYLES_PART: _restyle_styles,
    THEME_PART: _restyle_theme,
    FONT_TABLE_PART: _restyle_font_table,
    SETTINGS_PART: _restyle_settings,
}


# --------------------------------------------------------------------------------------------
# Package handling
# --------------------------------------------------------------------------------------------


def _parse_xml(data: bytes, part: str) -> etree._ElementTree:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        return etree.ElementTree(etree.fromstring(data, parser))
    except etree.XMLSyntaxError as exc:
        raise ReferenceDocError(f"{part} is not well-formed XML: {exc}") from exc


def _serialize(tree: etree._ElementTree) -> bytes:
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _read_package(data: bytes) -> list[tuple[zipfile.ZipInfo, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return [(info, archive.read(info)) for info in archive.infolist()]
    except zipfile.BadZipFile as exc:
        raise ReferenceDocError(f"not a valid DOCX package: {exc}") from exc


def _write_package(entries: Iterable[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for original, content in entries:
            # Keep names and order; pin timestamps so the output is reproducible.
            info = zipfile.ZipInfo(original.filename, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def _check_ignorable_prefixes(root: etree._Element, part: str) -> None:
    """Word refuses files whose ``mc:Ignorable`` lists an undeclared namespace prefix."""
    attr = f"{{{MC_NS}}}Ignorable"
    for element in root.iter(etree.Element):
        value = element.get(attr)
        if value is None:
            continue
        missing = [prefix for prefix in value.split() if prefix not in element.nsmap]
        if missing:
            raise ReferenceDocError(f"{part}: undeclared mc:Ignorable prefixes {missing}")


def _check_content_types(parts: dict[str, bytes]) -> None:
    types = _parse_xml(parts[CONTENT_TYPES_PART], CONTENT_TYPES_PART).getroot()
    defaults = {el.get("Extension", "").lower() for el in types.iterfind("ct:Default", NS)}
    overrides = {el.get("PartName", "").lower() for el in types.iterfind("ct:Override", NS)}
    for name in parts:
        if name == CONTENT_TYPES_PART or name.endswith("/"):
            continue
        base = name.rsplit("/", 1)[-1]
        extension = base.rpartition(".")[2].lower() if "." in base else ""
        if f"/{name}".lower() not in overrides and extension not in defaults:
            raise ReferenceDocError(f"{name} has no content type")


def _check_document(parts: dict[str, bytes]) -> None:
    body = _parse_xml(parts[DOCUMENT_PART], DOCUMENT_PART).getroot().find("w:body", NS)
    if body is None:
        raise ReferenceDocError("document.xml has no body")
    sections = list(body.iter(_w("sectPr")))
    if len(sections) != 1 or len(body) == 0 or body[-1] is not sections[0]:
        raise ReferenceDocError("document.xml must end with exactly one w:sectPr")


def _check_styles(parts: dict[str, bytes]) -> None:
    root = _parse_xml(parts[STYLES_PART], STYLES_PART).getroot()
    ids = [el.get(_w("styleId")) for el in root.iterfind("w:style", NS)]
    duplicates = sorted({style_id for style_id in ids if ids.count(style_id) > 1})
    if duplicates:
        raise ReferenceDocError(f"duplicate style ids: {duplicates}")


def _validate(data: bytes) -> None:
    entries = _read_package(data)
    parts = {info.filename: content for info, content in entries}
    if len(parts) != len(entries):
        raise ReferenceDocError("duplicate entries in the DOCX package")
    for required in (CONTENT_TYPES_PART, DOCUMENT_PART, *_PART_PATCHERS):
        if required not in parts:
            raise ReferenceDocError(f"missing part {required}")
    for name, content in parts.items():
        if name.endswith((".xml", ".rels")):
            _check_ignorable_prefixes(_parse_xml(content, name).getroot(), name)
    _check_content_types(parts)
    _check_document(parts)
    _check_styles(parts)


def restyle_reference(data: bytes) -> bytes:
    """Return a restyled copy of a DOCX reference document (pure function, validated)."""
    entries = _read_package(data)
    patched: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, content in entries:
        patch = _PART_PATCHERS.get(info.filename)
        if patch is not None:
            tree = _parse_xml(content, info.filename)
            patch(tree.getroot())
            content = _serialize(tree)
        patched.append((info, content))
    result = _write_package(patched)
    _validate(result)
    return result


def _pandoc_default_reference(pandoc_path: str) -> bytes:
    command = [pandoc_path, "--sandbox", "--print-default-data-file", "reference.docx"]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=PANDOC_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ReferenceDocError(f"pandoc timed out after {PANDOC_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ReferenceDocError(f"cannot run pandoc ({pandoc_path!r}): {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()[:500]
        raise ReferenceDocError(f"pandoc exited with status {completed.returncode}: {stderr}")
    return completed.stdout


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600 files; the reference is built as root but read by the app user.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def build_base_reference(pandoc_path: str, out_path: Path) -> Path:
    """Generate the restyled base ``reference.docx`` at ``out_path`` (written atomically).

    Raises :class:`ReferenceDocError` when pandoc fails or the result does not validate.
    """
    out_path = Path(out_path)
    data = restyle_reference(_pandoc_default_reference(pandoc_path))
    _write_atomically(out_path, data)
    return out_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.converter.docx_reference",
        description="Generate the Word reference document used for DOCX output.",
    )
    parser.add_argument("out_path", type=Path, metavar="OUT_PATH", help="output .docx file")
    parser.add_argument(
        "--pandoc",
        metavar="PATH",
        help="pandoc executable (default: PRELO_PANDOC_PATH or 'pandoc')",
    )
    args = parser.parse_args(argv)
    pandoc_path = args.pandoc or get_settings().pandoc_path
    try:
        path = build_base_reference(pandoc_path, args.out_path)
    except (ReferenceDocError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"reference document written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
