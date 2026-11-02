#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
"$PYTHON_BIN" -m venv .venv
if [[ "${1:-}" != "--runner-only" ]]; then
  .venv/bin/python -m pip install -r requirements.txt
fi
echo "Ready. Run: .venv/bin/python benchmark.py --help"
