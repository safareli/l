#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="vercel-labs"
REPO_NAME="agent-browser"
UPSTREAM_PATH="skills/agent-browser"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="$(mktemp -d)"
ARCHIVE_PATH="$TMP_DIR/repo.tar.gz"
EXTRACT_DIR="$TMP_DIR/extract"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# If no arg is passed, read installed CLI version from `agent-browser --version`.
# You can still override manually: ./update.sh 0.17.1 or ./update.sh v0.17.1
if [[ $# -gt 0 ]]; then
  VERSION_RAW="$1"
else
  if ! command -v agent-browser >/dev/null 2>&1; then
    echo "Error: agent-browser is not in PATH. Pass a version explicitly, e.g. ./update.sh 0.17.1" >&2
    exit 1
  fi

  VERSION_OUTPUT="$(agent-browser --version 2>/dev/null || true)"
  VERSION_RAW="$(printf '%s\n' "$VERSION_OUTPUT" | grep -Eo 'v?[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z]+)*' | head -n 1 || true)"

  if [[ -z "$VERSION_RAW" ]]; then
    echo "Error: could not parse version from: $VERSION_OUTPUT" >&2
    exit 1
  fi
fi

TAG="v${VERSION_RAW#v}"
ARCHIVE_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/tags/${TAG}"

echo "Downloading ${ARCHIVE_URL}"
curl -fsSL "$ARCHIVE_URL" -o "$ARCHIVE_PATH"

mkdir -p "$EXTRACT_DIR"
tar -xzf "$ARCHIVE_PATH" -C "$EXTRACT_DIR"

ROOT_DIR="$(find "$EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
SOURCE_DIR="$ROOT_DIR/$UPSTREAM_PATH"

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "Error: could not find '$UPSTREAM_PATH' in archive" >&2
  exit 1
fi

# Replace everything in this directory except this updater script
find "$SCRIPT_DIR" -mindepth 1 -maxdepth 1 ! -name "update.sh" -exec rm -rf {} +
cp -a "$SOURCE_DIR"/. "$SCRIPT_DIR"/

echo "Updated: $SCRIPT_DIR"
echo "Source:  ${REPO_OWNER}/${REPO_NAME} tag ${TAG} / ${UPSTREAM_PATH}"
