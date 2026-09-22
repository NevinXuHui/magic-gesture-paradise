#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON_BIN=${RPS_PYTHON:-.venv/bin/python}
test -x "$PYTHON_BIN" || { echo '先安装 Python 环境，参见 README.md'; exit 1; }
exec "$PYTHON_BIN" server.py --port "${PORT:-5174}" "$@"
