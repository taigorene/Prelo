"""System endpoints, OpenAPI/Swagger documentation and the web UI."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.config import Settings
from app.converter.options import (
    ACCEPTED_EXTENSIONS,
    ConversionOptions,
    Margin,
    MermaidTheme,
    Orientation,
    PageSize,
)
from tests.conftest import ClientFactory
from tests.utils import make_settings

CONVERT_PATH = "/api/v1/convert/{format}"
CONVERT_TEXT_PATH = "/api/v1/convert/{format}/text"


@pytest.fixture(scope="module")
def openapi(client: TestClient) -> dict[str, Any]:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def _resolve(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = spec["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def _enum_values(spec: dict[str, Any], node: dict[str, Any]) -> set[str]:
    node = _resolve(spec, node)
    values = set(node.get("enum", []))
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(key, []):
            values |= _enum_values(spec, sub)
    return values


# --------------------------------------------------------------------------- health / info


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"pandoc": True, "browser": True}}


def test_info_describes_service(client: TestClient) -> None:
    settings: Settings = make_settings()
    response = client.get("/api/v1/info")
    assert response.status_code == 200
    info = response.json()

    assert info["name"]
    assert info["version"] == __version__
    assert info["formats"] == ["pdf", "docx"]
    assert info["accepted_extensions"] == list(ACCEPTED_EXTENSIONS)
    assert info["limits"]["max_upload_mb"] == settings.max_upload_mb
    assert info["limits"]["max_concurrent_jobs"] == settings.max_concurrent_jobs
    assert info["limits"]["conversion_timeout_seconds"] == settings.conversion_timeout
    assert info["limits"]["allow_remote_images"] is settings.allow_remote_images
    assert info["defaults"] == ConversionOptions().model_dump(mode="json")
    assert info["options"] == {
        "page_size": [item.value for item in PageSize],
        "orientation": [item.value for item in Orientation],
        "margin": [item.value for item in Margin],
        "mermaid_theme": [item.value for item in MermaidTheme],
    }
    engines = info["engines"]
    assert engines["pandoc"].startswith("3.")
    assert engines["chromium"].isdigit()  # only the major version (no exact build)
    mermaid_version = settings.mermaid_js_path.parent / "VERSION"
    if mermaid_version.is_file():
        assert engines["mermaid"] == mermaid_version.read_text().strip()


# --------------------------------------------------------------------------- OpenAPI


def test_openapi_metadata(openapi: dict[str, Any]) -> None:
    assert openapi["info"]["title"] == "Prelo API"
    assert openapi["info"]["version"] == __version__
    assert "MIT" in openapi["info"]["license"]["name"]
    tags = {tag["name"] for tag in openapi.get("tags", [])}
    assert {"Conversão", "Sistema"} <= tags
    for path in (CONVERT_PATH, CONVERT_TEXT_PATH, "/api/v1/health", "/api/v1/info"):
        assert path in openapi["paths"], path


def test_openapi_documents_multipart_upload(openapi: dict[str, Any]) -> None:
    operation = openapi["paths"][CONVERT_PATH]["post"]
    assert "Conversão" in operation["tags"]

    format_param = next(p for p in operation["parameters"] if p["name"] == "format")
    assert format_param["in"] == "path"
    assert _enum_values(openapi, format_param["schema"]) == {"pdf", "docx"}

    schema = _resolve(openapi, operation["requestBody"]["content"]["multipart/form-data"]["schema"])
    properties = schema["properties"]
    assert "file" in schema.get("required", [])
    expected_enums = {
        "page_size": {item.value for item in PageSize},
        "orientation": {item.value for item in Orientation},
        "margin": {item.value for item in Margin},
        "mermaid_theme": {item.value for item in MermaidTheme},
    }
    for name, values in expected_enums.items():
        assert name in properties, name
        assert _enum_values(openapi, properties[name]) == values, name
    for name in ("toc", "page_numbers"):
        assert name in properties, name

    responses = operation["responses"]
    for status in ("200", "400", "413", "415", "422", "503", "504"):
        assert status in responses, status
    media_types = set(responses["200"]["content"])
    assert media_types & {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }


def test_openapi_documents_text_endpoint(openapi: dict[str, Any]) -> None:
    operation = openapi["paths"][CONVERT_TEXT_PATH]["post"]
    content = operation["requestBody"]["content"]["application/json"]
    schema = _resolve(openapi, content["schema"])
    assert {"markdown", "filename", "options"} <= set(schema["properties"])
    assert "markdown" in schema.get("required", [])
    examples = [
        *(content.get("examples") or {}).values(),
        *(schema.get("examples") or []),
        *([content["example"]] if "example" in content else []),
    ]
    assert examples, "the text endpoint should ship a realistic example"


def test_openapi_documents_error_schema(openapi: dict[str, Any]) -> None:
    responses = openapi["paths"][CONVERT_PATH]["post"]["responses"]
    error = _resolve(openapi, responses["413"]["content"]["application/json"]["schema"])
    assert {"detail", "code"} <= set(error["properties"])


# --------------------------------------------------------------------------- docs pages


def test_swagger_ui_is_served(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "swagger-ui" in html.lower()
    assert "/openapi.json" in html

    bundle = re.search(r'src="([^"]*swagger-ui-bundle\.js)"', html)
    assert bundle, "Swagger UI bundle script not referenced"
    if make_settings().swagger_ui_dir.joinpath("swagger-ui-bundle.js").is_file():
        url = bundle.group(1)
        assert not url.startswith(("http://", "https://")), "vendored Swagger UI must be used"
        asset = client.get(url)
        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]


def test_swagger_ui_is_not_blocked_by_csp(client: TestClient) -> None:
    csp = client.get("/docs").headers.get("content-security-policy")
    if csp and "script-src" in csp:
        assert any(token in csp for token in ("'unsafe-inline'", "'nonce-", "'sha256-"))


def test_redoc_is_served(client: TestClient) -> None:
    response = client.get("/redoc")
    assert response.status_code == 200
    html = response.text
    assert "redoc" in html.lower()
    assert "fonts.googleapis.com" not in html

    bundle = re.search(r'src="([^"]*redoc[^"]*\.js)"', html)
    assert bundle, "ReDoc script not referenced"
    if make_settings().vendor_dir.joinpath("redoc", "redoc.standalone.js").is_file():
        url = bundle.group(1)
        assert not url.startswith(("http://", "https://")), "vendored ReDoc must be used"
        asset = client.get(url)
        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]


# --------------------------------------------------------------------------- web UI


def test_ui_index(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert 'lang="pt-BR"' in html
    assert "Prelo" in html
    assert re.search(r'href="(?:\.?/)?docs"', html), "the UI should link to the Swagger UI"


def test_ui_security_headers(client: TestClient) -> None:
    headers = client.get("/").headers
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("referrer-policy") == "no-referrer"
    csp = headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_ui_is_csp_compliant(client: TestClient) -> None:
    html = client.get("/").text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> breaks the CSP"
    assert "<style" not in html, "inline <style> breaks the CSP"
    assert not re.search(r"\sstyle\s*=", html), "style= attributes break the CSP"


def test_ui_assets_are_served(client: TestClient) -> None:
    html = client.get("/").text
    references = set(re.findall(r'(?:src|href)="([^"]+)"', html))
    local = {
        ref
        for ref in references
        if not ref.startswith(("http://", "https://", "//", "data:", "mailto:", "#"))
    }
    assert any(ref.endswith(".css") for ref in local)
    assert any(ref.endswith(".js") for ref in local)
    for ref in sorted(local):
        path = ref if ref.startswith("/") else f"/{ref.removeprefix('./')}"
        assert client.get(path.split("#", 1)[0]).status_code == 200, ref


def test_cors_allows_any_origin_by_default(client: TestClient) -> None:
    response = client.options(
        "/api/v1/convert/pdf",
        headers={"Origin": "https://exemplo.com.br", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"


# --------------------------------------------------------------------------- routing / methods


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/nope"),
        ("POST", "/api/v1/convert"),
        ("POST", "/api/v2/convert/pdf"),
        ("GET", "/api/v1/nada"),
    ],
)
def test_unknown_api_paths_are_404(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path)
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("GET", "/api/v1/convert/pdf", "POST"),
        ("PUT", "/api/v1/convert/pdf/text", "POST"),
        ("POST", "/api/v1/health", "GET"),
        ("POST", "/docs", "GET, HEAD"),
        ("POST", "/", "GET, HEAD"),
    ],
)
def test_wrong_methods_are_405_with_allow(
    client: TestClient, method: str, path: str, allow: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == 405, response.text
    assert response.json()["code"] == "method_not_allowed"
    allowed = {item.strip() for item in response.headers.get("allow", "").split(",")}
    assert allowed == {item.strip() for item in allow.split(",")}


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/info", "/docs", "/redoc", "/"])
def test_head_requests(client: TestClient, path: str) -> None:
    response = client.head(path)
    assert response.status_code == 200
    assert response.content == b""


def test_ui_still_serves_its_files(client: TestClient) -> None:
    assert client.get("/css/app.css").status_code == 200
    assert client.get("/nao-existe.css").status_code == 404


# --------------------------------------------------------------------------- docs language


def test_openapi_texts_are_portuguese(openapi: dict[str, Any]) -> None:
    for path in ("/api/v1/health", "/api/v1/info"):
        description = openapi["paths"][path]["get"]["responses"]["200"]["description"]
        assert description != "Successful Response", path
    framework_schemas = {"HTTPValidationError", "ValidationError"}  # FastAPI's own
    for name, schema in openapi["components"]["schemas"].items():
        if name.startswith("Body_") or name in framework_schemas:
            continue
        description = schema.get("description", "")
        assert "``" not in description, name
        assert not re.search(r"\b(the|of|and|when|Limits|Versions)\b", description), (
            name,
            description,
        )
    info = openapi["components"]["schemas"]["InfoResponse"]["properties"]
    for field in ("limits", "engines", "defaults"):
        assert info[field].get("description") or _resolve(openapi, info[field])["description"]


def test_operation_ids_have_no_underscores(openapi: dict[str, Any]) -> None:
    ids = [
        operation["operationId"]
        for operations in openapi["paths"].values()
        for operation in operations.values()
    ]
    assert ids and not any("_" in operation_id for operation_id in ids), ids


def test_documented_error_examples_match_the_endpoints(client_factory: ClientFactory) -> None:
    spec = client_factory(max_upload_mb=5).get("/openapi.json").json()
    text = spec["paths"][CONVERT_TEXT_PATH]["post"]["responses"]
    upload = spec["paths"][CONVERT_PATH]["post"]["responses"]
    text_error = text["422"]["content"]["application/json"]["example"]
    assert text_error["errors"][0]["loc"] == ["body", "options", "page_size"]
    assert "options.page_size" in text_error["detail"]
    assert upload["422"]["content"]["application/json"]["example"]["errors"][0]["loc"] == [
        "body",
        "page_size",
    ]
    for responses in (text, upload):
        assert "5 MB" in responses["413"]["content"]["application/json"]["example"]["detail"]
    assert "bad_request" in upload["400"]["description"]
    assert "`not_found`" in spec["info"]["description"]


def test_malformed_json_message_is_portuguese(client: TestClient) -> None:
    response = client.post(
        "/api/v1/convert/pdf/text",
        content=b'{"markdown":',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "Expecting" not in response.json()["detail"]


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
def test_docs_pages_declare_portuguese(client: TestClient, page: str) -> None:
    html = client.get(page).text
    assert '<html lang="pt-BR">' in html
    assert "requires Javascript" not in html
