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

# ---------- 3. Install llama.cpp + GGUF models ----------
echo "[*] Installing llama.cpp and GGUF models..."
bash "$PROJECT_DIR/run/install_llama.sh" || {
    echo "[!] llama.cpp 安装失败，可稍后手动运行 run/install_llama.sh"
}

# ---------- 4. Create data directories ----------
mkdir -p data/test
echo "[OK] Data directories created"

echo ""
echo "=========================================="
echo "  Setup complete! You can now run"
echo "  'bash start.sh' to start the project."
echo "=========================================="
