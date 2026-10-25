#!/bin/bash
set -e

# ==============================================================================
# Nascence Huiye - Docker Entrypoint Script
# 容器启动预检与自动初始化
# ==============================================================================

# 确保运行时数据与日志目录存在
mkdir -p /app/data/test /app/run/logs /app/config

# 若宿主机挂载了空 config 卷，自动从镜像内置备份还原核心代码与示例配置
if [ ! -f /app/config/api_config.py ] && [ -d /app/_config_backup ]; then
    echo "[*] Initializing config files from image template..."
    cp -n /app/_config_backup/* /app/config/ 2>/dev/null || true
fi

# 若缺少 QQ 白名单配置文件，自动从 example 复制生成
if [ ! -f /app/config/qq_manifest.json ] && [ -f /app/config/qq_manifest.example.json ]; then
    echo "[*] Creating default qq_manifest.json from example..."
    cp /app/config/qq_manifest.example.json /app/config/qq_manifest.json
fi

# 若缺少插件配置（含行为 LLM），自动从 example 复制生成
if [ ! -f /app/config/plugins.json ] && [ -f /app/config/plugins.example.json ]; then
    echo "[*] Creating default plugins.json from example..."
    cp /app/config/plugins.example.json /app/config/plugins.json
fi

# 微信清单 / Bilibili 凭据：没有则从示例创建空模板
if [ ! -f /app/config/wechat_manifest.json ] && [ -f /app/config/wechat_manifest.example.json ]; then
    echo "[*] Creating default wechat_manifest.json from example..."
    cp /app/config/wechat_manifest.example.json /app/config/wechat_manifest.json
fi
if [ ! -f /app/config/bilibili_credentials.json ] && [ -f /app/config/bilibili.example.json ]; then
    echo "[*] Creating default bilibili_credentials.json from example..."
    cp /app/config/bilibili.example.json /app/config/bilibili_credentials.json
fi

exec "$@"
