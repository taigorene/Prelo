"""Errors raised by the conversion engine. Messages are user-facing (pt-BR)."""

from __future__ import annotations


class ConversionError(Exception):
    """Base error. ``status_code`` / ``code`` are used by the HTTP layer."""

    status_code: int = 500
    code: str = "conversion_failed"
    default_message: str = "Falha ao converter o documento."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidInputError(ConversionError):
    status_code = 400
    code = "invalid_input"
    default_message = "Arquivo de entrada inválido."


class UnsupportedFileError(ConversionError):
    status_code = 415
    code = "unsupported_file"
    default_message = "Tipo de arquivo não suportado."


class InputTooLargeError(ConversionError):
    status_code = 413
    code = "input_too_large"
    default_message = "Arquivo grande demais."


class ConversionTimeoutError(ConversionError):
    status_code = 504
    code = "timeout"
    default_message = "A conversão excedeu o tempo limite."


class ServiceUnavailableError(ConversionError):
    status_code = 503
    code = "unavailable"
    default_message = "Serviço de conversão indisponível no momento."
