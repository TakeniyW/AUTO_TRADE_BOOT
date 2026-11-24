#!/usr/bin/env bash
# 刘一策略 · 合约交易终端 启动脚本
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

if [ ! -d ".venv" ]; then
  echo "[run] 创建虚拟环境 .venv"
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[run] 安装依赖"
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

echo "[run] 启动服务： http://127.0.0.1:${PORT}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
