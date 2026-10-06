"""HTTP endpoints: Markdown conversion (file upload or JSON text) and service status."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from collections.abc import Sequence
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, Form, Path, Request, Response, UploadFile

from app import __version__
from app.api.errors import InternalError, upload_limit_message
from app.api.schemas import (
    EngineVersions,
    ErrorResponse,
    HealthChecks,
    HealthResponse,
    InfoLimits,
    InfoOptions,
    InfoResponse,
    TextConversionRequest,
    ValidationErrorResponse,
)
from app.config import Settings
from app.converter.errors import ConversionError, InputTooLargeError, ServiceUnavailableError
from app.converter.options import (
    ACCEPTED_EXTENSIONS,
    ConversionOptions,
    Margin,
    MermaidTheme,
    Orientation,
    OutputFormat,
    PageSize,
)
from app.converter.service import ConversionResult, ConversionService

logger = logging.getLogger("prelo.api")

API_PREFIX = "/api/v1"
CONVERT_PATH_PREFIX = f"{API_PREFIX}/convert"
TAG_CONVERSION = "Conversão"
TAG_SYSTEM = "Sistema"

UPLOAD_CHUNK_BYTES = 1024 * 1024
WARNINGS_HEADER_MAX_BYTES = 4096
WARNING_MAX_CHARS = 500
STATUS_TIMEOUT_SECONDS = 4.0
DEFAULT_STEM = "documento"
EXPOSED_HEADERS = (
    "Content-Disposition",
    "X-Conversion-Warnings",
    "X-Conversion-Warnings-Count",
    "X-Conversion-Time-Ms",
)
# How often a running conversion checks whether its client is still connected.
DISCONNECT_POLL_SECONDS = 0.5

_DEFAULT_OPTIONS = ConversionOptions()
_STRIPPED_SUFFIXES = tuple(
    sorted({*ACCEPTED_EXTENSIONS, *(fmt.extension for fmt in OutputFormat)}, key=len, reverse=True)
)
_UNSAFE_ASCII = re.compile(r"[^A-Za-z0-9 ._()\[\]-]+")
# Browsers escape these in multipart file names (WHATWG): ", CR and LF.
_FORM_ESCAPES = re.compile(r"%(22|0D|0A)", re.IGNORECASE)
_FORM_UNESCAPED = {"22": '"', "0d": "", "0a": ""}

router = APIRouter(prefix=API_PREFIX)


# --------------------------------------------------------------------------- dependencies


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


def conversion_service(request: Request) -> ConversionService:
    service: ConversionService | None = getattr(request.app.state, "converter", None)
    if service is None:
        raise ServiceUnavailableError()
    return service


SettingsDep = Annotated[Settings, Depends(app_settings)]
ConverterDep = Annotated[ConversionService, Depends(conversion_service)]
FormatPath = Annotated[
    OutputFormat,
    Path(
        alias="format",
        description="Formato de saída: `pdf` (PDF) ou `docx` (Microsoft Word).",
    ),
]


def _option_doc(name: str) -> str:
    return ConversionOptions.model_fields[name].description or ""


# --------------------------------------------------------------------------- helpers


def _to_ascii(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _UNSAFE_ASCII.sub("_", plain).strip(" _")


def content_disposition(filename: str) -> str:
    """``attachment`` header with an ASCII fallback and the UTF-8 name (RFC 6266 / RFC 5987)."""
    name = "".join(ch for ch in filename if ch.isprintable()).replace("/", "_").replace("\\", "_")
    name = name.strip() or DEFAULT_STEM
    stem, suffix = os.path.splitext(name)
    fallback = (_to_ascii(stem) or DEFAULT_STEM) + _to_ascii(suffix)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def warnings_header(
    warnings: Sequence[str], total: int | None = None, max_bytes: int = WARNINGS_HEADER_MAX_BYTES
) -> str:
    """Encode warnings as an ASCII JSON array no longer than ``max_bytes``.

    ``total`` is the number of warnings of the conversion (``warnings`` may hold only the
    first ones); when some are left out, a last item says how many.
    """
    total = max(total or 0, len(warnings))
    reserve = 80  # room for the trailing "omitted" note
    items: list[str] = []
    for warning in warnings:
        text = warning if len(warning) <= WARNING_MAX_CHARS else warning[:WARNING_MAX_CHARS] + "…"
        if len(json.dumps([*items, text])) > max_bytes - reserve:
            break
        items.append(text)
    omitted = total - len(items)
    if omitted:
        items.append(
            f"… e mais {omitted} aviso(s) omitido(s)." if items else f"{omitted} aviso(s)."
        )
    return json.dumps(items)


def _upload_filename(name: str | None) -> str:
    # Browsers send '"' as %22 (and drop CR/LF) inside the multipart file name.
    name = _FORM_ESCAPES.sub(lambda match: _FORM_UNESCAPED[match[1].lower()], name or "")
    # Some clients send full paths (``C:\\fakepath\\x.md``); keep only the base name.
    return name.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _markdown_filename(name: str) -> str:
    stem = _upload_filename(name)
    lowered = stem.lower()
    for suffix in _STRIPPED_SUFFIXES:
        if lowered.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f"{stem.strip() or DEFAULT_STEM}.md"


def _ensure_within_limit(size: int, settings: Settings) -> None:
    if size > settings.max_upload_bytes:
        raise InputTooLargeError(upload_limit_message(settings.max_upload_mb))


async def _read_upload(file: UploadFile, settings: Settings) -> bytes:
    """Read the upload in chunks, stopping as soon as it exceeds the size limit."""
    if file.size is not None:
        _ensure_within_limit(file.size, settings)
    buffer = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        buffer.extend(chunk)
        _ensure_within_limit(len(buffer), settings)
    return bytes(buffer)


def _file_response(result: ConversionResult) -> Response:
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={
            "Content-Disposition": content_disposition(result.filename),
            "X-Conversion-Warnings": warnings_header(result.warnings, result.warnings_total),
            "X-Conversion-Warnings-Count": str(result.warnings_total),
            "X-Conversion-Time-Ms": str(result.duration_ms),
            "Cache-Control": "no-store",
        },
    )


async def _wait_for_disconnect(request: Request) -> None:
    """Return once the client has gone away (the request body was already read)."""
    try:
        while not await request.is_disconnected():
            await asyncio.sleep(DISCONNECT_POLL_SECONDS)
    except Exception:
        logger.debug("Cannot watch the client connection", exc_info=True)
        await asyncio.Event().wait()  # unknown: never report a disconnect


async def _convert(
    service: ConversionService,
    request: Request,
    *,
    data: bytes,
    filename: str,
    fmt: OutputFormat,
    options: ConversionOptions,
) -> Response:
    """Run the conversion, cancelling it (and freeing its slot) if the client disconnects."""
    conversion = asyncio.ensure_future(
        service.convert(data=data, filename=filename, fmt=fmt, options=options)
    )
    watcher = asyncio.ensure_future(_wait_for_disconnect(request))
    try:
        await asyncio.wait({conversion, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (conversion, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(conversion, watcher, return_exceptions=True)
    if conversion.cancelled():
        logger.info("Client disconnected; %s conversion cancelled", fmt.value)
        return Response(status_code=499)  # nobody is listening any more
    try:
        result = conversion.result()
    except ConversionError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error while converting a document to %s", fmt.value)
        raise InternalError() from exc
    return _file_response(result)


# --------------------------------------------------------------------------- OpenAPI docs

_BINARY_SCHEMA = {"schema": {"type": "string", "format": "binary"}}
_ACCEPTED_LIST = ", ".join(f"`{ext}`" for ext in ACCEPTED_EXTENSIONS)

_FILE_HEADERS: dict[str, Any] = {
    "Content-Disposition": {
        "description": (
            "Nome do arquivo gerado: o nome original com a nova extensão. Traz `filename` "
            "(ASCII) e `filename*` (UTF-8, RFC 5987) para nomes acentuados."
        ),
        "schema": {"type": "string"},
        "example": "attachment; filename=\"relatorio.pdf\"; filename*=UTF-8''relat%C3%B3rio.pdf",
    },
    "X-Conversion-Warnings": {
        "description": (
            "Lista JSON (codificada em ASCII, até 4 KB) com avisos da conversão — por exemplo, "
            "imagem não encontrada ou erro de sintaxe em um diagrama Mermaid. `[]` quando não "
            "há avisos. Se nem todos couberem, o último item informa quantos foram omitidos."
        ),
        "schema": {"type": "string"},
        "example": '["Imagem n\\u00e3o encontrada: img/logo.png"]',
    },
    "X-Conversion-Warnings-Count": {
        "description": "Número total de avisos da conversão (inclusive os omitidos da lista).",
        "schema": {"type": "integer"},
        "example": 1,
    },
    "X-Conversion-Time-Ms": {
        "description": "Duração da conversão, em milissegundos.",
        "schema": {"type": "integer"},
        "example": 842,
    },
}


def _error_doc(description: str, code: str, detail: str) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": {"example": {"detail": detail, "code": code}}},
    }


# Example of the 413 detail; the OpenAPI schema replaces it with this instance's limit.
UPLOAD_LIMIT_EXAMPLE_MB = 20
_PAGE_SIZE_ERROR = "valor inválido (aceitos: 'A4', 'Letter', 'Legal', 'A3' ou 'A5')"


def _validation_doc(loc: list[str]) -> dict[str, Any]:
    field = ".".join(loc[1:])
    return {
        "model": ValidationErrorResponse,
        "description": "Parâmetros inválidos (por exemplo, um valor fora das opções aceitas).",
        "content": {
            "application/json": {
                "example": {
                    "detail": f"Requisição inválida — {field}: {_PAGE_SIZE_ERROR}.",
                    "code": "validation_error",
                    "errors": [{"loc": loc, "msg": _PAGE_SIZE_ERROR, "type": "enum"}],
                }
            }
        },
    }


_COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        **_error_doc(
            "Entrada inválida: documento vazio, conteúdo binário ou `.zip` inválido "
            "(corrompido, protegido por senha, com caminhos inseguros ou sem nenhum arquivo "
            "Markdown) — código `invalid_input`; ou corpo da requisição ilegível (por exemplo, "
            "`multipart/form-data` malformado) — código `bad_request`.",
            "invalid_input",
            "O arquivo Markdown está vazio.",
        ),
    },
    413: _error_doc(
        "O documento excede o tamanho máximo permitido (veja `GET /api/v1/info`).",
        "input_too_large",
        upload_limit_message(UPLOAD_LIMIT_EXAMPLE_MB),
    ),
    500: _error_doc(
        "Erro inesperado durante a conversão.",
        InternalError.code,
        InternalError.default_message,
    ),
    503: _error_doc(
        "Serviço de conversão indisponível no momento (por exemplo, navegador reiniciando). "
        "Tente novamente em instantes.",
        ServiceUnavailableError.code,
        ServiceUnavailableError.default_message,
    ),
    504: _error_doc(
        "A conversão excedeu o tempo limite (documento muito grande ou diagramas complexos).",
        "timeout",
        "A conversão excedeu o tempo limite.",
    ),
}


def _conversion_responses(*, file_upload: bool) -> dict[int | str, dict[str, Any]]:
    loc = ["body", "page_size"] if file_upload else ["body", "options", "page_size"]
    responses: dict[int | str, dict[str, Any]] = {
        200: {
            "description": (
                "Documento convertido, enviado como download: `application/pdf` quando "
                "`format=pdf` ou o tipo do Word (.docx) quando `format=docx`."
            ),
            "content": {
                OutputFormat.PDF.media_type: _BINARY_SCHEMA,
                OutputFormat.DOCX.media_type: _BINARY_SCHEMA,
            },
            "headers": _FILE_HEADERS,
        },
        **_COMMON_ERROR_RESPONSES,
        422: _validation_doc(loc),
    }
    if file_upload:
        responses[415] = _error_doc(
            f"Extensão de arquivo não suportada (aceitas: {_ACCEPTED_LIST}).",
            "unsupported_file",
            "Tipo de arquivo não suportado.",
        )
    return dict(sorted(responses.items(), key=lambda item: int(item[0])))


def _options_doc(where: str) -> str:
    return f"""
