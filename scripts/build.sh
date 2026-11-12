#!/usr/bin/env bash
# Build and package TA_hpe_aruba_central.
#   scripts/build.sh [version]
# Requires: .venv (UCC 6.6.1) and .venv-build39 (Python 3.9 with pip), see README.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-$(python3 -c 'import json;print(json.load(open("globalConfig.json"))["meta"]["version"])')}"
UCC=".venv/bin/ucc-gen"
PY39="${PY39:-$PWD/.venv-build39/bin/python}"

rm -rf output dist
"$UCC" build \
  --source package \
  --ta-version "$VERSION" \
  --python-binary-name "$PY39" \
  --pip-custom-flag="--no-compile --prefer-binary --ignore-installed --no-binary charset-normalizer"

cp README.md CHANGELOG.md output/TA_hpe_aruba_central/

mkdir -p dist
"$UCC" package --path output/TA_hpe_aruba_central -o dist

COMPANION_VERSION="$(sed -n 's/^version = //p' companion/aruba_central_poc/default/app.conf | head -1)"
COPYFILE_DISABLE=1 tar -C companion -czf "dist/aruba_central_poc-${COMPANION_VERSION}.tar.gz" \
  --exclude='local' --exclude='.DS_Store' aruba_central_poc
ls -l dist/
