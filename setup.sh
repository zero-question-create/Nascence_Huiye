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
if [ ! -f "$OLLAMA_BIN" ]; then
    echo "[*] Detecting OS..."
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    case "$OS" in
        Linux)  OLLAMA_URL="https://ollama.com/download/ollama-linux-${ARCH}.tgz" ;;
        Darwin) OLLAMA_URL="https://ollama.com/download/Ollama-darwin.zip" ;;
        *)      echo "[!] Unsupported OS: $OS"; exit 1 ;;
    esac
    echo "[*] Downloading Ollama for $OS ($ARCH)..."
    mkdir -p "$OLLAMA_DIR/bin" "$OLLAMA_DIR/temp"

    if [ "$OS" = "Linux" ]; then
        curl -fsSL "$OLLAMA_URL" -o "$OLLAMA_DIR/ollama.tgz"
        echo "[*] Extracting..."
        tar -xzf "$OLLAMA_DIR/ollama.tgz" -C "$OLLAMA_DIR/temp"
        find "$OLLAMA_DIR/temp" -name "ollama" -type f -exec cp {} "$OLLAMA_BIN" \;
        rm -rf "$OLLAMA_DIR/temp" "$OLLAMA_DIR/ollama.tgz"
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
        echo "[OK] Ollama installed to ollama/bin/"
    else
        echo "[!] ollama binary not found in downloaded archive."
        echo "[!] You can manually download from https://ollama.com"
    fi
else
    echo "[OK] Ollama already exists"
fi

# ---------- 4. Create data directories ----------
mkdir -p data/test
echo "[OK] Data directories created"

# ---------- 5. (Optional) Pull embedding model ----------
if [ -f "$OLLAMA_BIN" ]; then
    echo "[*] Starting Ollama and pulling embedding model..."
    export OLLAMA_HOME="$PROJECT_DIR/ollama/home"
    mkdir -p "$OLLAMA_HOME"

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
