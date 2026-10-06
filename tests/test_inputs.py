"""Input handling: validation, encodings, size limits and .zip archives."""

from __future__ import annotations

import io
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.utils import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    SVG_SAMPLE,
    WORD_IMAGE_SIGNATURES,
    assert_no_passwd_leak,
    build_zip,
    convert,
    docx_drawing_count,
    docx_media,
    docx_paragraph_styles,
    docx_text,
    expect_error,
    expect_ok,
    make_png,
    mark_encrypted,
    pdf_image_count,
    warnings_of,
)

PNG = make_png(8, 8, (22, 163, 74))

# --------------------------------------------------------------------------- invalid uploads


@pytest.mark.parametrize(
    ("data", "filename"),
    [
        (b"", "vazio.md"),
        (b"   \n\t\r\n  ", "espacos.md"),
        (bytes(range(256)) * 64, "binario.md"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 512, "imagem.md"),
    ],
    ids=["empty", "whitespace", "binary", "png-named-md"],
)
def test_invalid_markdown_is_rejected(client: TestClient, data: bytes, filename: str) -> None:
    expect_error(convert(client, "pdf", data, filename), 400, "invalid_input")


@pytest.mark.parametrize("filename", ["programa.exe", "documento.pdf", "planilha.xlsx"])
def test_unsupported_extension_is_rejected(client: TestClient, filename: str) -> None:
    response = convert(client, "docx", b"# Titulo\n", filename)
    expect_error(response, 415, "unsupported_file")


@pytest.mark.parametrize("filename", ["NOTAS.MD", "notas.markdown", "notas.mdown", "notas.txt"])
def test_accepted_extensions(client: TestClient, filename: str) -> None:
    response = convert(client, "docx", "# Título\n\nTexto.", filename)
    content = expect_ok(response, DOCX_MEDIA_TYPE).content
    assert "Título" in docx_paragraph_styles(content).get("Heading 1", [])


def test_missing_file_field(client: TestClient) -> None:
    response = client.post("/api/v1/convert/pdf", data={"toc": "true"})
    expect_error(response, 422, "validation_error")


# --------------------------------------------------------------------------- encodings


@pytest.mark.parametrize(
    "encoded",
    [
        "\ufeff# Título\n\nAção e coração.".encode(),
        "# Título\n\nAção e coração.".encode("utf-16"),
        "# Título\n\nAção e coração.".encode("cp1252"),
        "# Título\r\n\r\nAção e coração.\r\n".encode(),
        "# Título\r\rAção e coração.\r".encode(),
    ],
    ids=["utf8-bom", "utf16-bom", "cp1252", "crlf", "cr"],
)
def test_text_encodings_and_line_endings(client: TestClient, encoded: bytes) -> None:
    content = expect_ok(convert(client, "docx", encoded, "texto.md"), DOCX_MEDIA_TYPE).content
    assert docx_paragraph_styles(content).get("Heading 1") == ["Título"]
    text = docx_text(content)
    assert "Ação e coração." in text
    assert "\r" not in text and "\ufeff" not in text


# --------------------------------------------------------------------------- size limits


def test_upload_over_limit_is_rejected(small_client: TestClient) -> None:
    oversized = ("Texto de enchimento. " * 1200).encode()  # ~25 KB > 10 KB
    expect_error(convert(small_client, "pdf", oversized, "grande.md"), 413)


def test_text_endpoint_over_limit_is_rejected(small_client: TestClient) -> None:
    response = small_client.post(
        "/api/v1/convert/pdf/text", json={"markdown": "Texto de enchimento. " * 1200}
    )
    expect_error(response, 413)


def test_declared_content_length_over_limit_is_rejected(small_client: TestClient) -> None:
    body = b"x" * (int(0.01 * 1024 * 1024) + 1024 * 1024 + 4096)
    response = small_client.post(
        "/api/v1/convert/pdf",
        content=body,
        headers={"Content-Type": "multipart/form-data; boundary=limite"},
    )
    expect_error(response, 413)


def test_upload_under_limit_is_accepted(small_client: TestClient) -> None:
    response = convert(small_client, "docx", "# Pequeno\n\nCabe no limite.", "pequeno.md")
    expect_ok(response, DOCX_MEDIA_TYPE)


# --------------------------------------------------------------------------- zip archives


def test_zip_with_relative_images(client: TestClient) -> None:
    readme = (
        "# Projeto\n\n"
        "![Quadrado](../img/quadrado.png)\n\n"
        "![Vetor](../img/forma.svg)\n\n"
        "![Raiz do zip](/img/quadrado.png)\n"
    )
    archive = build_zip(
        [("docs/README.md", readme), ("img/quadrado.png", PNG), ("img/forma.svg", SVG_SAMPLE)]
    )
    response = expect_ok(convert(client, "docx", archive, "projeto.zip"), DOCX_MEDIA_TYPE)
    assert warnings_of(response) == []
    assert docx_drawing_count(response.content) == 3
    media = docx_media(response.content)
    assert PNG in media.values()
    for name, data in media.items():
        assert data.startswith(WORD_IMAGE_SIGNATURES), f"{name}: SVG must be rasterized for Word"


def test_zip_with_relative_image_to_pdf(client: TestClient) -> None:
    archive = build_zip([("leia.md", "# Foto\n\n![Quadrado](fotos/q.png)\n"), ("fotos/q.png", PNG)])
    response = expect_ok(convert(client, "pdf", archive, "fotos.zip"), PDF_MEDIA_TYPE)
    assert warnings_of(response) == []
    assert pdf_image_count(response.content) >= 1


