#!/usr/bin/env sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -f "$APP_DIR/dist/index.html" ]; then
  echo '缺少 dist/index.html，请先在开发电脑执行 npm run build。' >&2
  exit 1
fi
echo "请在本机浏览器打开 http://localhost:${PORT:-8080}，按 Ctrl+C 停止。"
exec python3 -m http.server "${PORT:-8080}" --bind 127.0.0.1 --directory "$APP_DIR/dist"
