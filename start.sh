#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "============================================"
echo "   moz - 一键启动"
echo "============================================"
echo

if [[ ! -f ".env" ]]; then
    echo "[错误] 未找到 .env 文件，请先复制 .env.example 并填写配置。"
    exit 1
fi

PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    if command -v python3.13 >/dev/null 2>&1; then
        echo "[1/4] 正在创建 Python 3.13 虚拟环境..."
        python3.13 -m venv .venv
    elif command -v python3 >/dev/null 2>&1; then
        if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; then
            echo "[错误] 系统 Python 版本过低，请安装 Python 3.13 或更高版本。"
            exit 1
        fi
        echo "[1/4] 正在使用系统 Python 创建虚拟环境..."
        python3 -m venv .venv
    else
        echo "[错误] 未找到 Python，请安装 Python 3.13 或更高版本。"
        exit 1
    fi
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; then
    echo "[错误] $PYTHON 不是 Python 3.11+ 解释器。请重建 .venv。"
    exit 1
fi

echo "      检查后端依赖..."
if ! "$PYTHON" -c 'import fastapi' >/dev/null 2>&1; then
    echo "      安装后端依赖..."
    "$PYTHON" -m pip install -r requirements.txt
fi

if ! command -v node >/dev/null 2>&1; then
    echo "[错误] 未找到 Node.js，请安装 Node.js 22 LTS。"
    exit 1
fi

if [[ ! -d "frontend/node_modules" ]]; then
    echo "[2/4] 按 package-lock.json 安装前端依赖..."
    (cd frontend && npm ci --no-audit --no-fund)
fi

echo "[3/4] 启动后端服务 (端口 8000)..."
cd backend
"$PYTHON" -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!
cd ..

echo "[4/4] 启动前端服务 (端口 3000)..."
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

echo
echo "============================================"
echo "   启动完成！"
echo "   后端: http://127.0.0.1:8000"
echo "   前端: http://127.0.0.1:3000"
echo "============================================"
echo

cleanup() {
    echo "正在停止服务..."
    kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup SIGINT SIGTERM

wait
