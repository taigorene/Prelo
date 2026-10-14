# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# base: system dependencies (pandoc, headless Chromium, fonts) + Python deps
# ---------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS base

ARG TARGETARCH
ARG PANDOC_VERSION=3.12
ARG PANDOC_SHA256_AMD64=91903ff19f1b1d4db4129797c7e18f71212990d7394fcff1787719aebf04e372
ARG PANDOC_SHA256_ARM64=9c9165d5eb627b2ccc12868478f847487bda9dc043aa09a964105d5aebfc7b79

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PRELO_VENDOR_DIR=/opt/prelo/vendor \
    PRELO_REFERENCE_DOCX=/opt/prelo/reference.docx

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl tini \
      fonts-inter-variable fonts-jetbrains-mono fonts-dejavu-core fonts-dejavu-extra \
      fonts-noto-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# pandoc (pinned release, checksum verified)
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sum="$PANDOC_SHA256_AMD64" ;; \
      arm64) sum="$PANDOC_SHA256_ARM64" ;; \
      *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /tmp/pandoc.deb \
      "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-${arch}.deb"; \
    echo "$sum  /tmp/pandoc.deb" | sha256sum -c -; \
    dpkg -i /tmp/pandoc.deb; \
    rm -f /tmp/pandoc.deb; \
    pandoc --version | head -n 1

WORKDIR /app

# Python dependencies: every package pinned with its hashes (requirements.txt is generated
# from requirements.in, see scripts/lock_requirements.sh). Playwright pins the Chromium build.
COPY requirements.txt ./
RUN pip install --require-hashes -r requirements.txt \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

# Mermaid, Swagger UI and ReDoc assets (pinned, checksum verified). Line endings are
# normalized first: a Windows checkout may have converted the script to CRLF.
COPY scripts/fetch_vendor.sh /usr/local/bin/fetch_vendor.sh
RUN sed -i 's/\r$//' /usr/local/bin/fetch_vendor.sh \
 && sh /usr/local/bin/fetch_vendor.sh "$PRELO_VENDOR_DIR"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

# ---------------------------------------------------------------------------
# app: application code + generated Word reference document
# ---------------------------------------------------------------------------
FROM base AS app

COPY app ./app
RUN python -m app.converter.docx_reference "$PRELO_REFERENCE_DOCX"

# ---------------------------------------------------------------------------
# test: app + dev dependencies + test suite  (docker build --target test .)
# ---------------------------------------------------------------------------
FROM app AS test

COPY requirements-dev.txt pyproject.toml ./
RUN pip install --require-hashes -r requirements-dev.txt
COPY tests ./tests
COPY examples ./examples
USER app
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# ---------------------------------------------------------------------------
# runtime (default target)
# ---------------------------------------------------------------------------
FROM app AS runtime

# uvicorn settings (read from the environment):
# * FORWARDED_ALLOW_IPS: proxies whose X-Forwarded-For/-Proto headers are trusted
#   (as seen from the container; a reverse proxy on the Docker host is the bridge gateway);
# * UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN: on stop, running conversions get this long to finish
#   (keep it below the container's stop timeout, e.g. compose's stop_grace_period).
ENV FORWARDED_ALLOW_IPS=127.0.0.1,::1 \
    UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=125

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
