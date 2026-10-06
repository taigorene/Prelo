"""Application factory: API routes, documentation pages, middleware and the web UI."""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.routing import Match, Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__
from app.api.errors import (
    error_response,
    format_megabytes,
    install_error_handlers,
    upload_limit_message,
)
from app.api.routes import (
    CONVERT_PATH_PREFIX,
    EXPOSED_HEADERS,
    TAG_CONVERSION,
    TAG_SYSTEM,
    router,
)
from app.config import MB, Settings, get_settings
from app.converter.service import ConversionService

logger = logging.getLogger("prelo")

ServiceFactory = Callable[[Settings], ConversionService]

STATIC_DIR = Path(__file__).resolve().parent / "static"
DOCS_URL = "/docs"
REDOC_URL = "/redoc"
API_ROOT = "/api"  # every API version lives below it (never served by the web UI)
SWAGGER_ASSETS_URL = "/static/swagger-ui"
REDOC_ASSETS_URL = "/static/redoc"
MULTIPART_OVERHEAD_BYTES = MB
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
FAVICON_PATH = "/favicon.svg"
LOGO_PATH = "/brand/logo-light.svg"
# Brand bar above Swagger UI, aligned with its 1460px wrapper (the docs CSP allows inline CSS).
SWAGGER_BRAND_STYLE = (
    "<style>.prelo-brand{box-sizing:border-box;max-width:1460px;margin:0 auto;"
    "padding:24px 20px 0}.prelo-brand img{display:block;width:auto;height:40px}</style>"
)
# ReDoc reads its theme from a JSON attribute; this keeps the x-logo in line with the menu.
REDOC_THEME = '{"logo":{"maxWidth":"180px","gutter":"24px 20px 12px"}}'
# The docs pages are in Portuguese (screen readers pick the voice from ``lang``).
HTML_LANG = '<html lang="pt-BR">'
REDOC_NOSCRIPT = (
    "ReDoc requires Javascript to function. Please enable it to browse the documentation."
)
REDOC_NOSCRIPT_PT = "O ReDoc precisa de JavaScript. Ative-o para navegar pela documentação."

# The web app manifest needs its own media type; older or trimmed mime.types files lack it.
mimetypes.add_type("application/manifest+json", ".webmanifest")

SWAGGER_UI_PARAMETERS = {
    "defaultModelsExpandDepth": 0,
    "tryItOutEnabled": True,
    "displayRequestDuration": True,
    # Disable the badge that sends the spec URL to the public validator.swagger.io service.
    "validatorUrl": None,
}

UI_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
    "object-src 'none'"
)
# Swagger UI / ReDoc rely on inline scripts and styles; only forbid framing there.
DOCS_CSP = "frame-ancestors 'none'"

OPENAPI_TAGS = [
    {
        "name": TAG_CONVERSION,
        "description": "Converte documentos Markdown em PDF ou Word (.docx).",
    },
    {
        "name": TAG_SYSTEM,
        "description": "Saúde do serviço, versão, limites e opções disponíveis.",
    },
]


# --------------------------------------------------------------------------- middleware


def _route_path(scope: Scope) -> str:
    """Request path relative to the application's ``root_path``."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path) and path[len(root_path) :][:1] in ("", "/"):
        return path[len(root_path) :] or "/"
    return path


class _BodyTooLarge(Exception):
    """Raised from ``receive`` to abort reading a request body over the limit."""


class UploadLimitMiddleware:
    """Rejects conversion requests whose body exceeds the configured limit with HTTP 413.

    ``Content-Length`` is checked before anything is read; bodies without it (chunked
    uploads) are counted while they stream in and cut off as soon as they cross the limit.
    The route itself enforces the exact limit on the decoded file / text.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, message: str) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.message = message

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _route_path(scope).startswith(CONVERT_PATH_PREFIX):
            await self.app(scope, receive, send)
            return

        if self._declared_length(scope) > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    if not response_started and not rejected:
                        rejected = True
                        await self._reject(scope, receive, send)
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return  # the 413 response was already sent
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise

    @staticmethod
    def _declared_length(scope: Scope) -> int:
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return 0
        return 0

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(413, self.message, "input_too_large")
        await response(scope, receive, send)


