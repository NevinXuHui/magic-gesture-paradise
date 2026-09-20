#!/bin/sh
set -eu
cd "$(dirname "$0")"
test -f dist/index.html || { echo '请先运行 npm run build'; exit 1; }
exec python3 -m http.server "${PORT:-5174}" --bind 0.0.0.0 --directory dist
