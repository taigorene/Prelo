#!/usr/bin/env sh
# Downloads pinned third-party front-end assets (Mermaid, Swagger UI and ReDoc)
# and verifies their SHA-256 checksums.
#
# Usage: scripts/fetch_vendor.sh [target_dir]   (default: ./vendor)
set -eu

TARGET="${1:-vendor}"
MERMAID_VERSION="12.1.0"
SWAGGER_UI_VERSION="5.33.1"
REDOC_VERSION="2.5.4"
CDN="https://cdn.jsdelivr.net/npm"

fetch() {
  url="$1"; dest="$2"; sum="$3"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ] && echo "$sum  $dest" | sha256sum -c - >/dev/null 2>&1; then
    return 0
  fi
  echo "-> $url"
  curl -fsSL --retry 3 -o "$dest.tmp" "$url"
  echo "$sum  $dest.tmp" | sha256sum -c - >/dev/null
  mv "$dest.tmp" "$dest"
}

fetch "$CDN/mermaid@$MERMAID_VERSION/dist/mermaid.min.js" \
  "$TARGET/mermaid/mermaid.min.js" \
  6484afc32872a3aa16cac9a76ba1816a1ed4cc870a6593cc2e17757750f518b2

fetch "$CDN/swagger-ui-dist@$SWAGGER_UI_VERSION/swagger-ui-bundle.js" \
  "$TARGET/swagger-ui/swagger-ui-bundle.js" \
  050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7

fetch "$CDN/swagger-ui-dist@$SWAGGER_UI_VERSION/swagger-ui.css" \
  "$TARGET/swagger-ui/swagger-ui.css" \
  1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321

fetch "$CDN/swagger-ui-dist@$SWAGGER_UI_VERSION/favicon-32x32.png" \
  "$TARGET/swagger-ui/favicon-32x32.png" \
  3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837

fetch "$CDN/redoc@$REDOC_VERSION/bundles/redoc.standalone.js" \
  "$TARGET/redoc/redoc.standalone.js" \
  dcaf76612bc4a3fbcc923a8966dee2f6146a5f32e5ce1b6f02dd60cbbf89500b

printf '%s\n' "$MERMAID_VERSION" > "$TARGET/mermaid/VERSION"
printf '%s\n' "$SWAGGER_UI_VERSION" > "$TARGET/swagger-ui/VERSION"
printf '%s\n' "$REDOC_VERSION" > "$TARGET/redoc/VERSION"
echo "Vendor assets ready in $TARGET"
