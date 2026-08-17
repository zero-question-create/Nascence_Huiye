#!/bin/bash
# ============================================================
# NASCENCE 辉夜 - 一键启动脚本（Linux/macOS）
# 所有环境文件均位于项目文件夹内，不污染系统环境
# 启动后同时打开 客户端(/) 与 管理台(/admin) 两个标签页
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "=========================================="
echo "  NASCENCE 辉夜 - 启动 Web 服务"
echo "  客户端: http://127.0.0.1:8787/"
echo "  管理台: http://127.0.0.1:8787/admin"
echo "=========================================="

# ---------- 1. 检查 venv，缺失则自动创建并安装依赖 ----------
PY="$PROJECT_DIR/venv/bin/python"
if [ ! -f "$PY" ]; then
    echo "[*] 虚拟环境未找到，正在创建..."
    if ! python3 -m venv venv; then
        echo "[!] 创建虚拟环境失败，请先安装 python3-venv（如 apt install python3-venv）" >&2
        exit 1
    fi
fi
# 确保 venv 内有 pip（残缺 venv / 旧版 --without-pip 场景）
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "[*] venv 内未找到 pip，正在引导..."
    if ! "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
        curl -sS https://bootstrap.pypa.io/get-pip.py | "$PY" || {
            echo "[!] pip 引导失败，请手动安装 pip" >&2
            exit 1
        }
    fi
fi
# 统一用 venv 内 python -m pip（避免误用系统 pip 触发 PEP 668）
if ! "$PY" -m pip install -r requirements.txt -q -i https://mirrors.huaweicloud.com/repository/pypi/simple/ 2>/dev/null; then
    echo "[!] 华为源安装失败，尝试切换清华源..."
    if ! "$PY" -m pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null; then
        echo "[!] 清华源安装失败，尝试使用默认源（Python 官方源）..."
        "$PY" -m pip install -r requirements.txt -q -i https://pypi.org/simple/ \
            || { echo "[!] 依赖安装失败，请检查 requirements.txt" >&2; exit 1; }
    fi
fi
echo "[√] 虚拟环境已创建，依赖已安装"

# ---------- 2. 确保数据目录 ----------
mkdir -p data/test

# ---------- 3. 模型目录（不强制下载；首次使用本地模型时由服务端提示并下载） ----------
mkdir -p models
echo "[*] 提示：使用本地默认模型时，服务端会在启动模型时自动提示并下载所需 llama.cpp 与模型"

# ---------- 浏览器打开辅助：仅在有图形界面时尝试，无 GUI 只提示 URL ----------
open_url() {
    local url="$1"
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1
    elif [ "$(uname)" = "Darwin" ] && command -v open >/dev/null 2>&1; then
        open "$url" >/dev/null 2>&1
    else
        echo "  [提示] 未检测到图形界面，请手动在浏览器打开: $url"
    fi
}

# ---------- 4. 端口 8787 预检（已在运行则直接开浏览器） ----------
if (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":8787 ") \
   || (command -v netstat >/dev/null 2>&1 && netstat -ltn 2>/dev/null | grep -q ":8787 "); then
    echo "[*] 检测到 8787 端口已被占用，服务可能已在运行，直接打开浏览器。"
    open_url "http://127.0.0.1:8787/"
    open_url "http://127.0.0.1:8787/admin"
    exit 0
fi

# ---------- 5. 启动 WebUI（延迟打开两个标签页） ----------
echo ""
echo "=========================================="
echo "  正在启动 Web 服务..."
echo "  客户端: http://127.0.0.1:8787/"
echo "  管理台: http://127.0.0.1:8787/admin"
echo "=========================================="

# 延迟 2 秒等端口绑定后再打开浏览器
(
    sleep 2
    open_url "http://127.0.0.1:8787/"
    open_url "http://127.0.0.1:8787/admin"
) &

"$PY" webui.py
CODE=$?

if [ "$CODE" -ne 0 ]; then
    echo "[ERROR] 服务异常退出，代码: $CODE" >&2
else
    echo "[√] 服务已正常退出"
fi

exit "$CODE"
