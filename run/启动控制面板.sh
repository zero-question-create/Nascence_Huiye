#!/bin/bash

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$PROJECT_DIR/run/启动控制面板.sh"

# 文件管理器双击执行时没有终端，主动创建一个可见终端。
if [ ! -t 0 ] && [ "${NASCENCE_IN_TERMINAL:-0}" != "1" ]; then
    exec gnome-terminal --wait --title="Nascence 辉夜" --env=NASCENCE_IN_TERMINAL=1 -- bash "$LAUNCHER"
fi

cd "$PROJECT_DIR"

# PySide6 的 X11 插件需要 libxcb-cursor；使用项目内副本，不修改系统库。
LOCAL_LIB="$PROJECT_DIR/run/lib/usr/lib/x86_64-linux-gnu"
if [ -d "$LOCAL_LIB" ]; then
    export LD_LIBRARY_PATH="$LOCAL_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# 使用项目内 Fcitx Qt6 插件连接系统正在运行的搜狗输入法。
export QT_IM_MODULE=fcitx
export GTK_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export QT_PLUGIN_PATH="$PROJECT_DIR/run/qt-plugins"

cleanup() {
    if [ -n "${PANEL_PID:-}" ]; then
        kill -TERM "$PANEL_PID" 2>/dev/null || true
        wait "$PANEL_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM HUP

echo "=========================================="
echo " Nascence 辉夜控制面板"
echo " 项目目录: $PROJECT_DIR"
echo " 关闭此终端即停止全部项目服务"
echo "=========================================="

if [ ! -x "$PROJECT_DIR/venv/bin/python" ]; then
    echo "未找到项目虚拟环境，正在执行 setup.sh..."
    bash "$PROJECT_DIR/setup.sh"
fi

"$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/control_panel.py" &
PANEL_PID=$!
wait "$PANEL_PID"
PANEL_PID=""

echo "控制面板已退出。"
