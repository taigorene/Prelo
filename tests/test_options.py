"""Conversion options and page geometry (pure functions, no engine needed)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.converter.options import (
    ConversionOptions,
    Margin,
    Orientation,
    OutputFormat,
    PageSize,
    page_geometry,
)


def test_defaults() -> None:
    options = ConversionOptions()
    assert options.page_size is PageSize.A4
    assert options.orientation is Orientation.PORTRAIT
    assert options.margin is Margin.NORMAL
    assert options.toc is False
    assert options.page_numbers is True


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ConversionOptions(cor="azul")  # type: ignore[call-arg]


def test_a4_portrait_geometry() -> None:
    geometry = page_geometry(ConversionOptions())
    assert (geometry.width_mm, geometry.height_mm) == (210.0, 297.0)
    assert geometry.margin_mm == 20.0
    assert not geometry.landscape
    assert geometry.content_width_mm == pytest.approx(170.0)
    assert geometry.content_width_px == 642  # 170 mm at 96 dpi


@pytest.mark.parametrize("size", list(PageSize))
def test_landscape_swaps_dimensions(size: PageSize) -> None:
    portrait = page_geometry(ConversionOptions(page_size=size))
    landscape = page_geometry(ConversionOptions(page_size=size, orientation="landscape"))
    assert landscape.landscape
    assert (landscape.width_mm, landscape.height_mm) == (portrait.height_mm, portrait.width_mm)
    assert landscape.content_width_px > portrait.content_width_px


@pytest.mark.parametrize(
    ("margin", "expected_mm"), [("narrow", 12.7), ("normal", 20.0), ("wide", 30.0)]
)
def test_margins(margin: str, expected_mm: float) -> None:
    assert page_geometry(ConversionOptions(margin=margin)).margin_mm == expected_mm


def test_twips_conversion() -> None:
    assert page_geometry(ConversionOptions()).mm_to_twips(25.4) == 1440


def test_output_formats() -> None:
    assert OutputFormat.PDF.media_type == "application/pdf"
    assert OutputFormat.DOCX.media_type.endswith("wordprocessingml.document")
    assert OutputFormat.DOCX.extension == ".docx"


def test_settings_derived_values() -> None:
    settings = Settings(max_upload_mb=2, cors_origins=" https://a.com , ,https://b.com ")
    assert settings.max_upload_bytes == 2 * 1024 * 1024
    assert settings.cors_origin_list == ["https://a.com", "https://b.com"]
    assert settings.mermaid_js_path.name == "mermaid.min.js"
