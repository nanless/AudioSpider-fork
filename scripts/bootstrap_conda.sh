#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_NAME="${CONDA_ENV_NAME:-audiospider}"

if [[ -n "${CONDA_EXE:-}" && -x "$CONDA_EXE" ]]; then
  CONDA_BIN="$CONDA_EXE"
elif command -v conda >/dev/null 2>&1; then
  CONDA_BIN="$(command -v conda)"
elif [[ -x /root/miniforge3/bin/conda ]]; then
  CONDA_BIN=/root/miniforge3/bin/conda
else
  echo "未找到 conda。请先安装 Miniforge/Miniconda，或设置 CONDA_EXE。" >&2
  exit 1
fi

if "$CONDA_BIN" env list | awk '{print $1}' | grep -Fxq "$ENV_NAME"; then
  echo "更新 Conda 环境: $ENV_NAME"
  "$CONDA_BIN" env update -n "$ENV_NAME" -f "$ROOT_DIR/environment.yml"
else
  echo "创建 Conda 环境: $ENV_NAME"
  "$CONDA_BIN" env create -n "$ENV_NAME" -f "$ROOT_DIR/environment.yml"
fi

echo
echo "完成。激活命令："
echo "  source \"$($CONDA_BIN info --base)/etc/profile.d/conda.sh\""
echo "  conda activate $ENV_NAME"
