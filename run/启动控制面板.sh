#!/bin/bash

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$PROJECT_DIR/run/启动控制面板.sh"

# 文件管理器双击执行时没有终端，主动创建一个可见终端。
if [ ! -t 0 ] && [ "${NASCENCE_IN_TERMINAL:-0}" != "1" ]; then
    exec gnome-terminal --wait --title="Nascence 辉夜" --env=NASCENCE_IN_TERMINAL=1 -- bash "$LAUNCHER"
fi

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

echo "[*] 正在启动 Web 控制面板（http://127.0.0.1:8080）..."
"$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/web_panel.py" --port 8080 --open &
PANEL_PID=$!
wait "$PANEL_PID"
PANEL_PID=""

echo "控制面板已退出。"
