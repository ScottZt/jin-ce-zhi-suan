#!/bin/bash
# 金策智算启动脚本 - WSL 版
# 使用 Windows 原生 Python 启动网关服务

set -e

PROJECT_DIR="/mnt/d/jin-ce-zhi-suan"
WIN_PYTHON="/mnt/c/Users/Administrator.DESKTOP-DK7FP95/AppData/Local/Python/bin/python.exe"
SERVER_SCRIPT="D:\\jin-ce-zhi-suan\\server.py"
PORT=8000

echo "=================================="
echo "  金策智算网关启动程序"
echo "=================================="
echo ""

# 检查通达信客户端是否运行
echo "[1/4] 检查通达信客户端..."
if pgrep -f "tdx" > /dev/null 2>&1; then
    echo "      ✅ 通达信客户端已运行"
else
    echo "      ⚠️  通达信客户端未运行，请先启动 D:\\new_tdx\\"
fi

# 检查端口是否被占用
echo "[2/4] 检查端口 $PORT..."
if netstat -tuln 2>/dev/null | grep -q ":$PORT "; then
    echo "      ⚠️  端口 $PORT 已被占用，尝试自动切换端口"
fi

# 启动服务
echo "[3/4] 启动网关服务..."
echo "      使用 Python: $WIN_PYTHON"
echo "      服务脚本: $SERVER_SCRIPT"
echo ""

cd "$PROJECT_DIR"

# 使用 nohup 在后台启动
nohup "$WIN_PYTHON" "$SERVER_SCRIPT" > "$PROJECT_DIR/server.log" 2>&1 &
PID=$!

echo "      服务进程 PID: $PID"
echo ""

# 等待服务启动
echo "[4/4] 等待服务就绪..."
for i in {1..30}; do
    if curl -s http://localhost:$PORT > /dev/null 2>&1; then
        echo ""
        echo "=================================="
        echo "  ✅ 金策智算网关启动成功！"
        echo "=================================="
        echo ""
        echo "  📊 访问地址: http://localhost:$PORT"
        echo "  📁 项目目录: $PROJECT_DIR"
        echo "  📝 日志文件: $PROJECT_DIR/server.log"
        echo ""
        echo "  常用命令:"
        echo "    查看日志: tail -f $PROJECT_DIR/server.log"
        echo "    停止服务: kill $PID"
        echo ""
        exit 0
    fi
    sleep 1
    echo -n "."
done

echo ""
echo "❌ 服务启动超时，请检查日志: $PROJECT_DIR/server.log"
exit 1
