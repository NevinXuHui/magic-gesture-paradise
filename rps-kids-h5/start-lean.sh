#!/bin/sh
set -eu
cd "$(dirname "$0")"
test -x .venv-lean/bin/python || { echo '请先运行 bash scripts/setup-lean.sh'; exit 1; }
exec .venv-lean/bin/python server.py --port "${PORT:-5185}" "$@"
