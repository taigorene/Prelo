"""Conversion options shared by the API layer and the conversion engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ACCEPTED_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".markdown",
    ".mdown",
    ".mkd",
    ".mkdn",
    ".txt",
    ".zip",
)

CSS_PX_PER_MM = 96 / 25.4
TWIPS_PER_MM = 1440 / 25.4


class OutputFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"

    @property
    def media_type(self) -> str:
        if self is OutputFormat.PDF:
            return "application/pdf"
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    @property
    def extension(self) -> str:
        return f".{self.value}"


class PageSize(StrEnum):
    A4 = "A4"
    LETTER = "Letter"
    LEGAL = "Legal"
    A3 = "A3"
    A5 = "A5"


class Orientation(StrEnum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class Margin(StrEnum):
    NARROW = "narrow"
    NORMAL = "normal"
    WIDE = "wide"


class MermaidTheme(StrEnum):
    DEFAULT = "default"
    NEUTRAL = "neutral"
    DARK = "dark"
    FOREST = "forest"


# Portrait (width, height) in millimetres.
PAGE_SIZES_MM: dict[PageSize, tuple[float, float]] = {
    PageSize.A4: (210.0, 297.0),
    PageSize.LETTER: (215.9, 279.4),
    PageSize.LEGAL: (215.9, 355.6),
    PageSize.A3: (297.0, 420.0),
    PageSize.A5: (148.0, 210.0),
}

MARGINS_MM: dict[Margin, float] = {
    Margin.NARROW: 12.7,
    Margin.NORMAL: 20.0,
    Margin.WIDE: 30.0,
}


def schema_description(text: str) -> ConfigDict:
    """Model config giving the OpenAPI schema a (pt-BR) description of its own, so the API
    docs do not fall back to the English class docstring."""
    return ConfigDict(json_schema_extra={"description": text})


class ConversionOptions(BaseModel):
    """Layout options. All fields are optional and have sensible defaults."""

    model_config = ConfigDict(
        extra="forbid",
        **schema_description(
            "Opções de layout. Todos os campos são opcionais e têm valores padrão."
        ),
    )

    page_size: PageSize = Field(PageSize.A4, description="Tamanho da página.")
    orientation: Orientation = Field(Orientation.PORTRAIT, description="Orientação da página.")
    margin: Margin = Field(
        Margin.NORMAL, description="Margens: narrow (12,7 mm), normal (20 mm) ou wide (30 mm)."
    )
    toc: bool = Field(False, description="Inclui um sumário (table of contents) no início.")
    page_numbers: bool = Field(True, description="Numera as páginas no rodapé.")
    mermaid_theme: MermaidTheme = Field(
        MermaidTheme.DEFAULT, description="Tema usado para renderizar diagramas Mermaid."
    )


@dataclass(frozen=True)
class PageGeometry:
    width_mm: float
    height_mm: float
    margin_mm: float
    landscape: bool

    @property
    def content_width_mm(self) -> float:
        return self.width_mm - 2 * self.margin_mm

    @property
    def content_height_mm(self) -> float:
        return self.height_mm - 2 * self.margin_mm

    @property
    def content_width_px(self) -> int:
        """Printable width in CSS pixels (96 dpi)."""
        return int(self.content_width_mm * CSS_PX_PER_MM)

    @staticmethod
    def mm_to_twips(value_mm: float) -> int:
        return round(value_mm * TWIPS_PER_MM)


def page_geometry(options: ConversionOptions) -> PageGeometry:
    width, height = PAGE_SIZES_MM[options.page_size]
    landscape = options.orientation is Orientation.LANDSCAPE
    if landscape:
        width, height = height, width
    return PageGeometry(
        width_mm=width,
        height_mm=height,
        margin_mm=MARGINS_MM[options.margin],
        landscape=landscape,
    )