**Opções** ({where}, todas opcionais):

| Campo | Valores | Padrão |
| --- | --- | --- |
| `page_size` | `A4`, `Letter`, `Legal`, `A3`, `A5` | `A4` |
| `orientation` | `portrait` (retrato), `landscape` (paisagem) | `portrait` |
| `margin` | `narrow` (12,7 mm), `normal` (20 mm), `wide` (30 mm) | `normal` |
| `toc` | `true` / `false` — inclui um sumário | `false` |
| `page_numbers` | `true` / `false` — numera as páginas no rodapé | `true` |
| `mermaid_theme` | `default`, `neutral`, `dark`, `forest` | `default` |

**Resposta**: o arquivo convertido (download), com os cabeçalhos `Content-Disposition`,
`X-Conversion-Warnings` (avisos em JSON), `X-Conversion-Warnings-Count` (total de avisos) e
`X-Conversion-Time-Ms`. Avisos não impedem a conversão: um diagrama Mermaid inválido, por
exemplo, é mantido como bloco de código.
"""


_CONVERT_FILE_DOC = f"""
Envie um arquivo Markdown via `multipart/form-data` (campo `file`) e receba o documento
convertido em **PDF** ou **Word (.docx)**.

- Arquivos aceitos: {_ACCEPTED_LIST}.
- Use um **`.zip`** para incluir imagens locais: o Markdown principal (de preferência
  `README.md` ou `index.md`) e as imagens referenciadas por caminho relativo.
