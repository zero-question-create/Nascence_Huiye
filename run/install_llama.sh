#!/bin/bash
# ============================================================
# Nascence 辉夜 - llama.cpp 与 GGUF 模型下载（Linux）
#
# 安装内容：
#   1. llama.cpp 预编译 Linux CPU 二进制（含 llama-server）
#   2. 三个 GGUF 模型
#      - Qwen3-4B-Instruct-2507-Q4_K_M.gguf   （文本）
#      - qwen3-embed-0.6b-q8_0.gguf           （向量，0.6B=1024维；若与记忆库维度不一致，
#                                               启动后用控制面板「维护」页的维度重建工具全量重建）
#      - Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf   （多模态）
#      - Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf
#
# 幂等：已存在文件跳过。
# 模型来源：huggingface.co 直链 + hf-mirror.com 国内镜像，逐个尝试。
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

MODEL_DIR="$PROJECT_DIR/models"
LLAMA_DIR="$PROJECT_DIR/llama"
mkdir -p "$MODEL_DIR" "$LLAMA_DIR"

echo "=========================================="
echo "  Nascence Huiye - llama.cpp Installer"
echo "=========================================="

# ---------- 1. llama.cpp 二进制 ----------
LLAMA_BIN="$LLAMA_DIR/bin/llama-server"
if [ -f "$LLAMA_BIN" ]; then
    echo "[OK] llama-server already exists"
else
    echo "[*] Downloading llama.cpp (Linux CPU)..."
    # 从官方 GitHub Releases 选取 Linux x64 资产
    API="https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    ZIP="$LLAMA_DIR/llama.zip"
    TMP="$LLAMA_DIR/temp"
    ASSET_URL=$(curl -s -H "User-Agent: Nascence-Installer" "$API" \
        | python3 -c "import sys,json;
try:
  d=json.load(sys.stdin)
  a=[x for x in d.get('assets',[]) if x['name'].endswith('.zip') and 'ubuntu' in x['name'] and 'x64' in x['name']]
  print(a[0]['browser_download_url'] if a else '')
except Exception:
  print('')")
    if [ -n "$ASSET_URL" ] && curl -L --fail --retry 3 -o "$ZIP" "$ASSET_URL"; then
        mkdir -p "$TMP"
        unzip -o -q "$ZIP" -d "$TMP" || { echo "[!] unzip failed, trying python"; python3 -c "import zipfile;zipfile.ZipFile('$ZIP').extractall('$TMP')"; }
        SERVER=$(find "$TMP" -name "llama-server" -type f | head -1)
        if [ -n "$SERVER" ]; then
            mkdir -p "$LLAMA_DIR/bin"
            cp -r "$(dirname "$SERVER")/." "$LLAMA_DIR/bin/"
            chmod +x "$LLAMA_DIR/bin/llama-server"
            echo "[OK] llama.cpp installed to llama/bin/"
        else
            echo "[!] llama-server not found in archive."
        fi
        rm -f "$ZIP"; rm -rf "$TMP"
    else
        echo "[!] Failed to download llama.cpp. Please install llama.cpp manually and place llama-server in llama/bin/"
    fi
fi

# ---------- 2. 模型下载 ----------
# 数组格式: 目标文件名 | 最小预期大小(字节) | 源1(huggingface) | 源2(hf-mirror)
# 已存在文件若小于最小预期大小，视为下载不完整，删除重新下载。
MODELS=(
  "Qwen3-4B-Instruct-2507-Q4_K_M.gguf|2000000000|https://huggingface.co/DhruvalLabs/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf|https://hf-mirror.com/DhruvalLabs/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
  "qwen3-embed-0.6b-q8_0.gguf|500000000|https://huggingface.co/cstr/qwen3-embed-0.6b-GGUF/resolve/main/qwen3-embed-0.6b-q8_0.gguf|https://hf-mirror.com/cstr/qwen3-embed-0.6b-GGUF/resolve/main/qwen3-embed-0.6b-q8_0.gguf"
  "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf|1500000000|https://huggingface.co/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf|https://hf-mirror.com/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
  "Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf|1000000000|https://huggingface.co/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf|https://hf-mirror.com/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf"
)

for ENTRY in "${MODELS[@]}"; do
    IFS='|' read -r NAME MINSIZE URL1 URL2 <<< "$ENTRY"
    TARGET="$MODEL_DIR/$NAME"
    if [ -f "$TARGET" ]; then
        SIZE=$(stat -c%s "$TARGET" 2>/dev/null || stat -f%z "$TARGET")
        if [ "$SIZE" -ge "$MINSIZE" ]; then
            echo "[OK] $NAME already exists ($((SIZE/1024/1024)) MB)"
            continue
        fi
        echo "[!] $NAME 存在但大小异常 ($((SIZE/1024/1024)) MB)，判定为损坏，重新下载"
        rm -f "$TARGET"
    fi
    DOWNLOADED=0
    for URL in "$URL1" "$URL2"; do
        echo "[*] Downloading $NAME"
        echo "    from $URL"
        if curl -L --fail --retry 3 -C - -o "$TARGET" "$URL"; then
            SIZE=$(stat -c%s "$TARGET" 2>/dev/null || stat -f%z "$TARGET")
            if [ "$SIZE" -lt "$MINSIZE" ]; then
                echo "[!] $NAME download incomplete (below expected size)"
                rm -f "$TARGET"
                continue
            fi
            echo "[OK] $NAME downloaded ($((SIZE/1024/1024)) MB)"
            DOWNLOADED=1
            break
        else
            rm -f "$TARGET"
            echo "[!] 该源下载失败"
        fi
    done
    if [ "$DOWNLOADED" -ne 1 ]; then
        echo "[!] 所有源均失败，请手动下载 $NAME 放入 models/"
    fi
done

echo ""
echo "=========================================="
echo "  Installer finished."
echo "  llama.cpp : $LLAMA_DIR/bin"
echo "  models    : $MODEL_DIR"
echo "=========================================="
