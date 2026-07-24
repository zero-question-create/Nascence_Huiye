import json
import os


def normalize_proxy_environment():
    """让 httpx/OpenAI 能识别常见代理工具导出的 socks scheme。"""
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        value = os.environ.get(name)
        if value and value.lower().startswith("socks://"):
            os.environ[name] = "socks5://" + value[len("socks://"):]


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
    "ollama_embed_model": "shaw/dmeta-embedding-zh"
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k in DEFAULT_CONFIG:
                cfg.setdefault(k, DEFAULT_CONFIG[k])
            return cfg
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()
