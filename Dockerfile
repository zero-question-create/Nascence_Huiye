# ==============================================================================
# Nascence Huiye - Dockerfile
# AI人格增强底座容器镜像
# ==============================================================================

FROM python:3.11-slim-bookworm

# 基础环境变量：禁止生成 pyc、输出无缓冲、设置时区
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 安装必要的系统运行时工具：
# - ffmpeg: 多模态语音识别与格式转码
# - ca-certificates: HTTPS 证书校验
# - curl: 健康检查与 Ollama 探测
# - tzdata: 时区支持
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件并安装，利用 Docker 镜像层缓存
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 复制项目代码（敏感文件与本地数据由 .dockerignore 严格排除）
COPY . ./

# 创建运行时数据与日志目录，并配置非 root 安全用户
RUN mkdir -p /app/data/test /app/run/logs /app/config \
    && useradd -u 1000 -U -d /app -s /bin/bash huiye \
    && chown -R huiye:huiye /app

USER huiye

# 暴露端口：
# - 32123: Web 控制面板
# - 6700: NapCat 反向 WebSocket 服务端口
EXPOSE 32123 6700

# 容器健康检查（探测控制面板接口状态）
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -f http://127.0.0.1:32123/api/stats || exit 1

# 默认启动 Web 控制面板（绑定 0.0.0.0 支持宿主机端口映射）
CMD ["python", "web_panel.py", "--host", "0.0.0.0", "--port", "32123"]
