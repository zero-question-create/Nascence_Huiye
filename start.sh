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
    pip install -r requirements.txt -q -i https://mirrors.aliyun.com/pypi/simple/ \
        || { echo "[!] 阿里源安装失败，尝试切换清华源..."; pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
             || { echo "[!] 清华源安装失败，尝试使用默认源（Python 官方源）..."; pip install -r requirements.txt -q -i https://pypi.org/simple/; }; }
    echo "[√] 虚拟环境已创建，依赖已安装"
else
    echo "[√] 虚拟环境正常"
fi

source venv/bin/activate

# ---------- 2. 确保数据目录 ----------
mkdir -p data/test

# ---------- 3. 启动 Ollama ----------
OLLAMA_BIN="$PROJECT_DIR/ollama/bin/ollama"
OLLAMA_PID=""

start_ollama() {
    if [ -f "$OLLAMA_BIN" ]; then
        export OLLAMA_HOME="$PROJECT_DIR/ollama/home"
        mkdir -p "$OLLAMA_HOME"
        
        # 检查是否已有 Ollama 在运行
        if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo "[√] Ollama 服务已在运行"
            return 0
        fi
        
        echo "[*] 启动本地 Ollama 服务..."
        "$OLLAMA_BIN" serve > /dev/null 2>&1 &
        OLLAMA_PID=$!
        
        # 等待 Ollama 就绪
        for i in $(seq 1 30); do
            if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
                echo "[√] Ollama 服务已就绪"
                return 0
            fi
            sleep 1
        done
        echo "[!] Ollama 启动超时"
        return 1
    else
        echo "[!] Ollama 二进制未找到 ($OLLAMA_BIN)"
        echo "[!] 请确保系统已安装 Ollama 或重新运行 setup.sh"
        return 1
    fi
}

start_ollama || echo "[!] Ollama 启动失败，请手动启动"

# ---------- 4. 检查并拉取 Embedding 模型 ----------
if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    MODEL="shaw/dmeta-embedding-zh"
    if ! curl -s http://localhost:11434/api/tags | grep -q "dmeta-embedding-zh"; then
        echo "[*] 正在拉取 Embedding 模型: $MODEL ..."
        curl -s -X POST http://localhost:11434/api/pull -d "{\"model\":\"$MODEL\"}" > /dev/null 2>&1
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
echo "    3) 自我训练模式 (self_training.py)"
echo "=========================================="
echo ""
read -p "输入选择 (1/2/3，默认 1): " MODE_CHOICE
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
    3)
        echo "[*] 启动自我训练模式..."
        python3 self_training.py
        ;;
    *)
        echo "[!] 无效选择，启动 CLI 模式..."
        python3 main.py
        ;;
esac

cleanup