class ResponseHeadersMiddleware:
    """Adds security headers to every response and CSP / cache policy to HTML pages."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = _route_path(scope)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if not path.startswith("/api/"):
                    # UI and docs assets are revalidated (ETag) so upgrades show up at once.
                    headers.setdefault("Cache-Control", "no-cache")
                if headers.get("content-type", "").startswith("text/html"):
                    is_docs = path in (DOCS_URL, REDOC_URL)
                    headers.setdefault("Content-Security-Policy", DOCS_CSP if is_docs else UI_CSP)
                    headers.setdefault("X-Frame-Options", "DENY")
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- setup helpers


def configure_logging(level_name: str) -> None:
    """Set the level of the ``prelo`` loggers; add a handler unless logging is configured."""
    level = logging.getLevelNamesMapping().get(level_name.strip().upper(), logging.INFO)
    logger.setLevel(level)
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)


def _api_description(settings: Settings) -> str:
    ui_url = f"{settings.root_path.rstrip('/')}/"
    remote = "permitidas" if settings.allow_remote_images else "desativadas"
    return f"""
**Prelo** converte Markdown em PDF e Word mantendo tabelas, código, diagramas Mermaid, fórmulas e
muito mais.

Envie um documento **Markdown** e receba um **PDF** ou um **Word (.docx)** com a formatação
preservada: títulos, listas aninhadas e indentação, tabelas, blocos de código com realce de
sintaxe, diagramas **Mermaid**, fórmulas matemáticas, listas de tarefas, notas de rodapé,
alertas do GitHub, emojis, imagens e HTML simples. Prefere uma interface gráfica? Use a
[página web]({ui_url}).

## Como usar

**Arquivo** (`multipart/form-data`) — `.md`, `.markdown`, `.txt` ou `.zip` com imagens:

```bash
curl -f -OJ -F "file=@documento.md" http://localhost:8000/api/v1/convert/pdf
curl -f -OJ -F "file=@projeto.zip" -F "orientation=landscape" -F "toc=true" \\
  http://localhost:8000/api/v1/convert/docx
```

**Texto** (JSON):

```bash
curl -f -o ola.pdf -H "Content-Type: application/json" \\
  -d '{{"markdown": "# Olá\\n\\n| A | B |\\n|---|---|\\n| 1 | 2 |"}}' \\
  http://localhost:8000/api/v1/convert/pdf/text
```

A resposta é o próprio arquivo, com o nome em `Content-Disposition`, os avisos da conversão
em `X-Conversion-Warnings` (lista JSON; o total em `X-Conversion-Warnings-Count`) e a duração
em `X-Conversion-Time-Ms`.

## Limites desta instância

| Limite | Valor |
| --- | --- |
| Tamanho máximo do documento | {format_megabytes(settings.max_upload_mb)} MB |
| Conversões simultâneas | {settings.max_concurrent_jobs} |
| Tempo máximo por conversão | {settings.conversion_timeout:g} s |
| Imagens remotas (URL) | {remote} |
| Imagens por documento (tamanho somado) | {format_megabytes(settings.max_images_total_mb)} MB |

## Erros

Todas as respostas de erro são JSON no formato `{{"detail": "mensagem", "code": "código"}}`:

| HTTP | `code` | Quando |
| --- | --- | --- |
| 400 | `invalid_input` | Documento vazio, binário ou `.zip` inválido |
| 400 | `bad_request` | Corpo da requisição ilegível (por exemplo, `multipart` malformado) |
| 404 | `not_found` | Caminho inexistente |
| 405 | `method_not_allowed` | Método HTTP não aceito pelo caminho (veja o cabeçalho `Allow`) |
| 413 | `input_too_large` | Documento acima do limite, ou grande/complexo demais |
| 415 | `unsupported_file` | Extensão não suportada |
| 422 | `validation_error` | Parâmetro inválido (detalhes em `errors`) |
| 500 | `internal_error` | Erro inesperado |
| 503 | `unavailable` | Mecanismo de conversão indisponível ou ocupado |
| 504 | `timeout` | Conversão excedeu o tempo limite |
"""


def _install_docs(application: FastAPI, settings: Settings) -> None:
    """Swagger UI and ReDoc, served from local assets when available (offline-friendly)."""
    swagger_local = (settings.swagger_ui_dir / "swagger-ui-bundle.js").is_file()
    if swagger_local:
        application.mount(
            SWAGGER_ASSETS_URL, StaticFiles(directory=settings.swagger_ui_dir), name="swagger-ui"
        )
    else:
        logger.warning("Swagger UI assets not found in %s; using the CDN", settings.swagger_ui_dir)
    redoc_dir = settings.vendor_dir / "redoc"
    redoc_local = (redoc_dir / "redoc.standalone.js").is_file()
    if redoc_local:
        application.mount(REDOC_ASSETS_URL, StaticFiles(directory=redoc_dir), name="redoc")

    openapi_url = application.openapi_url or "/openapi.json"

    @application.api_route(DOCS_URL, methods=["GET", "HEAD"], include_in_schema=False)
    async def swagger_ui(request: Request) -> HTMLResponse:
        prefix = request.scope.get("root_path", "").rstrip("/")
        assets: dict[str, str] = {}
        if swagger_local:
            assets = {
                "swagger_js_url": f"{prefix}{SWAGGER_ASSETS_URL}/swagger-ui-bundle.js",
                "swagger_css_url": f"{prefix}{SWAGGER_ASSETS_URL}/swagger-ui.css",
            }
        page = get_swagger_ui_html(
            openapi_url=prefix + openapi_url,
            title=f"{application.title} — Swagger UI",
            swagger_favicon_url=f"{prefix}{FAVICON_PATH}",
            swagger_ui_parameters=SWAGGER_UI_PARAMETERS,
            **assets,
        )
        brand = (
            f'<header class="prelo-brand"><a href="{prefix}/">'
            f'<img src="{prefix}{LOGO_PATH}" alt="Prelo" width="112" height="40"></a></header>'
        )
        return _patch_html(
            page,
            {
                "<html>": HTML_LANG,
                "</head>": f"{SWAGGER_BRAND_STYLE}\n</head>",
                '<div id="swagger-ui">': f'{brand}\n<div id="swagger-ui">',
            },
        )

    @application.api_route(REDOC_URL, methods=["GET", "HEAD"], include_in_schema=False)
    async def redoc(request: Request) -> HTMLResponse:
        prefix = request.scope.get("root_path", "").rstrip("/")
        assets: dict[str, str] = {}
        if redoc_local:
            assets = {"redoc_js_url": f"{prefix}{REDOC_ASSETS_URL}/redoc.standalone.js"}
        page = get_redoc_html(
            openapi_url=prefix + openapi_url,
            title=f"{application.title} — ReDoc",
            redoc_favicon_url=f"{prefix}{FAVICON_PATH}",
            with_google_fonts=False,
            **assets,
        )
        return _patch_html(
            page,
            {
                "<html>": HTML_LANG,
                "<redoc ": f"<redoc theme='{REDOC_THEME}' ",
                REDOC_NOSCRIPT: REDOC_NOSCRIPT_PT,
            },
        )


def _patch_html(page: HTMLResponse, replacements: dict[str, str]) -> HTMLResponse:
    """Replace the first occurrence of each marker in a docs page built by FastAPI."""
    html = bytes(page.body).decode()
    for marker, markup in replacements.items():
        html = html.replace(marker, markup, 1)
    return HTMLResponse(html, status_code=page.status_code)


def _install_openapi_logo(application: FastAPI, settings: Settings) -> None:
    """Add the ReDoc ``x-logo`` extension (served by the web UI mount) to the OpenAPI info
    and use this instance's upload limit in the documented 413 examples."""
    build_schema = application.openapi
    root = settings.root_path.rstrip("/")
    logo = {"url": f"{root}{LOGO_PATH}", "altText": "Prelo", "href": f"{root}/"}

    def openapi() -> dict[str, Any]:
        if application.openapi_schema is None:
            schema = build_schema()
            schema["info"]["x-logo"] = logo
            limit = upload_limit_message(settings.max_upload_mb)
            for operations in schema.get("paths", {}).values():
                for operation in operations.values():
                    response = operation.get("responses", {}).get("413")
                    example = (response or {}).get("content", {}).get("application/json", {})
                    if "example" in example:
                        example["example"]["detail"] = limit
            application.openapi_schema = schema
        return application.openapi_schema

    application.openapi = openapi  # type: ignore[method-assign]


