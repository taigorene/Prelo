"""Security: local file disclosure, SSRF and hostile HTML inside the Markdown."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ImageServer
from tests.utils import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    PIXEL_PNG,
    assert_no_passwd_leak,
    build_zip,
    convert,
    docx_media,
    expect_ok,
    pdf_image_count,
    pdf_text,
    warnings_of,
)

# --------------------------------------------------------------------------- local files


@pytest.mark.parametrize(
    "reference",
    [
        "![segredo](/etc/passwd)",
        "![segredo](../../../../../../etc/passwd)",
        "![segredo](file:///etc/passwd)",
        "![segredo](%2Fetc%2Fpasswd)",
        "![segredo](/proc/self/environ)",
        '<img src="/etc/passwd" alt="segredo">',
        '<p><img src="file:///etc/passwd" alt="segredo"></p>',
    ],
)
def test_local_files_are_never_embedded_in_docx(client: TestClient, reference: str) -> None:
    response = convert(client, "docx", f"# Segredo\n\n{reference}\n\nFim.\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert docx_media(response.content) == {}
    assert warnings_of(response), "a skipped image must be reported"
    assert_no_passwd_leak(response.content)


def test_local_files_never_reach_the_pdf(client: TestClient) -> None:
    markdown = """# Documento

<iframe src="file:///etc/passwd"></iframe>
<iframe src="/etc/passwd"></iframe>
<iframe src="../../../../etc/passwd"></iframe>
<object data="file:///etc/passwd" type="text/plain"></object>
<embed src="file:///etc/passwd" type="text/plain">
<link rel="stylesheet" href="file:///etc/passwd">
<img src="file:///etc/passwd" alt="a">
<img src="/etc/passwd" alt="b">

![segredo](/etc/passwd)

Fim do documento.
"""
    response = expect_ok(convert(client, "pdf", markdown), PDF_MEDIA_TYPE)
    assert "Fim do documento." in pdf_text(response.content)
    assert warnings_of(response)
    assert_no_passwd_leak(response.content)


def test_zip_images_cannot_escape_the_archive(client: TestClient) -> None:
    readme = "# Fuga\n\n![a](../../../../etc/passwd)\n\n![b](/../../../etc/passwd)\n"
    archive = build_zip([("docs/README.md", readme)])
    response = expect_ok(convert(client, "docx", archive, "fuga.zip"), DOCX_MEDIA_TYPE)
    assert docx_media(response.content) == {}
    assert warnings_of(response)
    assert_no_passwd_leak(response.content)


@pytest.mark.parametrize(
    "source",
    [
        "javascript:alert(1)",
        "ftp://exemplo.com.br/imagem.png",
        "data:text/html;base64,PGgxPk9sw6E8L2gxPg==",
        "imagens/inexistente.png",
    ],
)
def test_unusable_image_sources_become_warnings(client: TestClient, source: str) -> None:
    response = convert(client, "docx", f"Texto.\n\n![imagem]({source})\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert docx_media(response.content) == {}
    assert warnings_of(response)


# ------------------------------------------------------------------ SSRF (blocked by default)


BLOCKED_HOSTS = ["127.0.0.1", "localhost", "0.0.0.0", "127.1", "2130706433", "[::ffff:127.0.0.1]"]


def test_private_addresses_are_blocked_by_default(
    client: TestClient, image_server: ImageServer
) -> None:
    images = "\n\n".join(
        f"![host {index}]({image_server.url(f'/host-{index}.png', host)})"
        for index, host in enumerate(BLOCKED_HOSTS)
    )
    response = convert(client, "docx", f"# SSRF\n\n{images}\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert image_server.hits == [], "the converter reached a loopback address"
    assert docx_media(response.content) == {}
    assert warnings_of(response)


def test_pdf_renderer_cannot_reach_private_addresses(
    client: TestClient, image_server: ImageServer
) -> None:
    url = image_server.url
    markdown = f"""# SSRF no PDF

<p><img src="{url("/raw.png")}" alt="bruto"></p>
<div style="background-image:url({url("/fundo.png")});width:20px;height:20px"></div>
<link rel="stylesheet" href="{url("/estilo.css")}">
<iframe src="{url("/quadro.html")}"></iframe>
<script src="{url("/script.js")}"></script>
<video src="{url("/video.mp4")}"></video>
<object data="{url("/objeto.png")}" type="image/png"></object>

