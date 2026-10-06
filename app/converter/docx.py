"""Word output: per-job reference document (page setup + footer) and the pandoc call.

pandoc takes page size, margins and footers from the reference document's final
``w:sectPr`` (and fits images to the text width derived from it), so each job gets
a copy of the base reference with that section patched before pandoc runs. After
pandoc, ``word/document.xml`` gets the touches pandoc cannot express (see
:func:`finish_document_xml`).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from collections.abc import Mapping
from pathlib import Path

from lxml import etree

from app.converter.html import ASSETS_DIR, HIGHLIGHT_STYLE, TOC_DEPTH
from app.converter.options import PageGeometry
from app.converter.pandoc import run_pandoc

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
FOOTER_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
FOOTER_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
DOCUMENT_PART = "word/document.xml"
DOCUMENT_RELS_PART = "word/_rels/document.xml.rels"
CONTENT_TYPES_PART = "[Content_Types].xml"
MEDIA_FILTER = ASSETS_DIR / "docx-media.lua"
# Images are passed to pandoc once each, in this metadata map, and referenced from the
# document as ``prelo-media:<key>`` (see docx-media.lua).
MEDIA_META_KEY = "prelo-media"
MEDIA_SCHEME = "prelo-media:"
# pandoc writes inline pictures without wrap distances; LibreOffice then applies its
# default (0.125 in) and shifts every picture right of the text margin.
_BARE_INLINE = b"<wp:inline>"
_FLUSH_INLINE = b'<wp:inline distT="0" distB="0" distL="0" distR="0">'
# Tables in the small font (paragraph style below) get narrower cell margins (twips).
SMALL_TABLE_STYLE_ID = "CompactSmall"
SMALL_TABLE_CELL_MARGIN = 57
FOOTER_COLOR = "6E7781"
FOOTER_HALF_POINTS = 18  # 9 pt

# Children of w:sectPr that must precede w:pgSz (CT_SectPr sequence order).
_BEFORE_PAGE_SIZE = frozenset(
    f"{{{W_NS}}}{name}"
    for name in ("headerReference", "footerReference", "footnotePr", "endnotePr", "type")
)
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)


def _w(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def patch_section_properties(
    document_xml: bytes, geometry: PageGeometry, *, footer_rel_id: str | None
) -> bytes:
    """Set page size/orientation/margins (and the default footer) on the final section."""
    root = etree.fromstring(document_xml, _PARSER)
    body = root.find(_w("body"))
    if body is None:
        raise ValueError("word/document.xml has no w:body")
    sections = body.findall(_w("sectPr"))
    section = sections[-1] if sections else etree.SubElement(body, _w("sectPr"))

    for name in ("pgSz", "pgMar"):
        for element in section.findall(_w(name)):
            section.remove(element)

    if footer_rel_id is not None:
        for element in section.findall(_w("footerReference")):
            if element.get(_w("type"), "default") == "default":
                section.remove(element)
        # SubElement reuses the namespace prefixes declared on the root element.
        reference = etree.SubElement(section, _w("footerReference"))
        reference.set(_w("type"), "default")
        reference.set(f"{{{R_NS}}}id", footer_rel_id)
        section.insert(0, reference)

    margin = PageGeometry.mm_to_twips(geometry.margin_mm)
    page_size = etree.SubElement(section, _w("pgSz"))
    page_size.set(_w("w"), str(PageGeometry.mm_to_twips(geometry.width_mm)))
    page_size.set(_w("h"), str(PageGeometry.mm_to_twips(geometry.height_mm)))
    if geometry.landscape:
        page_size.set(_w("orient"), "landscape")
    page_margins = etree.SubElement(section, _w("pgMar"))
    for side in ("top", "right", "bottom", "left"):
        page_margins.set(_w(side), str(margin))
    # Header/footer sit halfway into the top/bottom margins.
    page_margins.set(_w("header"), str(margin // 2))
    page_margins.set(_w("footer"), str(margin // 2))
    page_margins.set(_w("gutter"), "0")

    anchor = 0
    for index, child in enumerate(section):
        if child.tag in _BEFORE_PAGE_SIZE:
            anchor = index + 1
    section.insert(anchor, page_size)  # moves the appended elements into place
    section.insert(anchor + 1, page_margins)
    return _serialize(root)


def footer_xml() -> bytes:
    """Footer part with a centered "PAGE / NUMPAGES" line."""
    w = f"{{{W_NS}}}"
    root = etree.Element(f"{w}ftr", nsmap={"w": W_NS, "r": R_NS})
    paragraph = etree.SubElement(root, f"{w}p")
    properties = etree.SubElement(paragraph, f"{w}pPr")
    etree.SubElement(properties, f"{w}spacing", {f"{w}before": "0", f"{w}after": "0"})
    etree.SubElement(properties, f"{w}jc", {f"{w}val": "center"})

    def run(parent: etree._Element, text: str) -> None:
        element = etree.SubElement(parent, f"{w}r")
        run_properties = etree.SubElement(element, f"{w}rPr")
        etree.SubElement(run_properties, f"{w}color", {f"{w}val": FOOTER_COLOR})
        etree.SubElement(run_properties, f"{w}sz", {f"{w}val": str(FOOTER_HALF_POINTS)})
        etree.SubElement(run_properties, f"{w}szCs", {f"{w}val": str(FOOTER_HALF_POINTS)})
        text_element = etree.SubElement(element, f"{w}t")
        text_element.text = text
        text_element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    for index, field in enumerate(("PAGE", "NUMPAGES")):
        if index:
            run(paragraph, " / ")
        simple = etree.SubElement(paragraph, f"{w}fldSimple", {f"{w}instr": f" {field} "})
        run(simple, "1")
    return _serialize(root)


def _add_relationship(rels_xml: bytes, *, rel_type: str, target: str) -> tuple[bytes, str]:
    root = etree.fromstring(rels_xml, _PARSER)
    used = {element.get("Id") for element in root}
    index = 1
    while f"rIdMdFooter{index}" in used:
        index += 1
    rel_id = f"rIdMdFooter{index}"
    etree.SubElement(
        root, f"{{{PKG_REL_NS}}}Relationship", {"Id": rel_id, "Type": rel_type, "Target": target}
    )
    return _serialize(root), rel_id


def _add_override(content_types_xml: bytes, *, part_name: str, content_type: str) -> bytes:
    root = etree.fromstring(content_types_xml, _PARSER)
    if not any(element.get("PartName") == part_name for element in root):
        etree.SubElement(
            root, f"{{{CT_NS}}}Override", {"PartName": part_name, "ContentType": content_type}
        )
    return _serialize(root)


def build_job_reference(
    base_reference: Path, output: Path, geometry: PageGeometry, *, page_numbers: bool
) -> None:
    """Copy the base reference to ``output`` with this job's page setup and footer.

    Blocking (zip + XML work): run it in a worker thread.
    """
    with zipfile.ZipFile(base_reference) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    parts = {info.filename: data for info, data in entries}
    if DOCUMENT_PART not in parts:
        raise ValueError("reference document has no word/document.xml")

    replaced: dict[str, bytes] = {}
    added: dict[str, bytes] = {}
    footer_rel_id: str | None = None
    if page_numbers:
        index = 1
        while f"word/footer{index}.xml" in parts:
            index += 1
        footer_name = f"footer{index}.xml"
        replaced[DOCUMENT_RELS_PART], footer_rel_id = _add_relationship(
            parts[DOCUMENT_RELS_PART], rel_type=FOOTER_REL_TYPE, target=footer_name
        )
        replaced[CONTENT_TYPES_PART] = _add_override(
            parts[CONTENT_TYPES_PART],
            part_name=f"/word/{footer_name}",
            content_type=FOOTER_CONTENT_TYPE,
        )
        added[f"word/{footer_name}"] = footer_xml()
    replaced[DOCUMENT_PART] = patch_section_properties(
        parts[DOCUMENT_PART], geometry, footer_rel_id=footer_rel_id
    )

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in entries:
            target.writestr(info, replaced.get(info.filename, data))
        for name, data in added.items():
            target.writestr(name, data)


def docx_args(*, reference: Path, output: Path, toc: bool) -> list[str]:
    args = [
        "--from=json",
        "--to=docx",
        f"--reference-doc={reference}",
        f"--syntax-highlighting={HIGHLIGHT_STYLE}",
        f"--lua-filter={MEDIA_FILTER}",
        f"--output={output}",
    ]
    if toc:
        args += ["--toc", f"--toc-depth={TOC_DEPTH}"]
    return args


def finish_document_xml(document_xml: bytes, aligned_headers: Mapping[str, str]) -> bytes:
    """Post-process pandoc's ``word/document.xml``.

    * pictures get zero wrap distances (flush with the text margin in LibreOffice too);
    * headings in ``aligned_headers`` (identifier -> ``center``/``right``, from HTML
      ``align``) get their alignment, which pandoc's heading styles cannot carry.
      pandoc writes a heading's identifier as a bookmark just before its paragraph
      (see :func:`_bookmark_names`);
    * tables in the small font get narrower cell margins.
    """
    document_xml = document_xml.replace(_BARE_INLINE, _FLUSH_INLINE)
    small_tables = f'w:val="{SMALL_TABLE_STYLE_ID}"'.encode() in document_xml
    if not aligned_headers and not small_tables:
        return document_xml
    root = etree.fromstring(document_xml, _PARSER)
    if small_tables:
        _narrow_small_table_margins(root)
    names = {name: align for key, align in aligned_headers.items() for name in _bookmark_names(key)}
    for bookmark in root.iter(_w("bookmarkStart")):
        align = names.get(bookmark.get(_w("name"), ""))
        parent = bookmark.getparent()
        paragraph = parent if parent is not None and parent.tag == _w("p") else bookmark.getnext()
        if align is None or paragraph is None or paragraph.tag != _w("p"):
            continue
        properties = paragraph.find(_w("pPr"))
        if properties is None:
            properties = etree.SubElement(paragraph, _w("pPr"))
            paragraph.insert(0, properties)
        for element in properties.findall(_w("jc")):
            properties.remove(element)
        justification = etree.SubElement(properties, _w("jc"))
        justification.set(_w("val"), align)
        _place_justification(properties, justification)
    return _serialize(root)


def _narrow_small_table_margins(root: etree._Element) -> None:
    for table in root.iter(_w("tbl")):
        cells = table.findall(f"{_w('tr')}/{_w('tc')}")
        styles = {
            style.get(_w("val"))
            for cell in cells
            for style in cell.iterfind(f"{_w('p')}/{_w('pPr')}/{_w('pStyle')}")
        }
        properties = table.find(_w("tblPr"))
        if SMALL_TABLE_STYLE_ID not in styles or properties is None:
            continue
        for element in properties.findall(_w("tblCellMar")):
            properties.remove(element)
        margins = etree.SubElement(properties, _w("tblCellMar"))
        for side in ("left", "right"):
            etree.SubElement(
                margins, _w(side), {_w("w"): str(SMALL_TABLE_CELL_MARGIN), _w("type"): "dxa"}
            )
        look = properties.find(_w("tblLook"))
        if look is not None:  # CT_TblPr order: tblCellMar comes before tblLook
            look.addprevious(margins)


def _bookmark_names(identifier: str) -> tuple[str, ...]:
    """Names pandoc may give the bookmark of a heading: the identifier itself, or (hidden
    and within Word's 40-character limit, as pandoc 3 does) "_" + SHA-1 minus one digit."""
    digest = hashlib.sha1(identifier.encode("utf-8")).hexdigest()
    return identifier, f"_{identifier}", f"_{digest[1:]}"


# Children of w:pPr that come after w:jc (CT_PPrBase sequence order).
_AFTER_JC = frozenset(
    _w(name)
    for name in (
        "textDirection",
        "textAlignment",
        "textboxTightWrap",
        "outlineLvl",
        "divId",
        "cnfStyle",
        "rPr",
        "sectPr",
        "pPrChange",
    )
)


def _place_justification(properties: etree._Element, justification: etree._Element) -> None:
    for index, child in enumerate(properties):
        if child.tag in _AFTER_JC:
            properties.insert(index, justification)
            return


def _finish_package(path: Path, aligned_headers: Mapping[str, str]) -> bytes:
    """Rewrite ``word/document.xml`` inside the DOCX at ``path`` (blocking)."""
    output = io.BytesIO()
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == DOCUMENT_PART:
                data = finish_document_xml(data, aligned_headers)
            target.writestr(info, data)
    return output.getvalue()


async def render_docx(
    doc_json: bytes,
    *,
    pandoc_path: str,
    reference: Path,
    workdir: Path,
    toc: bool,
    aligned_headers: Mapping[str, str] | None = None,
) -> bytes:
    """Write the DOCX with pandoc (sandboxed) and return its bytes."""
    output = workdir / "output.docx"
    await run_pandoc(
        pandoc_path,
        docx_args(reference=reference, output=output, toc=toc),
        stdin=doc_json,
        cwd=workdir,
    )
    return await asyncio.to_thread(_finish_package, output, aligned_headers or {})