def _install_middleware(application: FastAPI, settings: Settings) -> None:
    # Added innermost first: CORS ends up outermost so even early 413s carry CORS headers.
    application.add_middleware(
        UploadLimitMiddleware,
        max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
        message=upload_limit_message(settings.max_upload_mb),
    )
    application.add_middleware(ResponseHeadersMiddleware)
    origins = settings.cors_origin_list
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=["*"] if "*" in origins else origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
            expose_headers=list(EXPOSED_HEADERS),
            max_age=600,
        )


class UIFiles(StaticFiles):
    """Static files whose 405 answer carries the ``Allow`` header (RFC 9110)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        if scope["method"] not in ("GET", "HEAD"):
            raise StarletteHTTPException(status_code=405, headers={"Allow": "GET, HEAD"})
        return await super().get_response(path, scope)


class UIMount(Mount):
    """The web UI at ``/``, which leaves the API and documentation paths to the router.

    A plain mount at ``/`` matches every path, so a wrong method on an API route (only a
    partial match there) or an unknown API path would get the static files' 405/404
    instead of the router's 404, or 405 with ``Allow``.
    """

    def __init__(self, *args: Any, reserved: tuple[str, ...], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reserved = reserved

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http":
            path = _route_path(scope)
            if path in self._reserved or path.startswith(f"{API_ROOT}/"):
                return Match.NONE, {}
        return super().matches(scope)


def _mount_ui(application: FastAPI) -> None:
    # Added last: "/" matches every other path, so it must come after the API and docs.
    if not STATIC_DIR.is_dir():
        logger.warning("Web UI directory not found: %s", STATIC_DIR)
        return
    reserved = (API_ROOT, DOCS_URL, REDOC_URL, application.openapi_url or "/openapi.json")
    application.router.routes.append(
        UIMount("/", app=UIFiles(directory=STATIC_DIR, html=True), name="ui", reserved=reserved)
    )


# --------------------------------------------------------------------------- factory


def create_app(
    settings: Settings | None = None, *, service_factory: ServiceFactory | None = None
) -> FastAPI:
    """Build the application.

    ``service_factory`` builds the conversion engine from the settings (default:
    :class:`ConversionService`); tests may pass a stub with the same interface.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    build_service = service_factory or ConversionService

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        service = build_service(settings)
        try:
            await service.start()
        except BaseException:
            logger.error("Conversion engine failed to start")
            with suppress(Exception):
                await service.stop()
            raise
        application.state.converter = service
        logger.info("Prelo %s ready", __version__)
        try:
            yield
        finally:
            application.state.converter = None
            await service.stop()

    application = FastAPI(
        title="Prelo API",
        version=__version__,
        summary="Conversão de Markdown para PDF e Word (.docx).",
        description=_api_description(settings),
        openapi_tags=OPENAPI_TAGS,
        contact={"name": "Prelo"},
        license_info={"name": "MIT", "identifier": "MIT"},
        root_path=settings.root_path,
        docs_url=None,
        redoc_url=None,
        separate_input_output_schemas=False,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.converter = None

    install_error_handlers(application)
    application.include_router(router)
    _install_docs(application, settings)
    _install_openapi_logo(application, settings)
    _install_middleware(application, settings)
    _mount_ui(application)
    return application


app = create_app()
