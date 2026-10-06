"""Markdown conversion engine (pandoc + headless Chromium).

The orchestrator lives in :mod:`app.converter.service`.
"""

from app.converter.errors import (
    ConversionError,
    ConversionTimeoutError,
    InputTooLargeError,
    InvalidInputError,
    ServiceUnavailableError,
    UnsupportedFileError,
)
from app.converter.options import (
    ACCEPTED_EXTENSIONS,
    ConversionOptions,
    Margin,
    MermaidTheme,
    Orientation,
    OutputFormat,
    PageSize,
)

__all__ = [
    "ACCEPTED_EXTENSIONS",
    "ConversionError",
    "ConversionOptions",
    "ConversionTimeoutError",
    "InputTooLargeError",
    "InvalidInputError",
    "Margin",
    "MermaidTheme",
    "Orientation",
    "OutputFormat",
    "PageSize",
    "ServiceUnavailableError",
    "UnsupportedFileError",
]
