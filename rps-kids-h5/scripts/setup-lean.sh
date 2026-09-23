#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PYTHON_BIN=${RPS_SETUP_PYTHON:-python3.11}
if [ ! -x .venv-lean/bin/python ]; then
  "$PYTHON_BIN" -m venv .venv-lean
fi
.venv-lean/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 11), "精简 PC 环境当前验证版本为 Python 3.11"'
.venv-lean/bin/python -m pip install --no-deps -r requirements-lean.txt
.venv-lean/bin/python scripts/check-lean.py
printf '%s\n' '精简环境已就绪。运行 bash start-lean.sh，然后打开 http://127.0.0.1:5185/?debug=1'
