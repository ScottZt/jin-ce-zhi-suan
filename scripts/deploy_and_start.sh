#!/usr/bin/env bash
set -euo pipefail

PYTHON_EXE="python3"
VENV_DIR=".venv"
SKIP_VENV="0"
SKIP_INSTALL="0"
NO_START="0"
BIND_HOST=""
PORT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_EXE="${2:-}"
      shift 2
      ;;
    --venv-dir)
      VENV_DIR="${2:-}"
      shift 2
      ;;
    --skip-venv)
      SKIP_VENV="1"
      shift
      ;;
    --skip-install)
      SKIP_INSTALL="1"
      shift
      ;;
    --no-start)
      NO_START="1"
      shift
      ;;
    --host)
      BIND_HOST="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

step() {
  echo "[deploy] $1"
}

if [[ "$SKIP_VENV" != "1" ]]; then
  VENV_PYTHON="$PROJECT_ROOT/$VENV_DIR/bin/python"
  if [[ ! -x "$VENV_PYTHON" ]]; then
    step "创建虚拟环境: $PROJECT_ROOT/$VENV_DIR"
    "$PYTHON_EXE" -m venv "$PROJECT_ROOT/$VENV_DIR"
  else
    step "复用已有虚拟环境: $PROJECT_ROOT/$VENV_DIR"
  fi
  PYTHON_CMD="$VENV_PYTHON"
else
  step "跳过虚拟环境，使用解释器: $PYTHON_EXE"
  PYTHON_CMD="$PYTHON_EXE"
fi

if [[ "$SKIP_INSTALL" != "1" ]]; then
  step "升级 pip"
  "$PYTHON_CMD" -m pip install --upgrade pip
  step "安装 requirements.txt"
  "$PYTHON_CMD" -m pip install -r "$PROJECT_ROOT/requirements.txt"
  step "安装 WebSocket 运行依赖 uvicorn[standard]"
  "$PYTHON_CMD" -m pip install "uvicorn[standard]"
else
  step "跳过依赖安装"
fi

if [[ -n "$BIND_HOST" ]]; then
  export SERVER_HOST="$BIND_HOST"
  step "设置 SERVER_HOST=$BIND_HOST"
fi

if [[ -n "$PORT" ]]; then
  export SERVER_PORT="$PORT"
  step "设置 SERVER_PORT=$PORT"
fi

if [[ "$NO_START" == "1" ]]; then
  step "部署步骤完成（no-start），未启动服务"
  exit 0
fi

step "启动 Web 面板服务: server.py"
"$PYTHON_CMD" "$PROJECT_ROOT/server.py"
