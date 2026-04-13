#!/usr/bin/env bash
set -euo pipefail

PYTHON_EXE="python3"
VENV_DIR=".venv"
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
    --host)
      BIND_HOST="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --no-start)
      NO_START="1"
      shift
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
  echo "[start] $1"
}

VENV_PYTHON="$PROJECT_ROOT/$VENV_DIR/bin/python"
if [[ -x "$VENV_PYTHON" ]]; then
  PYTHON_CMD="$VENV_PYTHON"
  step "使用虚拟环境解释器: $VENV_PYTHON"
else
  PYTHON_CMD="$PYTHON_EXE"
  step "使用系统解释器: $PYTHON_EXE"
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
  step "参数检查完成（no-start），未启动服务"
  exit 0
fi

step "启动 Web 面板服务: server.py"
"$PYTHON_CMD" "$PROJECT_ROOT/server.py"
