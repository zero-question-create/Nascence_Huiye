import json
import os


def normalize_proxy_environment():
    """让 httpx/OpenAI 能识别常见代理工具导出的 socks scheme，并保护本地服务不走代理。"""
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        value = os.environ.get(name)
        if value and value.lower().startswith("socks://"):
            os.environ[name] = "socks5://" + value[len("socks://"):]

    # 保护本地服务（Ollama、NapCat 等）不经过代理
    local_entries = {"localhost", "127.0.0.1", "::1"}
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    current = {e.strip() for e in no_proxy.replace(",", " ").replace(";", " ").split() if e.strip()}
    if not local_entries.issubset(current):
        merged = ", ".join(sorted(current | local_entries))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged


normalize_proxy_environment()

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "api_config.json")

DEFAULT_CONFIG = {
    "primary_api_key": "YOUR_API_KEY_HERE",
    "primary_base_url": "https://api.deepseek.com",
    "primary_model": "deepseek-v4-flash",
    "secondary_api_key": "YOUR_API_KEY_HERE",
    "secondary_base_url": "https://api.lucisapi.ai/v1",
    "secondary_model": "gpt-5.6-sol",
    "ollama_base_url": "http://localhost:11434",
    "ollama_embed_model": "shaw/dmeta-embedding-zh",
    "bot_qq": "123456",
    "active_group_id": "123456",
    "napcat_token": "Nascence",
    "panel_port": 32123,                    # 控制面板 Web 端口（修改后下次启动生效）
    # ===== 生物钟参数（动力学速率，非钟点；不预设节律周期）=====
    "biorhythm_wake_seconds": 18.40 * 3600, # 清醒时睡眠压力上升的指数时间常数（秒）
    "biorhythm_sleep_seconds": 9.1 * 3600,  # 睡眠时睡眠压力回落的指数时间常数（秒）
    "biorhythm_onset_threshold": 0.62,      # 入睡阈值（S 达到 0.62 即精力 0.38 时入睡）
    "biorhythm_wake_threshold": 0.20,       # 醒来阈值（S 低于 0.20 即精力回到 0.80 时醒来）
    # ===== 作息惯性（学得的昼夜节律，非硬编码钟点）=====
    "biorhythm_rhythm_weight": 0.25,        # 作息窗口内最大睡意抬升量（峰值时 S≥0.37 即可入睡）
    "biorhythm_idle_to_sleep": 300,         # 靠作息入睡所需的安静时长（秒），即"没人发消息"多久
    "biorhythm_rhythm_min_nights": 5,       # 积累多少晚后作息开始生效
    "biorhythm_rhythm_full_nights": 10,     # 积累多少晚后作息强度完全生效
    # ===== 动作抉择（本能冲动）=====
    "action_enabled": True,                 # 是否允许每轮末尾做动作抉择
    "action_max_pages": 4,                  # 单个动作类型的翻页上限
    "action_page_size": 12,                 # 每页候选数量
    "action_note_enabled": True             # 是否允许写 txt 笔记
}

ENV_MAPPING = {
    "PRIMARY_API_KEY": "primary_api_key",
    "PRIMARY_BASE_URL": "primary_base_url",
    "PRIMARY_MODEL": "primary_model",
    "SECONDARY_API_KEY": "secondary_api_key",
    "SECONDARY_BASE_URL": "secondary_base_url",
    "SECONDARY_MODEL": "secondary_model",
    "OLLAMA_BASE_URL": "ollama_base_url",
    "OLLAMA_EMBED_MODEL": "ollama_embed_model",
    "BOT_QQ": "bot_qq",
    "ACTIVE_GROUP_ID": "active_group_id",
    "NAPCAT_TOKEN": "napcat_token",
    "PANEL_PORT": "panel_port",
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
            for k in DEFAULT_CONFIG:
                cfg.setdefault(k, DEFAULT_CONFIG[k])
    else:
        cfg = dict(DEFAULT_CONFIG)

    # 容器化与环境变量支持（Docker / .env）：
    # 当配置项仍为默认占位符，或设置了 HUIYE_ENV_OVERRIDE=1 时，优先采用非空环境变量
    env_override = os.environ.get("HUIYE_ENV_OVERRIDE") == "1"
    for env_k, cfg_k in ENV_MAPPING.items():
        env_val = os.environ.get(env_k)
        if env_val is not None and str(env_val).strip():
            val_str = str(env_val).strip()
            is_placeholder = cfg.get(cfg_k) in (DEFAULT_CONFIG.get(cfg_k), "", None)
            if env_override or is_placeholder:
                if cfg_k == "panel_port":
                    try:
                        cfg[cfg_k] = int(val_str)
                    except ValueError:
                        pass
                else:
                    cfg[cfg_k] = val_str

    return cfg

def reload_config():
    """重读磁盘配置并原地更新全局 config，使运行中的模块立即感知最新设置。"""
    fresh = load_config()
    config.clear()
    config.update(fresh)
    return config

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()
