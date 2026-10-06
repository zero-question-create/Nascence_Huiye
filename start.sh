#!/bin/bash
# ============================================================
# Nascence 辉夜 一键启动脚本
# 所有环境/文件均位于项目文件夹内，不污染系统环境
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "=========================================="
echo "  Nascence 辉夜 - 启动中..."
echo "=========================================="

# ---------- 1. 检查 venv ----------
if [ ! -f "venv/bin/python3" ]; then
    echo "[!] 虚拟环境未找到，正在创建..."
    python3 -m venv venv --without-pip
    source venv/bin/activate
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3 > /dev/null 2>&1
    pip install -r requirements.txt -q -i https://mirrors.huaweicloud.com/repository/pypi/simple/ \
        || { echo "[!] 华为源安装失败，尝试切换清华源..."; pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
             || { echo "[!] 清华源安装失败，尝试使用默认源（Python 官方源）..."; pip install -r requirements.txt -q -i https://pypi.org/simple/; }; }
    echo "[√] 虚拟环境已创建，依赖已安装"
else
    echo "[√] 虚拟环境正常"
fi

source venv/bin/activate

# ---------- 2. 确保数据目录 ----------
mkdir -p data/test

# ---------- 3. 启动 Ollama ----------
OLLAMA_TARGET_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
OLLAMA_BIN="${OLLAMA_BIN:-$PROJECT_DIR/ollama/bin/ollama}"
if [ ! -f "$OLLAMA_BIN" ] && command -v ollama >/dev/null 2>&1; then
    OLLAMA_BIN="$(command -v ollama)"
fi
OLLAMA_PID=""

start_ollama() {
    # 优先检查指定或默认的 Ollama 服务是否已在运行
    if curl -s "$OLLAMA_TARGET_URL/api/tags" > /dev/null 2>&1; then
        echo "[√] Ollama 服务已在运行 ($OLLAMA_TARGET_URL)"
        return 0
    fi

    if [ -f "$OLLAMA_BIN" ]; then
        # 与 setup.sh / 控制面板统一模型目录，避免装好的模型运行时找不到
        export OLLAMA_HOME="${OLLAMA_HOME:-$PROJECT_DIR/ollama/home}"
        export OLLAMA_MODELS="${OLLAMA_MODELS:-$PROJECT_DIR/ollama/home/models}"
        mkdir -p "$OLLAMA_MODELS"
        
        echo "[*] 启动本地 Ollama 服务 ($OLLAMA_BIN)..."
        "$OLLAMA_BIN" serve > /dev/null 2>&1 &
        OLLAMA_PID=$!
        
        # 等待 Ollama 就绪
        for i in $(seq 1 30); do
            if curl -s "$OLLAMA_TARGET_URL/api/tags" > /dev/null 2>&1; then
                echo "[√] Ollama 服务已就绪"
                return 0
            fi
            sleep 1
        done
        echo "[!] Ollama 启动超时"
        return 1
    else
        echo "[*] 未找到本地 Ollama 二进制，且 $OLLAMA_TARGET_URL 未就绪"
        echo "[*] 若使用外部或宿主机 Ollama，请确保已启动服务并配置 OLLAMA_BASE_URL"
        return 0
    fi
}

start_ollama || echo "[!] Ollama 启动检查结束"

# ---------- 4. 检查并拉取 Embedding 模型 ----------
if curl -s "$OLLAMA_TARGET_URL/api/tags" > /dev/null 2>&1; then
    MODEL="${OLLAMA_EMBED_MODEL:-shaw/dmeta-embedding-zh}"
    if ! curl -s "$OLLAMA_TARGET_URL/api/tags" | grep -q "dmeta-embedding-zh"; then
        echo "[*] 正在拉取 Embedding 模型: $MODEL ..."
        curl -s -X POST "$OLLAMA_TARGET_URL/api/pull" -d "{\"model\":\"$MODEL\"}" > /dev/null 2>&1
        echo "[√] Embedding 模型已就绪"
    else
        echo "[√] Embedding 模型已存在"
    fi
fi

# ---------- 5. 启动模式选择 ----------
cleanup() {
    echo ""
    echo "[*] 正在关闭服务..."
    if [ -n "$OLLAMA_PID" ]; then
        kill "$OLLAMA_PID" 2>/dev/null || true
    fi
    echo "[√] 已退出"
    exit 0
}
trap cleanup SIGINT SIGTERM

echo ""
echo "=========================================="
echo "  请选择启动模式:"
echo "    1) CLI 命令行交互模式 (main.py)"
echo "    2) QQ Bot 模式 (qq_bot.py)"
echo "=========================================="
echo ""
read -p "输入选择 (1/2，默认 1): " MODE_CHOICE
MODE_CHOICE=${MODE_CHOICE:-1}

case "$MODE_CHOICE" in
    1)
        echo "[*] 启动 CLI 模式..."
        python3 main.py
        ;;
    2)
        echo "[*] 启动 QQ Bot 模式..."
        python3 qq_bot.py
        ;;
    *)
        echo "[!] 无效选择，启动 CLI 模式..."
        python3 main.py
        ;;
esac

cleanup
