# config/api_config.py
# ========================================================================
# 全局配置加载模块
#
# 本项目所有需要"运行时可改"的设置统一存放在 api_config.json（已 gitignore）：
#   - 三个模型后端（text / embed / multimodal）各自独立决定：
#       * 是否使用本地默认模型（llama.cpp 加载的 GGUF）
#       * 若不使用默认模型，则填写外部 API 的 base_url / key / model
#
# 关键能力：
#   - load_config()    : 从磁盘读取配置并补齐默认值
#   - reload_config()  : 重读磁盘并原地更新全局 config dict，让运行中的模块
#                        立即感知最新设置（无需重启进程）
#   - save_config()    : 把整个配置写回磁盘
# ========================================================================

import json
import os


def normalize_proxy_environment():
    """让 httpx/OpenAI 能识别常见代理工具导出的 socks scheme，并保护本地服务不走代理。

    - 把 socks:// 前缀归一化为 socks5://（httpx 只认识 socks5://）
    - 强制将 localhost / 127.0.0.1 / ::1 加入 NO_PROXY，
      避免 llama.cpp 等本机服务被代理劫持导致连不上。
    """
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        value = os.environ.get(name)
        if value and value.lower().startswith("socks://"):
            os.environ[name] = "socks5://" + value[len("socks://"):]

    # 保护本地服务（llama.cpp 等）不经过代理
    local_entries = {"localhost", "127.0.0.1", "::1"}
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    current = {e.strip() for e in no_proxy.replace(",", " ").replace(";", " ").split() if e.strip()}
    if not local_entries.issubset(current):
        merged = ", ".join(sorted(current | local_entries))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged


normalize_proxy_environment()

# 配置文件路径（与 __file__ 同级，保证任意工作目录下都能定位）
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_config.json")

# ------------------------------------------------------------------------
# 默认配置
# ------------------------------------------------------------------------
# 三个模型后端：
#   text        : 文本理解与生成（Qwen3-4B GGUF 或外部 API）
#   embed       : 语义向量（qwen3-embedding；用户明确"embedding 不 GGUF 也可以"，
#                 因此本地优先用 llama.cpp 加载 GGUF，无法加载时回落外部 API）
#   multimodal  : 多模态（图片）理解（Qwen2.5-VL-3B GGUF 或外部 API；
#                 按需求已舍弃音视频，仅保留图片理解）
# 每个后端字段：
#   use_default : True=使用本地默认模型（启动时检查并自动下载后由 llama.cpp 加载）
#                 False=使用下方填写的 API（默认值，避免误触发大文件下载）
#   api_base_url/api_key/api_model : 外部 API 三要素（use_default=False 时必填）
DEFAULT_CONFIG = {
    "bot_name": "辉夜",   # bot 名字（保存后需重启进程生效）
    "embed_dim": 768,     # 语义向量维度（与 embed 模型输出一致；由「维护」页维度重建工具更新）
    "server_port": 8787,  # 服务端(管理面板)监听端口
    "client_port": 8987,  # 网页版客户端独立端口（随服务器运行，访问该端口即打开客户端）
    "backends": {
        "text": {
            "use_default": False,
            "api_base_url": "",
            "api_key": "",
            "api_model": "",
        },
        "embed": {
            "use_default": False,
            "api_base_url": "",
            "api_key": "",
            "api_model": "",
        },
        "multimodal": {
            "use_default": False,
            "api_base_url": "",
            "api_key": "",
            "api_model": "",
        },
    },
}

# 旧的 v0.6 配置键名 → 新键名映射（一次性迁移用，避免用户丢失旧 API 设置）
_LEGACY_MIGRATION = {
    "primary_base_url": ("backends", "text", "api_base_url"),
    "primary_api_key": ("backends", "text", "api_key"),
    "primary_model": ("backends", "text", "api_model"),
    "secondary_base_url": ("backends", "multimodal", "api_base_url"),
    "secondary_api_key": ("backends", "multimodal", "api_key"),
    "secondary_model": ("backends", "multimodal", "api_model"),
}


def _deep_default(cfg):
    """把 DEFAULT_CONFIG 的层级默认值补进 cfg（不覆盖用户已有的值）。"""
    for top_key, top_val in DEFAULT_CONFIG.items():
        if isinstance(top_val, dict):
            sub = cfg.setdefault(top_key, {})
            for k, v in top_val.items():
                sub.setdefault(k, v)
        else:
            cfg.setdefault(top_key, top_val)
    return cfg


def load_config():
    """读取磁盘配置并补齐默认值；若配置为旧版则自动迁移一次。"""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = {}

    # 旧版配置迁移：检测到旧键且新结构为空时，把旧键映射到新键
    if "backends" not in cfg and any(k in cfg for k in _LEGACY_MIGRATION):
        migrated = {}
        for old_key, (top, sub, field) in _LEGACY_MIGRATION.items():
            if old_key in cfg and cfg.get(old_key):
                migrated.setdefault(top, {})[field] = cfg[old_key]
        cfg["backends"] = migrated

    # 清理已废弃的接入相关配置键（QQ 接入已移除）
    for stale_key in ("bot_qq", "active_group_id", "napcat_token"):
        cfg.pop(stale_key, None)

    return _deep_default(cfg)


def reload_config():
    """重读磁盘配置并原地更新全局 config，使运行中的模块立即感知最新设置。"""
    fresh = load_config()
    config.clear()
    config.update(fresh)
    return config


def save_config(cfg):
    """把整个配置对象写回磁盘（原子写入：先写临时文件再替换）。"""
    tmp_file = CONFIG_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, CONFIG_FILE)


# 模块级全局配置对象（所有模块通过 `from config.api_config import config` 读取）
config = load_config()
