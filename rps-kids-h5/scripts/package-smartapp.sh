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
VITE_SMARTAPP=1 npm run build

APP_ROOT="$STAGE_DIR/$APP_ID"
mkdir -p "$APP_ROOT/web" "$APP_ROOT/backend" "$OUTPUT_DIR"
cp -R dist/. "$APP_ROOT/web/"
cp manifest.json "$APP_ROOT/manifest.json"
cp backend/main.py backend/inference.py backend/requirements.txt "$APP_ROOT/backend/"
mkdir -p "$APP_ROOT/backend/models"
cp public/models/gesture_recognizer.task "$APP_ROOT/backend/models/"
# Python owns the model; ship no browser inference assets in the web component.
rm -rf "$APP_ROOT/web/vendor" "$APP_ROOT/web/models" "$APP_ROOT/web/inference-worker.js"

node scripts/validate-smartapp.mjs "$APP_ROOT"

ARCHIVE="$OUTPUT_DIR/$APP_ID-$VERSION.tar.gz"
TEMP_ARCHIVE="$STAGE_DIR/$APP_ID-$VERSION.tar.gz"
python3 - "$STAGE_DIR" "$APP_ID" "$TEMP_ARCHIVE" <<'PYARCHIVE'
import gzip, os, sys, tarfile
from pathlib import Path
stage, app_id, output = sys.argv[1:]
with open(output, 'wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as zipped:
    with tarfile.open(fileobj=zipped, mode='w') as archive:
        for path in sorted((Path(stage)/app_id).rglob('*')):
            info = archive.gettarinfo(str(path), str(path.relative_to(stage)))
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            if path.is_file():
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
PYARCHIVE
mv -f "$TEMP_ARCHIVE" "$ARCHIVE"

echo "SmartApp 包：$ARCHIVE"
python3 - "$ARCHIVE" <<'PYINFO'
import hashlib, sys
from pathlib import Path
p = Path(sys.argv[1])
print('packageSize=' + str(p.stat().st_size))
print('sha256=' + hashlib.sha256(p.read_bytes()).hexdigest())
PYINFO
