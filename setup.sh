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
PY="$PROJECT_DIR/venv/bin/python"
if [ ! -f "$PY" ]; then
    python3 -m venv venv || {
        echo "[!] 创建虚拟环境失败，请先安装 python3-venv（如 apt install python3-venv）" >&2
        exit 1
    }
    echo "[OK] Virtual environment created"
else
    echo "[OK] Virtual environment already exists"
fi

# 确保 venv 内有 pip（残缺 venv / 旧版 --without-pip 场景）
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "[*] venv 内未找到 pip，正在引导..."
    "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || curl -sS https://bootstrap.pypa.io/get-pip.py | "$PY" || {
        echo "[!] pip 引导失败，请手动安装 pip" >&2
        exit 1
    }
fi

# ---------- 2. Install dependencies ----------
echo "[*] Installing Python dependencies..."
# 统一用 venv 内 python -m pip（避免误用系统 pip 触发 PEP 668）
"$PY" -m pip install -r requirements.txt -q
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