![markdown]({url("/markdown.png")})

Fim do documento.
"""
    response = expect_ok(convert(client, "pdf", markdown), PDF_MEDIA_TYPE)
    assert image_server.hits == [], "the PDF renderer reached a loopback address"
    assert "Fim do documento." in pdf_text(response.content)
    assert warnings_of(response)


def test_pdf_renderer_never_runs_document_javascript(client: TestClient) -> None:
    markdown = """# Scripts

<script>document.body.insertAdjacentText("beforeend", "SCRIPT-EXECUTADO")</script>
<img src="data:," alt="" onerror="document.body.append('ONERROR-EXECUTADO')">
<p onclick="x()">Parágrafo com atributo de evento.</p>
"""
    response = expect_ok(convert(client, "pdf", markdown), PDF_MEDIA_TYPE)
    text = pdf_text(response.content)
    assert "Parágrafo com atributo de evento." in text
    for marker in ("SCRIPT-EXECUTADO", "ONERROR-EXECUTADO"):
        assert marker not in text, marker


def test_pdf_meta_refresh_cannot_navigate(client: TestClient, image_server: ImageServer) -> None:
    markdown = (
        f'<meta http-equiv="refresh" content="0; url={image_server.url("/refresh.html")}">\n\n'
        "# Conteúdo original\n\nTexto que deve aparecer no PDF.\n"
    )
    response = expect_ok(convert(client, "pdf", markdown), PDF_MEDIA_TYPE)
    assert image_server.hits == []
    assert "Texto que deve aparecer no PDF." in pdf_text(response.content)


# ------------------------------------------------------------------ SSRF (explicitly allowed)


def test_private_network_image_when_allowed(
    private_client: TestClient, image_server: ImageServer
) -> None:
    response = convert(private_client, "docx", f"![pixel]({image_server.url('/pixel.png')})\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert warnings_of(response) == []
    assert image_server.hits == ["/pixel.png"]
    assert list(docx_media(response.content).values()) == [PIXEL_PNG]


def test_pdf_raw_html_image_when_allowed(
    private_client: TestClient, image_server: ImageServer
) -> None:
    markdown = f'<p><img src="{image_server.url("/raw.png")}" width="40" alt="bruto"></p>\n\nFim.\n'
    response = expect_ok(convert(private_client, "pdf", markdown), PDF_MEDIA_TYPE)
    assert "/raw.png" in image_server.hits
    assert pdf_image_count(response.content) >= 1


def test_redirects_are_followed(private_client: TestClient, image_server: ImageServer) -> None:
    response = convert(private_client, "docx", f"![r]({image_server.url('/redirect.png')})\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert image_server.hits == ["/redirect.png", "/pixel.png"]
    assert list(docx_media(response.content).values()) == [PIXEL_PNG]


def test_redirect_loops_are_cut(private_client: TestClient, image_server: ImageServer) -> None:
    response = convert(private_client, "docx", f"![loop]({image_server.url('/loop.png')})\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert docx_media(response.content) == {}
    assert warnings_of(response)
    assert 1 < len(image_server.hits) <= 6  # first request + at most 5 redirects


def test_remote_non_image_is_rejected(
    private_client: TestClient, image_server: ImageServer
) -> None:
    response = convert(private_client, "docx", f"![html]({image_server.url('/page.html')})\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert docx_media(response.content) == {}
    assert warnings_of(response)


def test_remote_image_size_limit(private_client: TestClient, image_server: ImageServer) -> None:
    response = convert(private_client, "docx", f"![grande]({image_server.url('/big.png')})\n")
    expect_ok(response, DOCX_MEDIA_TYPE)
    assert docx_media(response.content) == {}
    assert warnings_of(response)


def test_remote_image_count_limit(private_client: TestClient, image_server: ImageServer) -> None:
    images = "\n\n".join(f"![{n}]({image_server.url(f'/img/{n}.png')})" for n in range(1, 5))
    response = expect_ok(convert(private_client, "docx", images), DOCX_MEDIA_TYPE)
    assert len(docx_media(response.content)) == 3  # max_remote_images=3
    assert len(set(image_server.hits)) == 3
    assert warnings_of(response)