def test_zip_main_document_selection(client: TestClient) -> None:
    archive = build_zip(
        [
            ("capitulos/a.md", "# Capítulo A\n"),
            ("readme.md", "# Documento principal\n"),
            ("anexos/index.md", "# Índice dos anexos\n"),
        ]
    )
    content = expect_ok(convert(client, "docx", archive, "livro.zip"), DOCX_MEDIA_TYPE).content
    assert docx_paragraph_styles(content).get("Heading 1") == ["Documento principal"]


def test_zip_without_markdown_is_rejected(client: TestClient) -> None:
    archive = build_zip([("img/quadrado.png", PNG), ("dados.json", "{}")])
    expect_error(convert(client, "pdf", archive, "sem-md.zip"), 400, "invalid_input")


def test_malformed_zip_is_rejected(client: TestClient) -> None:
    expect_error(convert(client, "pdf", b"PK\x03\x04isto nao e um zip", "quebrado.zip"), 400)


def test_encrypted_zip_is_rejected(client: TestClient) -> None:
    archive = mark_encrypted(build_zip([("README.md", "# Secreto\n")]))
    expect_error(convert(client, "pdf", archive, "cifrado.zip"), 400, "invalid_input")


@pytest.mark.parametrize(
    "entry_name",
    ["../fora.md", "docs/../../fora.md", "/etc/fora.md", "C:/fora.md"],
    ids=["dotdot", "nested-dotdot", "absolute", "drive-letter"],
)
def test_zip_slip_is_rejected(client: TestClient, entry_name: str) -> None:
    archive = build_zip([("README.md", "# Seguro\n"), (entry_name, "# Malicioso\n")])
    expect_error(convert(client, "docx", archive, "slip.zip"), 400, "invalid_input")


def test_zip_symlink_entry_is_not_followed(client: TestClient) -> None:
    link = zipfile.ZipInfo("img/senhas.png")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = build_zip(
        [("README.md", "# Link\n\n![Senhas](img/senhas.png)\n"), (link, "/etc/passwd")]
    )
    response = convert(client, "docx", archive, "link.zip")
    if response.status_code == 400:
        expect_error(response, 400, "invalid_input")
    else:
        expect_ok(response, DOCX_MEDIA_TYPE)
        assert docx_media(response.content) == {}
        assert warnings_of(response)
        assert_no_passwd_leak(response.content)


def test_zip_with_too_many_entries_is_rejected(small_client: TestClient) -> None:
    entries = [("README.md", "# Muitos\n")] + [(f"img/{n}.txt", "x") for n in range(10)]
    expect_error(convert(small_client, "pdf", build_zip(entries), "muitos.zip"), 413)


def test_zip_bomb_is_rejected(small_client: TestClient) -> None:
    archive = build_zip([("README.md", "# Bomba\n\n" + "a" * 400_000)])  # compresses to < 1 KB
    assert len(archive) < 4096
    expect_error(convert(small_client, "pdf", archive, "bomba.zip"), 413)


def test_macos_metadata_is_ignored(client: TestClient) -> None:
    archive = build_zip(
        [
            ("__MACOSX/._notas.md", b"\x00\x05\x16\x07" + b"\x00" * 64),
            (".DS_Store", b"\x00\x00\x00\x01Bud1" + b"\x00" * 64),
            ("notas.md", "# Notas do Mac\n"),
        ]
    )
    content = expect_ok(convert(client, "docx", archive, "mac.zip"), DOCX_MEDIA_TYPE).content
    assert docx_paragraph_styles(content).get("Heading 1") == ["Notas do Mac"]


def test_zip_markdown_is_held_to_the_upload_limit(small_client: TestClient) -> None:
    # 30 KB of Markdown (the limit is about 10 KB) compressed into a tiny .zip.
    archive = build_zip([("README.md", "# Grande\n\n" + "palavra " * 4_000)])
    assert len(archive) < 4096
    error = expect_error(convert(small_client, "pdf", archive, "grande.zip"), 413)
    assert "README.md" in error["detail"]


def test_zip_names_in_the_windows_oem_code_page(client: TestClient) -> None:
    """Names written in CP850 without the UTF-8 flag (7-Zip/older Windows on pt-BR)."""
    archive = build_zip(
        [
            ("relatorio.md", "# Relatório\n\n![Ação](imagens/ação.png)\n"),
            ("imagens/acao.png", PNG),
        ]
    )
    # Rename the image entry (local and central headers) to the CP850 bytes of "ação.png":
    # same length, UTF-8 flag unset.
    archive = archive.replace(b"imagens/acao.png", "imagens/ação.png".encode("cp850"))
    zipped = zipfile.ZipFile(io.BytesIO(archive))
    assert not any(info.flag_bits & 0x800 for info in zipped.infolist())
    response = expect_ok(convert(client, "docx", archive, "relatorio.zip"), DOCX_MEDIA_TYPE)
    assert warnings_of(response) == []
    assert len(docx_media(response.content)) == 1


def test_stray_legacy_bytes_do_not_garble_utf8_text(client: TestClient) -> None:
    utf8 = (
        "# Relatório de Ação\n\n" + "Este parágrafo tem acentuação: informações, órgãos.\n\n" * 20
    )
    data = utf8.encode() + "Linha colada de outro arquivo: café.\n".encode("latin-1")
    response = expect_ok(convert(client, "docx", data, "misto.md"), DOCX_MEDIA_TYPE)
    text = docx_text(response.content)
    assert "Relatório de Ação" in text and "informações, órgãos" in text
    assert "café" in text
    assert "Ã" not in text
    (warning,) = warnings_of(response)
    assert "UTF-8" in warning and "1 caractere" in warning