- A codificação do texto é detectada automaticamente (UTF-8, UTF-16 ou Latin-1).

```bash
curl -f -o relatorio.pdf -F "file=@relatorio.md" -F "toc=true" \\
  http://localhost:8000/api/v1/convert/pdf
```
{_options_doc("campos do formulário")}"""

_CONVERT_TEXT_DOC = f"""
Envie o conteúdo Markdown dentro de um JSON e receba o documento convertido em **PDF** ou
**Word (.docx)**. Ideal para integrações que já têm o texto em memória. Imagens só podem ser
remotas (URL) ou embutidas (`data:`); para imagens locais, envie um `.zip` em
`POST /api/v1/convert/{{format}}`.

```bash
curl -f -o ola.docx -H "Content-Type: application/json" \\
  -d '{{"markdown": "# Olá\\n\\nTexto em **negrito**.", "filename": "ola"}}' \\
  http://localhost:8000/api/v1/convert/docx/text
```
{_options_doc("objeto `options`")}"""

_TEXT_EXAMPLES: dict[str, Any] = {
    "relatorio": {
        "summary": "Relatório com tabela, código e diagrama Mermaid",
        "value": {
            "markdown": (
                "# Relatório de vendas\n\n"
                "Resumo do **3º trimestre** com tabela, código e diagrama.\n\n"
                "## Resultados\n\n"
                "| Região | Vendas | Variação |\n"
                "| :--- | ---: | :---: |\n"
                "| Sul | R$ 120.000 | +8% |\n"
                "| Nordeste | R$ 98.500 | +12% |\n\n"
                "```python\n"
                "def total(vendas: list[float]) -> float:\n"
                "    return sum(vendas)\n"
                "```\n\n"
                "## Fluxo de aprovação\n\n"
                "```mermaid\n"
                "flowchart LR\n"
                "    A[Pedido] --> B{Aprovado?}\n"
                "    B -- Sim --> C[Faturamento]\n"
                "    B -- Não --> D[Revisão]\n"
                "```\n"
            ),
            "filename": "relatório-de-vendas",
            "options": {
                "page_size": "A4",
                "orientation": "portrait",
                "margin": "normal",
                "toc": False,
                "page_numbers": True,
                "mermaid_theme": "default",
            },
        },
    },
    "minimo": {
        "summary": "Somente o texto (opções padrão)",
        "value": {"markdown": "# Olá, mundo!\n\nTexto com *ênfase* e `código`."},
    },
}


# --------------------------------------------------------------------------- endpoints


@router.post(
    "/convert/{format}",
    tags=[TAG_CONVERSION],
    summary="Converter um arquivo Markdown",
    description=_CONVERT_FILE_DOC,
    operation_id="convertFile",
    response_class=Response,
    responses=_conversion_responses(file_upload=True),
)
async def convert_file(
    request: Request,
    fmt: FormatPath,
    file: Annotated[
        UploadFile,
        File(description="Arquivo Markdown (`.md`, `.markdown`, `.txt`...) ou `.zip`."),
    ],
    service: ConverterDep,
    settings: SettingsDep,
    page_size: Annotated[PageSize, Form(description=_option_doc("page_size"))] = (
        _DEFAULT_OPTIONS.page_size
    ),
    orientation: Annotated[Orientation, Form(description=_option_doc("orientation"))] = (
        _DEFAULT_OPTIONS.orientation
    ),
    margin: Annotated[Margin, Form(description=_option_doc("margin"))] = _DEFAULT_OPTIONS.margin,
    toc: Annotated[bool, Form(description=_option_doc("toc"))] = _DEFAULT_OPTIONS.toc,
    page_numbers: Annotated[bool, Form(description=_option_doc("page_numbers"))] = (
        _DEFAULT_OPTIONS.page_numbers
    ),
    mermaid_theme: Annotated[MermaidTheme, Form(description=_option_doc("mermaid_theme"))] = (
        _DEFAULT_OPTIONS.mermaid_theme
    ),
) -> Response:
    data = await _read_upload(file, settings)
    options = ConversionOptions(
        page_size=page_size,
        orientation=orientation,
        margin=margin,
        toc=toc,
        page_numbers=page_numbers,
        mermaid_theme=mermaid_theme,
    )
    return await _convert(
        service,
        request,
        data=data,
        filename=_upload_filename(file.filename),
        fmt=fmt,
        options=options,
    )


@router.post(
    "/convert/{format}/text",
    tags=[TAG_CONVERSION],
    summary="Converter texto Markdown (JSON)",
    description=_CONVERT_TEXT_DOC,
    operation_id="convertText",
    response_class=Response,
    responses=_conversion_responses(file_upload=False),
)
async def convert_text(
    request: Request,
    fmt: FormatPath,
    payload: Annotated[TextConversionRequest, Body(openapi_examples=_TEXT_EXAMPLES)],
    service: ConverterDep,
    settings: SettingsDep,
) -> Response:
    data = payload.markdown.encode("utf-8")
    _ensure_within_limit(len(data), settings)
    return await _convert(
        service,
        request,
        data=data,
        filename=_markdown_filename(payload.filename),
        fmt=fmt,
        options=payload.options,
    )


@router.get(
    "/health",
    tags=[TAG_SYSTEM],
    summary="Verificar a saúde do serviço",
    description=(
        "Indica se o serviço consegue converter documentos agora (pandoc e Chromium "
        "disponíveis). Retorna **200** quando está tudo certo e **503** caso contrário — "
        "adequado para *health checks* de contêineres e balanceadores."
    ),
    operation_id="getHealth",
    response_model=HealthResponse,
    responses={
        200: {"description": "Serviço disponível: pandoc e Chromium prontos."},
        503: {
            "model": HealthResponse,
            "description": "Alguma dependência do mecanismo de conversão está indisponível.",
            "content": {
                "application/json": {
                    "example": {
                        "status": "unavailable",
                        "checks": {"pandoc": True, "browser": False},
                    }
                }
            },
        },
    },
)
async def health(request: Request, response: Response) -> HealthResponse:
    checks: dict[str, bool] = {"pandoc": False, "browser": False}
    service: ConversionService | None = getattr(request.app.state, "converter", None)
    if service is not None:
        try:
            async with asyncio.timeout(STATUS_TIMEOUT_SECONDS):
                checks.update(await service.health())
        except Exception:
            logger.warning("Health check failed", exc_info=True)
    healthy = all(checks.values())
    response.status_code = 200 if healthy else 503
    return HealthResponse(
        status="ok" if healthy else "unavailable", checks=HealthChecks.model_validate(checks)
    )


@router.get(
    "/info",
    tags=[TAG_SYSTEM],
    summary="Consultar versão, limites e opções",
    description=(
        "Retorna a versão da aplicação, os formatos e extensões aceitos, os limites desta "
        "instância (tamanho máximo de upload, tempo limite...), os valores padrão e aceitos "
        "de cada opção e as versões dos mecanismos de conversão."
    ),
    operation_id="getInfo",
    response_model=InfoResponse,
    responses={200: {"description": "Metadados do serviço."}},
)
async def info(request: Request, settings: SettingsDep) -> InfoResponse:
    versions: dict[str, str] = {}
    service: ConversionService | None = getattr(request.app.state, "converter", None)
    if service is not None:
        try:
            async with asyncio.timeout(STATUS_TIMEOUT_SECONDS):
                versions = await service.versions()
        except Exception:
            logger.warning("Could not read engine versions", exc_info=True)
    return InfoResponse(
        name="Prelo",
        version=__version__,
        formats=list(OutputFormat),
        accepted_extensions=list(ACCEPTED_EXTENSIONS),
        limits=InfoLimits(
            max_upload_mb=settings.max_upload_mb,
            max_concurrent_jobs=settings.max_concurrent_jobs,
            conversion_timeout_seconds=settings.conversion_timeout,
            allow_remote_images=settings.allow_remote_images,
        ),
        defaults=_DEFAULT_OPTIONS,
        options=InfoOptions(
            page_size=list(PageSize),
            orientation=list(Orientation),
            margin=list(Margin),
            mermaid_theme=list(MermaidTheme),
        ),
        engines=EngineVersions(
            pandoc=versions.get("pandoc"),
            # Only the major version: the exact build would help target browser bugs.
            chromium=(versions.get("chromium") or "").split(".", 1)[0] or None,
            mermaid=versions.get("mermaid"),
        ),
    )


# HEAD for the status endpoints (uptime monitors often use it); hidden from the schema so
# each operation stays documented once.
router.add_api_route("/health", health, methods=["HEAD"], include_in_schema=False)
router.add_api_route("/info", info, methods=["HEAD"], include_in_schema=False)
