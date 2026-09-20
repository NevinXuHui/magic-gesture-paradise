#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON=${PYTHON:-python3}

if [ -n "${PYTHONPATH:-}" ]; then
    PYTHONPATH="$PROJECT_ROOT/src:$PYTHONPATH"
else
    PYTHONPATH="$PROJECT_ROOT/src"
fi
export PYTHONPATH

"$PYTHON" -m compileall -q "$PROJECT_ROOT/src" "$PROJECT_ROOT/tests"
"$PYTHON" -m unittest discover -s "$PROJECT_ROOT/tests" -p 'test_*.py' -v
