"""HTML document assembly for the PDF output: pandoc HTML5 + the document stylesheet."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from app.converter.pandoc import run_pandoc

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
TEMPLATE_PATH = ASSETS_DIR / "template.html"
STYLESHEET_PATH = ASSETS_DIR / "document.css"
CSS_PLACEHOLDER = "/*__DOCUMENT_CSS__*/"
HIGHLIGHT_STYLE = "pygments"
TOC_DEPTH = 3


def html_args(*, toc: bool, template: Path = TEMPLATE_PATH) -> list[str]:
    args = [
        "--from=json",
        "--to=html5",
        "--standalone",
        f"--template={template}",
        f"--syntax-highlighting={HIGHLIGHT_STYLE}",
        "--math-method=mathml",
        "--wrap=none",
    ]
    if toc:
        args += ["--toc", f"--toc-depth={TOC_DEPTH}"]
    return args


def inject_stylesheet(html: str, css: str) -> str:
    """Put ``css`` into the template's ``<style>/*__DOCUMENT_CSS__*/</style>`` slot.

    Only the exact ``<style>`` slot is replaced (document text can never contain an
    unescaped ``<style>``), falling back to appending a style element to ``<head>``.
    """
    safe_css = css.replace("</", "<\\/")  # cannot terminate the <style> element
    slot = f"<style>{CSS_PLACEHOLDER}</style>"
    if slot in html:
        return html.replace(slot, f"<style>{safe_css}</style>", 1)
    head_end = html.lower().find("</head>")
    style = f"<style>{safe_css}</style>\n"
    if head_end < 0:
        return style + html
    return html[:head_end] + style + html[head_end:]


@cache
def load_stylesheet(path: Path = STYLESHEET_PATH) -> str:
    return path.read_text(encoding="utf-8")


async def render_html(
    doc_json: bytes,
    *,
    pandoc_path: str,
    toc: bool,
    cwd: Path | None = None,
    template: Path = TEMPLATE_PATH,
    stylesheet: Path = STYLESHEET_PATH,
) -> str:
    """Render a pandoc JSON AST into a standalone, styled HTML document."""
    output = await run_pandoc(
        pandoc_path, html_args(toc=toc, template=template), stdin=doc_json, cwd=cwd
    )
    return inject_stylesheet(output.decode("utf-8"), load_stylesheet(stylesheet))
