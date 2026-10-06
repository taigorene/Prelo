#!/bin/sh
# Regenerate requirements.txt and requirements-dev.txt (every package pinned, with hashes)
# from requirements.in and requirements-dev.in, inside the same base image the Dockerfile
# uses so that the resolution matches the container (Linux, CPython 3.13).
#
# Usage: sh scripts/lock_requirements.sh   (or: make lock)
set -eu

cd "$(dirname "$0")/.."
image="$(sed -n 's/^FROM \(python:[^ ]*\) AS base$/\1/p' Dockerfile)"
[ -n "$image" ] || { echo "base image not found in the Dockerfile" >&2; exit 1; }

docker run --rm -v "$PWD:/src" -w /src -e PIP_DISABLE_PIP_VERSION_CHECK=1 "$image" sh -ec '
  pip install --quiet --root-user-action=ignore "pip-tools==7.6.1"
  for name in requirements requirements-dev; do
    pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras \
      --output-file="$name.txt" "$name.in"
    # pip-tools 7.6 writes a spurious --no-index into the command it records.
    sed -i "s/^\(#    pip-compile .*\) --no-index /\1 /" "$name.txt"
  done
'
