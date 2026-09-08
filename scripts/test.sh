#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"

PYTHONWARNINGS=error::ResourceWarning "$PYTHON_BIN" -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/audiospider-pycache" \
  "$PYTHON_BIN" -m compileall -q .
"$PYTHON_BIN" -m pip check
"$PYTHON_BIN" doctor.py
"$PYTHON_BIN" scripts/check_docs.py
bash -n download_zh_podcast.sh scripts/bootstrap_conda.sh scripts/test.sh scripts/probe.sh

echo "全部测试通过。"
