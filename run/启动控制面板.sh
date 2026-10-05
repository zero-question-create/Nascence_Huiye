#!/bin/bash

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

cleanup() {
    if [ -n "${PANEL_PID:-}" ]; then
        kill -TERM "$PANEL_PID" 2>/dev/null || true
        wait "$PANEL_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM HUP

echo "=========================================="
echo " Nascence 辉夜 Web 控制面板"
echo " 项目目录: $PROJECT_DIR"
echo " 关闭此终端即停止全部项目服务"
echo "=========================================="

if [ ! -x "$PROJECT_DIR/venv/bin/python" ]; then
    echo "未找到项目虚拟环境，正在执行 setup.sh..."
    bash "$PROJECT_DIR/setup.sh"
fi

echo "[*] 正在启动 Web 控制面板..."
OPEN_ARG=""
if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    OPEN_ARG="--open"
fi

"$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/web_panel.py" $OPEN_ARG "$@" &
PANEL_PID=$!
wait "$PANEL_PID"
PANEL_PID=""

echo "控制面板已退出。"
