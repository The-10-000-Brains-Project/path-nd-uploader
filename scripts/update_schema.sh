#!/usr/bin/env bash
# Fetches a specific tagged/commit revision of the pathnd-cdes CDE schema and
# vendors it into vendor/pathnd-cdes/. Deliberately pins to a ref rather than
# tracking main, so validation results are reproducible across runs.
#
# Usage: scripts/update_schema.sh <git-ref>
#   e.g. scripts/update_schema.sh v1.1.0
#        scripts/update_schema.sh 6a0f8771d107daa2773dee8473c51e791bd82114

set -euo pipefail

REF="${1:?Usage: update_schema.sh <git-ref>}"
REPO_URL="https://github.com/The-10-000-Brains-Project/pathnd-cdes.git"
DEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src/pathnd_uploader/vendor/pathnd-cdes"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

git clone --quiet "$REPO_URL" "$TMP_DIR"
git -C "$TMP_DIR" checkout --quiet "$REF"

cp "$TMP_DIR/PathND-Core-Schema.csv" "$DEST_DIR/"
cp "$TMP_DIR/VERSION" "$DEST_DIR/"
cp "$TMP_DIR/README.md" "$DEST_DIR/UPSTREAM_README.md"
git -C "$TMP_DIR" rev-parse HEAD > "$DEST_DIR/SOURCE_COMMIT.txt"

echo "Vendored pathnd-cdes schema at ref '$REF' (version $(cat "$DEST_DIR/VERSION"))"
