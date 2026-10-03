#!/bin/bash
# ============================================================
# Nascence Huiye Environment Setup (Linux/macOS)
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "=========================================="
echo "  Nascence Huiye - Environment Setup"
echo "=========================================="

# ---------- 1. Create venv ----------
echo "[*] Creating Python virtual environment..."
if [ ! -f "venv/bin/python" ]; then
    python3 -m venv venv
    echo "[OK] Virtual environment created"
else
    echo "[OK] Virtual environment already exists"
fi

PIP="$PROJECT_DIR/venv/bin/pip"

# ---------- 2. Install dependencies ----------
echo "[*] Installing Python dependencies..."
$PIP install -r requirements.txt -q
if [ $? -ne 0 ]; then
    echo "[!] Dependency installation failed. Check requirements.txt."
    read -p "Press Enter to exit"
    exit 1
fi
echo "[OK] Python dependencies installed"

# ---------- 3. Download Ollama (Linux/macOS) ----------
OLLAMA_DIR="$PROJECT_DIR/ollama"
OLLAMA_BIN="$OLLAMA_DIR/bin/ollama"
# 模型目录统一：所有入口（setup/start/控制面板）都用 OLLAMA_MODELS 指向 ollama/home/models。
# 此前 setup/start 只设 OLLAMA_HOME、面板另设 OLLAMA_MODELS，
# 二者可能指向不同位置，导致"装好的模型运行时找不到"。
export OLLAMA_HOME="${OLLAMA_HOME:-$OLLAMA_DIR/home}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$OLLAMA_DIR/home/models}"
if [ ! -f "$OLLAMA_BIN" ]; then
    echo "[*] Detecting OS..."
    OS="$(uname -s)"
    ARCH_RAW="$(uname -m)"
    case "$ARCH_RAW" in
        x86_64|amd64)  ARCH="amd64" ;;
        aarch64|arm64) ARCH="arm64" ;;
        *)             ARCH="$ARCH_RAW" ;;
    esac
    case "$OS" in
        Linux)
            # 官方 Linux 包现为 .tar.zst，且架构名用 amd64/arm64（不是 uname 的 x86_64）
            OLLAMA_URL="https://github.com/ollama/ollama/releases/latest/download/ollama-linux-${ARCH}.tar.zst"
            ;;
        Darwin) OLLAMA_URL="https://ollama.com/download/Ollama-darwin.zip" ;;
        *)      echo "[!] Unsupported OS: $OS"; exit 1 ;;
    esac
    echo "[*] Downloading Ollama for $OS ($ARCH_RAW → $ARCH)..."
    mkdir -p "$OLLAMA_DIR/bin" "$OLLAMA_DIR/temp"

    if [ "$OS" = "Linux" ]; then
        if ! curl -fsSL "$OLLAMA_URL" -o "$OLLAMA_DIR/ollama.tar.zst"; then
            echo "[!] 下载失败：$OLLAMA_URL"
            echo "[!] 请检查网络，或手动从 https://github.com/ollama/ollama/releases 下载对应架构包，"
            echo "[!] 解压后把 bin/ 与 lib/ 放到 $OLLAMA_DIR/ 下。"
            rm -rf "$OLLAMA_DIR/temp"
            exit 1
        fi
        echo "[*] Extracting..."
        # 优先 zstd（官方现用格式），缺失时回退尝试 tar 直解（旧版 .tgz）
        if command -v zstd >/dev/null 2>&1; then
            tar --use-compress-program=unzstd -xf "$OLLAMA_DIR/ollama.tar.zst" -C "$OLLAMA_DIR/temp"
        else
            echo "[!] 未找到 zstd，尝试按 tar 直接解压（若失败请先安装 zstd：apt install zstd）"
            tar -xf "$OLLAMA_DIR/ollama.tar.zst" -C "$OLLAMA_DIR/temp" || {
                echo "[!] 解压失败。请安装 zstd 后重试。"; exit 1; }
        fi
        # 保留完整目录结构：ollama 依赖同包的运行库（lib/），
        # 只复制单个二进制会丢掉推理所需库文件。
        mkdir -p "$OLLAMA_DIR/bin" "$OLLAMA_DIR/lib"
        find "$OLLAMA_DIR/temp" -name "ollama" -type f -exec cp {} "$OLLAMA_BIN" \; 2>/dev/null || true
        if [ -d "$OLLAMA_DIR/temp/lib" ]; then
            cp -R "$OLLAMA_DIR/temp/lib/." "$OLLAMA_DIR/lib/"
        fi
        rm -rf "$OLLAMA_DIR/temp" "$OLLAMA_DIR/ollama.tar.zst"
    elif [ "$OS" = "Darwin" ]; then
        curl -fsSL "$OLLAMA_URL" -o "$OLLAMA_DIR/ollama.zip"
        echo "[*] Extracting..."
        unzip -q "$OLLAMA_DIR/ollama.zip" -d "$OLLAMA_DIR/temp"
        find "$OLLAMA_DIR/temp" -name "ollama" -type f -exec cp {} "$OLLAMA_BIN" \; 2>/dev/null || \
        find "$OLLAMA_DIR/temp" -name "Ollama.app" -type d -exec cp -R {} "$OLLAMA_DIR/Ollama.app" \;
        rm -rf "$OLLAMA_DIR/temp" "$OLLAMA_DIR/ollama.zip"
    fi

    chmod +x "$OLLAMA_BIN" 2>/dev/null || true
    if [ -f "$OLLAMA_BIN" ]; then
        echo "[OK] Ollama installed to ollama/bin/ (models: $OLLAMA_MODELS)"
    else
        echo "[!] ollama binary not found in downloaded archive."
        echo "[!] You can manually download from https://ollama.com"
    fi
else
    echo "[OK] Ollama already exists (models: $OLLAMA_MODELS)"
fi

# ---------- 4. Create data directories ----------
mkdir -p data/test
echo "[OK] Data directories created"

# ---------- 5. (Optional) Pull embedding model ----------
if [ -f "$OLLAMA_BIN" ]; then
    echo "[*] Starting Ollama and pulling embedding model..."
    # OLLAMA_HOME / OLLAMA_MODELS 已在第 3 步统一设置，这里只需确保目录存在
    mkdir -p "$OLLAMA_MODELS"

    # 后台启动 Ollama
    "$OLLAMA_BIN" serve &
    OLLAMA_PID=$!
    sleep 3

    MODEL="shaw/dmeta-embedding-zh"
    # 检查模型是否已存在
    if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
        EXISTS=$(curl -sf http://localhost:11434/api/tags | python3 -c "import sys,json; data=json.load(sys.stdin); print(any('dmeta-embedding-zh' in m.get('name','') for m in data.get('models',[])))" 2>/dev/null)
        if [ "$EXISTS" != "True" ]; then
            echo "[*] Pulling model $MODEL (about 400MB, first time may be slow)..."
            curl -sf http://localhost:11434/api/pull -d "{\"model\": \"$MODEL\"}" > /dev/null 2>&1
            echo "[OK] Embedding model pulled"
        else
            echo "[OK] Embedding model already exists"
        fi
    else
        echo "[!] Ollama not responding, skipping model pull."
    fi

    kill "$OLLAMA_PID" 2>/dev/null || true
    wait "$OLLAMA_PID" 2>/dev/null || true
    echo "[OK] Ollama service stopped"
else
    echo "[*] Ollama not installed, skipping model pull."
fi

echo ""
echo "=========================================="
echo "  Setup complete! You can now run"
echo "  'bash start.sh' to start the project."
echo "=========================================="
