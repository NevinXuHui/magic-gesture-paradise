#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

APP_ID=$(node -e "process.stdout.write(require('./manifest.json').appId)")
VERSION=$(node -e "process.stdout.write(require('./manifest.json').version)")
PACKAGE_VERSION=$(node -e "process.stdout.write(require('./package.json').version)")
OUTPUT_DIR=${SMARTAPP_OUTPUT_DIR:-"$PROJECT_DIR/build/smartapp"}
STAGE_DIR=$(mktemp -d)

cleanup() {
  if [[ -n "${STAGE_DIR:-}" && -d "$STAGE_DIR" ]]; then
    rm -rf -- "$STAGE_DIR"
  fi
}
trap cleanup EXIT

if [[ "$VERSION" != "$PACKAGE_VERSION" ]]; then
  echo "manifest.json version ($VERSION) 与 package.json version ($PACKAGE_VERSION) 不一致" >&2
  exit 1
fi

echo "构建 SmartApp H5：$APP_ID@$VERSION"
VITE_SMARTAPP=1 VITE_CAMERA_API_BASE=http://127.0.0.1:18081 npm run build

APP_ROOT="$STAGE_DIR/$APP_ID"
mkdir -p "$APP_ROOT/web" "$APP_ROOT/backend" "$OUTPUT_DIR"
cp -R --no-preserve=ownership dist/. "$APP_ROOT/web/"
cp manifest.json "$APP_ROOT/manifest.json"
cp backend/main.py "$APP_ROOT/backend/main.py"

node scripts/validate-smartapp.mjs "$APP_ROOT"

ARCHIVE="$OUTPUT_DIR/$APP_ID-$VERSION.tar.gz"
TEMP_ARCHIVE="$STAGE_DIR/$APP_ID-$VERSION.tar.gz"
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
  -czf "$TEMP_ARCHIVE" -C "$STAGE_DIR" "$APP_ID"
mv -f "$TEMP_ARCHIVE" "$ARCHIVE"

echo "SmartApp 包：$ARCHIVE"
echo "packageSize=$(stat -c '%s' "$ARCHIVE")"
echo "sha256=$(sha256sum "$ARCHIVE" | awk '{print $1}')"
