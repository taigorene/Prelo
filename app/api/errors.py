"""HTTP error handling: every error response follows the ``ErrorResponse`` model."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from http.client import responses as http_reasons
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.converter.errors import ConversionError

logger = logging.getLogger("prelo.api")

RETRY_AFTER_SECONDS = 10
MAX_SUMMARIZED_ERRORS = 3

# Status codes raised by the framework itself (routing, body parsing, static files).
_HTTP_ERRORS: dict[int, tuple[str, str]] = {
    400: (
        "bad_request",
        "Não foi possível ler a requisição. Verifique o formato do corpo enviado.",
    ),
    404: ("not_found", "Recurso não encontrado."),
    405: ("method_not_allowed", "Método HTTP não permitido para este recurso."),
    413: ("input_too_large", "O conteúdo enviado é grande demais."),
    415: ("unsupported_media_type", "Tipo de conteúdo não suportado."),
}

# pt-BR wording for the most common pydantic error types; placeholders come from ``ctx``.
_VALIDATION_MESSAGES: dict[str, str] = {
    "missing": "campo obrigatório",
    "enum": "valor inválido (aceitos: {expected})",
    "literal_error": "valor inválido (aceitos: {expected})",
    "bool_parsing": "deve ser verdadeiro ou falso (true/false)",
    "bool_type": "deve ser verdadeiro ou falso (true/false)",
    "string_type": "deve ser um texto",
    "string_unicode": "contém caracteres Unicode inválidos",
    "string_too_short": "deve ter pelo menos {min_length} caractere(s)",
    "string_too_long": "deve ter no máximo {max_length} caracteres",
    "extra_forbidden": "campo não permitido",
    "model_type": "deve ser um objeto JSON",
    "model_attributes_type": "deve ser um objeto JSON",
    "dict_type": "deve ser um objeto JSON",
    "json_invalid": "JSON inválido",
    "json_type": "deve ser um JSON válido",
}

_LOCATION_ROOTS = {"body", "query", "path", "header", "cookie"}


class InternalError(ConversionError):
    """Unexpected failure inside the conversion engine (already logged)."""

    status_code = 500
    code = "internal_error"
    default_message = "Erro interno ao converter o documento."


def format_megabytes(value: float) -> str:
    """Format a size in MB the Brazilian way (``20``, ``0,5``)."""
    return f"{value:g}".replace(".", ",")


def upload_limit_message(max_upload_mb: float) -> str:
    return f"O documento excede o limite de {format_megabytes(max_upload_mb)} MB."


def error_response(
    status_code: int,
    detail: str,
    code: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, **extra},
        headers=dict(headers) if headers else None,
    )


def _field_label(loc: Sequence[str | int], error_type: str) -> str:
    parts = list(loc)
    origin = parts.pop(0) if parts and parts[0] in _LOCATION_ROOTS else None
    if error_type == "json_invalid" or not parts:
        return "corpo da requisição" if origin in (None, "body") else str(origin)
    label = ""
    for part in parts:
        label += f"[{part}]" if isinstance(part, int) else (f".{part}" if label else str(part))
    return label


def _validation_message(error: Mapping[str, Any]) -> str:
    template = _VALIDATION_MESSAGES.get(error.get("type", ""))
    if template is None:
        return str(error.get("msg", "valor inválido"))
    ctx = {key: str(value).replace(" or ", " ou ") for key, value in error.get("ctx", {}).items()}
    try:
        return template.format(**ctx)
    except (KeyError, IndexError):
        return str(error.get("msg", "valor inválido"))


def _validation_items(exc: RequestValidationError) -> list[dict[str, Any]]:
    # Only ``loc``/``msg``/``type`` are returned: ``input`` may echo large or sensitive content.
    return [
        {
            "loc": [part if isinstance(part, int) else str(part) for part in error.get("loc", ())],
            "msg": _validation_message(error),
            "type": str(error.get("type", "value_error")),
        }
        for error in exc.errors()
    ]


def _validation_summary(items: Sequence[Mapping[str, Any]]) -> str:
    parts = [
        f"{_field_label(item['loc'], item['type'])}: {item['msg']}"
        for item in items[:MAX_SUMMARIZED_ERRORS]
    ]
    summary = "Requisição inválida — " + "; ".join(parts)
    if len(items) > MAX_SUMMARIZED_ERRORS:
        summary += f" (e mais {len(items) - MAX_SUMMARIZED_ERRORS} erro(s))"
    return summary + "."


async def _handle_conversion_error(request: Request, exc: ConversionError) -> JSONResponse:
    # InternalError is raised after its traceback was logged; don't log it twice.
    if exc.status_code >= 500 and not isinstance(exc, InternalError):
        logger.warning(
            "Conversion failed on %s %s: [%s] %s",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
        )
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS)} if exc.status_code == 503 else None
    return error_response(exc.status_code, exc.message, exc.code, headers=headers)


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    items = _validation_items(exc)
    return error_response(422, _validation_summary(items), "validation_error", errors=items)


async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    known = _HTTP_ERRORS.get(exc.status_code)
    if known is not None:
        code, detail = known
    else:
        code = f"http_{exc.status_code}"
        detail = (
            exc.detail if isinstance(exc.detail, str) else http_reasons.get(exc.status_code, "Erro")
        )
    return error_response(exc.status_code, detail, code, headers=exc.headers)


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(500, InternalError.default_message, InternalError.code)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ConversionError, _handle_conversion_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
