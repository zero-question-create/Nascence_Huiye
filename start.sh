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
if [ ! -f "venv/bin/python" ]; then
    echo "[*] 虚拟环境未找到，正在创建..."
    if ! python3 -m venv venv --without-pip 2>/dev/null; then
        if ! python3 -m venv venv; then
            echo "[!] 创建虚拟环境失败，请检查 python3-venv 是否已安装" >&2
            exit 1
        fi
    fi
    source venv/bin/activate
    # 引导 pip（--without-pip 场景）
    if ! pip --version >/dev/null 2>&1; then
        curl -sS https://bootstrap.pypa.io/get-pip.py | python3 || {
            echo "[!] pip 引导失败，请手动安装 pip" >&2
            exit 1
        }
    fi
    pip install -r requirements.txt -q -i https://mirrors.huaweicloud.com/repository/pypi/simple/ \
        || { echo "[!] 华为源安装失败，尝试切换清华源..."; pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
             || { echo "[!] 清华源安装失败，尝试使用默认源（Python 官方源）..."; pip install -r requirements.txt -q -i https://pypi.org/simple/ \
                  || { echo "[!] 依赖安装失败，请检查 requirements.txt" >&2; exit 1; }; }; }
    echo "[√] 虚拟环境已创建，依赖已安装"
else
    echo "[√] 虚拟环境正常"
    source venv/bin/activate
fi

# ---------- 2. 确保数据目录 ----------
mkdir -p data/test

# ---------- 3. 确保 llama.cpp 与模型 ----------
if [ ! -f "llama/bin/llama-server" ]; then
    echo "[*] llama.cpp 未找到，正在下载..."
    bash "$PROJECT_DIR/run/install_llama.sh" || echo "[!] llama.cpp 安装失败，请手动安装"
fi
mkdir -p models

# ---------- 4. 端口 8787 预检（已在运行则直接开浏览器） ----------
if (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":8787 ") \
   || (command -v netstat >/dev/null 2>&1 && netstat -ltn 2>/dev/null | grep -q ":8787 "); then
    echo "[*] 检测到 8787 端口已被占用，服务可能已在运行，直接打开浏览器。"
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:8787/" >/dev/null 2>&1
        xdg-open "http://127.0.0.1:8787/admin" >/dev/null 2>&1
    elif command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:8787/"
        open "http://127.0.0.1:8787/admin"
    fi
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
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:8787/" >/dev/null 2>&1
        xdg-open "http://127.0.0.1:8787/admin" >/dev/null 2>&1
    elif command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:8787/"
        open "http://127.0.0.1:8787/admin"
    fi
) &

python3 webui.py
CODE=$?

if [ "$CODE" -ne 0 ]; then
    echo "[ERROR] 服务异常退出，代码: $CODE" >&2
else
    echo "[√] 服务已正常退出"
fi

exit "$CODE"
