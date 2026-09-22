#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"
APP_ID=$(node -e "process.stdout.write(require(\"./manifest.json\").appId)")
VERSION=$(node -e "process.stdout.write(require(\"./manifest.json\").version)")
PACKAGE_VERSION=$(node -e "process.stdout.write(require(\"./package.json\").version)")
OUTPUT_DIR=${SMARTAPP_OUTPUT_DIR:-"$PROJECT_DIR/build/smartapp"}
STAGE_DIR=$(mktemp -d)
trap 'rm -rf -- "$STAGE_DIR"' EXIT

if [[ "$VERSION" != "$PACKAGE_VERSION" ]]; then
  echo "manifest.json version ($VERSION) 与 package.json version ($PACKAGE_VERSION) 不一致" >&2
  exit 1
fi

APP_ROOT="$STAGE_DIR/$APP_ID"
mkdir -p "$APP_ROOT/web" "$OUTPUT_DIR"
cp pages/english_show_800x480.html "$APP_ROOT/web/index.html"
cp manifest.json "$APP_ROOT/manifest.json"
node scripts/validate-smartapp.mjs "$APP_ROOT"
ARCHIVE="$OUTPUT_DIR/$APP_ID-$VERSION.tar.gz"
tar --sort=name --mtime="@0" --owner=0 --group=0 --numeric-owner -czf "$ARCHIVE.tmp" -C "$STAGE_DIR" "$APP_ID"
mv -f "$ARCHIVE.tmp" "$ARCHIVE"
echo "SmartApp 包：$ARCHIVE"
echo "packageSize=$(stat -c '%s' "$ARCHIVE")"
echo "sha256=$(sha256sum "$ARCHIVE" | awk '{print $1}')"
