#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_NAME="${AUDIOSPIDER_OCR_ENV_NAME:-audiospider-ocr}"

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
  "$CONDA_BIN" env update -n "$ENV_NAME" -f "$ROOT_DIR/environment-ocr.yml"
else
  "$CONDA_BIN" env create -n "$ENV_NAME" -f "$ROOT_DIR/environment-ocr.yml"
fi

OCR_PYTHON="$($CONDA_BIN run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)')"
"$OCR_PYTHON" -m pip install \
  paddlepaddle-gpu==3.3.0 \
  --index-url https://www.paddlepaddle.org.cn/packages/stable/cu118/
"$OCR_PYTHON" -m pip install \
  --index-url https://mirrors.aliyun.com/pypi/simple \
  --trusted-host mirrors.aliyun.com \
  --resume-retries 30 --timeout 120 \
  opencv-contrib-python==4.10.0.84
"$OCR_PYTHON" -m pip install \
  --resume-retries 30 --timeout 120 \
  -r "$ROOT_DIR/requirements-lock.txt" \
  -r "$ROOT_DIR/requirements-ocr-lock.txt"

# PaddleOCR's documented behavior is to fetch official models when no local
# cache exists. Do it here as an explicit setup step instead of surprising a
# long-running data job. ModelScope hosts PaddlePaddle's official model repos;
# bypass only the flaky health check, not TLS or model-content validation.
PADDLE_PDX_MODEL_SOURCE=modelscope PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
  PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  "$OCR_PYTHON" -c \
  'from bilibili_visual_ocr_paddle import create_paddle_engine; create_paddle_engine(device="gpu:0", allow_model_download=True); print("PaddleOCR 模型缓存预热完成")'

AUDIOSPIDER_VISUAL_OCR_PYTHON="$OCR_PYTHON" \
  "$OCR_PYTHON" "$ROOT_DIR/doctor.py" --ocr --db "$ROOT_DIR/audiospider.db" \
  --download-dir "$ROOT_DIR/downloads"

echo "OCR 环境已就绪：$ENV_NAME"
