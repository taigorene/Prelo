"""Request and response models of the HTTP API (they document the OpenAPI schema)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.converter.options import (
    ConversionOptions,
    Margin,
    MermaidTheme,
    Orientation,
    OutputFormat,
    PageSize,
    schema_description,
)


class ErrorResponse(BaseModel):
    """Error payload returned by every endpoint."""

    model_config = schema_description("Erro retornado por qualquer endpoint.")

    detail: str = Field(
        description="Mensagem de erro legível, em português.",
        examples=["Tipo de arquivo não suportado."],
    )
    code: str = Field(
        description="Código estável do erro, útil para tratamento automatizado.",
        examples=["unsupported_file"],
    )


class ValidationErrorItem(BaseModel):
    """One invalid field of a request."""

    model_config = schema_description("Um campo inválido da requisição.")

    loc: list[str | int] = Field(
        description="Localização do campo inválido (origem e nome).",
        examples=[["body", "page_size"]],
    )
    msg: str = Field(
        description="Descrição do problema.",
        examples=["valor inválido (aceitos: 'A4', 'Letter', 'Legal', 'A3' ou 'A5')"],
    )
    type: str = Field(description="Tipo do erro de validação.", examples=["enum"])


class ValidationErrorResponse(ErrorResponse):
    """Error payload for requests whose parameters fail validation (HTTP 422)."""

    model_config = schema_description("Erro de validação dos parâmetros (HTTP 422).")

    errors: list[ValidationErrorItem] = Field(description="Detalhes de cada campo inválido.")


class TextConversionRequest(BaseModel):
    """Markdown sent as text inside a JSON body."""

    model_config = ConfigDict(
        extra="forbid", **schema_description("Markdown enviado como texto em um JSON.")
    )

    markdown: str = Field(
        min_length=1,
        description="Conteúdo Markdown a converter (sintaxe do GitHub).",
        examples=["# Título\n\nTexto com **negrito** e `código`."],
    )
    filename: str = Field(
        "documento",
        max_length=255,
        description=(
            "Nome do arquivo gerado, sem extensão (uma extensão informada é ignorada). "
            "Usado no cabeçalho `Content-Disposition` da resposta."
        ),
        examples=["relatório-mensal"],
    )
    options: ConversionOptions = Field(
        default_factory=ConversionOptions,
        description="Opções de layout. Todos os campos são opcionais.",
    )


class HealthChecks(BaseModel):
    """Status of each dependency of the conversion engine."""

    model_config = ConfigDict(
        extra="allow",
        **schema_description("Situação de cada dependência do mecanismo de conversão."),
    )

    pandoc: bool = Field(description="O pandoc está disponível.")
    browser: bool = Field(
        description="O Chromium headless (usado no PDF e nos diagramas Mermaid) está ativo."
    )


class HealthResponse(BaseModel):
    """Service health."""

    model_config = schema_description("Saúde do serviço.")

    status: Literal["ok", "unavailable"] = Field(
        description="`ok` quando todas as verificações passam; `unavailable` caso contrário."
    )
    checks: HealthChecks = Field(description="Situação de cada dependência.")


class InfoLimits(BaseModel):
    """Limits enforced by this instance."""

    model_config = schema_description("Limites desta instância.")

    max_upload_mb: float = Field(description="Tamanho máximo do arquivo enviado, em MB.")
    max_concurrent_jobs: int = Field(description="Número de conversões executadas em paralelo.")
    conversion_timeout_seconds: float = Field(description="Tempo máximo de cada conversão.")
    allow_remote_images: bool = Field(
        description="Se imagens referenciadas por URL (http/https) são baixadas e incorporadas."
    )


class InfoOptions(BaseModel):
    """Accepted values of each conversion option."""

    model_config = schema_description("Valores aceitos por opção de conversão.")

    page_size: list[PageSize]
    orientation: list[Orientation]
    margin: list[Margin]
    mermaid_theme: list[MermaidTheme]


class EngineVersions(BaseModel):
    """Versions of the conversion engines (``null`` when unknown)."""

    model_config = schema_description(
        "Versões dos mecanismos de conversão (`null` quando desconhecida)."
    )

    pandoc: str | None = Field(description="Versão do pandoc.")
    chromium: str | None = Field(description="Versão principal do Chromium.")
    mermaid: str | None = Field(description="Versão do Mermaid.")


class InfoResponse(BaseModel):
    """Service metadata: version, limits, options and engines."""

    model_config = schema_description("Metadados do serviço: versão, limites, opções e motores.")

    name: str = Field(description="Nome do serviço.", examples=["Prelo"])
    version: str = Field(description="Versão da aplicação.", examples=["1.0.0"])
    formats: list[OutputFormat] = Field(description="Formatos de saída disponíveis.")
    accepted_extensions: list[str] = Field(
        description="Extensões de arquivo aceitas no upload.",
        examples=[[".md", ".markdown", ".txt", ".zip"]],
    )
    limits: InfoLimits = Field(description="Limites desta instância.")
    defaults: ConversionOptions = Field(description="Valores padrão das opções de conversão.")
    options: InfoOptions = Field(description="Valores aceitos por opção.")
    engines: EngineVersions = Field(description="Versões dos mecanismos de conversão.")
