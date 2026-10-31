# plugins/registry.py
# ========================================================================
# 插件发现与配置。
#
# - 扫描 plugins/<id>/plugin.py，读取模块级 PLUGIN 类；
# - config/plugins.json 保存启用状态与每个插件的行为 LLM；
# - 首次运行从 config/plugins.example.json（或内置默认）生成；
# - 插件 LLM 留空时回落到 api_config.json 的 primary_*。
#
# 新增插件不需要改本文件：只要目录里有 plugin.py 且导出 PLUGIN 类，
# 就会被自动发现，并在配置中补一条默认关闭的条目。
# ========================================================================

from __future__ import annotations

import copy
import importlib
import json
import logging
import pkgutil
import threading
from pathlib import Path
from typing import Dict, List, Optional, Type

from utils.atomic_io import atomic_write_json

from .base import Capability, Plugin

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "plugins.json"
EXAMPLE_FILE = CONFIG_DIR / "plugins.example.json"

# 面板展示顺序；未列出的插件排在其后
BUILTIN_ORDER = ("qq_napcat", "wechat", "web", "bilibili", "browser")

_DEFAULT_LLM = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": None,
    "max_tokens": None,
    "timeout": None,
}

_DEFAULT_PLUGIN = {
    "enabled": False,
    "llm": dict(_DEFAULT_LLM),
    "follow_global_behavior": True,
    "action_llm": dict(_DEFAULT_LLM),
    "action_include_reasoning": False,
}

DEFAULT_CONFIG = {
    "active": "qq_napcat",
    "plugins": {
        "qq_napcat": {**copy.deepcopy(_DEFAULT_PLUGIN), "enabled": True},
        "wechat": copy.deepcopy(_DEFAULT_PLUGIN),
        "web": copy.deepcopy(_DEFAULT_PLUGIN),
        "bilibili": copy.deepcopy(_DEFAULT_PLUGIN),
        "browser": {**copy.deepcopy(_DEFAULT_PLUGIN), "enabled": True},
    },
}

_LOCK = threading.RLock()
_config_cache: Optional[dict] = None
_plugin_classes: Optional[Dict[str, Type[Plugin]]] = None
_instances: Dict[str, Plugin] = {}


def _logger():
    return logging.getLogger("Plugins")


def _deep_copy(value):
    return copy.deepcopy(value)


def _normalize_entry(current, entry: dict) -> dict:
    """规范化单个插件配置；兼容旧配置并补齐行为模型字段。"""
    current = current if isinstance(current, dict) else {}
    current.setdefault("enabled", bool(entry.get("enabled", False)))
    current.setdefault("action_include_reasoning", False)
    # 迁移：旧配置里已经填了插件级 action_llm 的，默认继续使用自己的
    # 行为模型；没填过的默认跟随全局行为模型。
    if "follow_global_behavior" not in current:
        action = current.get("action_llm")
        action = action if isinstance(action, dict) else {}
        current["follow_global_behavior"] = not any(
            str(action.get(k) or "").strip()
            for k in ("api_key", "base_url", "model")
        )
    current.setdefault("follow_global_behavior", True)
    for field in ("llm", "action_llm"):
        value = current.get(field)
        if not isinstance(value, dict):
            value = {}
        for key, default_value in entry.get(field, {}).items():
            value.setdefault(key, default_value)
        current[field] = value
    return current


def _normalize(raw: dict) -> dict:
    """补齐缺省字段，保证面板与运行期看到的配置结构一致。"""
    raw = raw if isinstance(raw, dict) else {}
    plugins = raw.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}

    defaults = _deep_copy(DEFAULT_CONFIG["plugins"])
    for plugin_id, entry in defaults.items():
        plugins[plugin_id] = _normalize_entry(plugins.get(plugin_id), entry)

    # 自动发现的新插件：默认关闭，行为模型默认跟随全局。
    for plugin_id in _classes():
        if plugin_id not in plugins:
            plugins[plugin_id] = _deep_copy(_DEFAULT_PLUGIN)

    # 第三方/旧自定义插件也补齐字段，避免面板显示与运行期判断不一致。
    for plugin_id, current in list(plugins.items()):
        plugins[plugin_id] = _normalize_entry(current, _DEFAULT_PLUGIN)

    active = raw.get("active")
    if active not in plugins:
        active = next((pid for pid in BUILTIN_ORDER if pid in plugins), next(iter(plugins), ""))

    return {"active": active or "", "plugins": plugins}


def _read_raw() -> dict:
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            _logger().error("插件配置解析失败（%s），本次使用内置默认值", exc)
        return {}
    if EXAMPLE_FILE.exists():
        try:
            with EXAMPLE_FILE.open("r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 首次运行：把示例复制成可写的工作配置
                atomic_write_json(str(CONFIG_FILE), _normalize(data))
                return data
        except (OSError, json.JSONDecodeError) as exc:
            _logger().error("插件示例配置读取失败（%s），本次使用内置默认值", exc)
    atomic_write_json(str(CONFIG_FILE), _normalize(DEFAULT_CONFIG))
    return _deep_copy(DEFAULT_CONFIG)


def load_config() -> dict:
    """读取并规范化插件配置（带进程内缓存）。返回副本。"""
    global _config_cache
    with _LOCK:
        if _config_cache is None:
            _config_cache = _normalize(_read_raw())
        return _deep_copy(_config_cache)


def save_config(cfg: dict) -> bool:
    """原子写入插件配置并热更新实例设置（不销毁运行中的插件线程）。"""
    global _config_cache
    normalized = _normalize(cfg)
    ok = atomic_write_json(str(CONFIG_FILE), normalized)
    if ok:
        with _LOCK:
            _config_cache = normalized
            for plugin_id, instance in _instances.items():
                entry = normalized["plugins"].get(plugin_id)
                if isinstance(entry, dict):
                    try:
                        instance.settings.clear()
                        instance.settings.update(_deep_copy(entry))
                    except Exception:
                        pass
    return ok


def reload():
    """丢弃配置/类缓存并热更新实例设置。

    重新扫描插件目录：运行中新放入的插件无需重启即可被发现。
    已有插件实例保留，避免正在运行的微信桥/插件线程因为保存配置而失去
    stop() 句柄。已导入模块的代码改动仍需重启才能生效。
    """
    global _config_cache, _plugin_classes
    with _LOCK:
        _config_cache = None
        _plugin_classes = None
        # 重新读盘并把新设置灌回现有实例；不销毁实例本身。
        fresh = _normalize(_read_raw())
        _config_cache = fresh
        for plugin_id, instance in _instances.items():
            entry = fresh["plugins"].get(plugin_id)
            if isinstance(entry, dict):
                try:
                    instance.settings.clear()
                    instance.settings.update(_deep_copy(entry))
                except Exception:
                    pass


def _discover() -> Dict[str, Type[Plugin]]:
    """扫描 plugins/*/plugin.py 中的 PLUGIN 类。"""
    found: Dict[str, Type[Plugin]] = {}
    try:
        import plugins as package
    except Exception as exc:
        _logger().exception("插件包导入失败: %s", exc)
        return found

    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg or info.name in ("base", "registry"):
            continue
        module_name = f"plugins.{info.name}.plugin"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # 没有 plugin.py 的目录（如仅放说明文档）直接跳过
            if exc.name == module_name:
                continue
            _logger().exception("插件 %s 导入失败", info.name)
            continue
        except Exception:
            _logger().exception("插件 %s 导入失败", info.name)
            continue
        cls = getattr(module, "PLUGIN", None)
        if not isinstance(cls, type) or not issubclass(cls, Plugin):
            _logger().warning("插件 %s 未导出 Plugin 子类 PLUGIN，已跳过", info.name)
            continue
        plugin_id = getattr(cls, "plugin_id", "") or info.name
        found[plugin_id] = cls
    return found


def _classes() -> Dict[str, Type[Plugin]]:
    global _plugin_classes
    with _LOCK:
        if _plugin_classes is None:
            _plugin_classes = _discover()
        return _plugin_classes


def plugin_ids() -> List[str]:
    ids = list(_classes().keys())
    ordered = [pid for pid in BUILTIN_ORDER if pid in ids]
    ordered.extend(sorted(pid for pid in ids if pid not in ordered))
    return ordered


def get_plugin(plugin_id: str) -> Optional[Plugin]:
    """按 id 取插件实例（带缓存）。"""
    plugin_id = str(plugin_id or "").strip()
    cls = _classes().get(plugin_id)
    if cls is None:
        return None
    with _LOCK:
        instance = _instances.get(plugin_id)
        if instance is None:
            settings = load_config()["plugins"].get(plugin_id, {})
            instance = cls(settings)
            _instances[plugin_id] = instance
        return instance


def get_active_id() -> str:
    return str(load_config().get("active") or "")


def active_plugin() -> Optional[Plugin]:
    """返回当前启用的插件；当前插件被禁用时返回 None（不回退到别的插件）。"""
    cfg = load_config()
    active_id = str(cfg.get("active") or "")
    entry = cfg["plugins"].get(active_id) or {}
    if not entry.get("enabled", False):
        return None
    return get_plugin(active_id)


def app_plugins() -> List[Plugin]:
    """返回已实现且启用的“软件”插件（用于未读状态与打开/关闭动作）。"""
    cfg = load_config()
    result = []
    for plugin_id in plugin_ids():
        entry = cfg["plugins"].get(plugin_id) or {}
        if not entry.get("enabled", False):
            continue
        instance = get_plugin(plugin_id)
        if instance is None or not instance.implemented or not getattr(instance, "is_app", False):
            continue
        result.append(instance)
    return result


def _safe_llm_value(llm: dict) -> dict:
    llm = llm if isinstance(llm, dict) else {}
    return {
        "base_url": llm.get("base_url", ""),
        "model": llm.get("model", ""),
        "temperature": llm.get("temperature"),
        "max_tokens": llm.get("max_tokens"),
        "timeout": llm.get("timeout"),
        "_has_api_key": str(llm.get("api_key") or "").strip() not in ("", "YOUR_API_KEY_HERE"),
    }


def _safe_llm(entry: dict) -> dict:
    return _safe_llm_value(entry.get("llm"))


def list_plugins() -> List[dict]:
    """供面板展示：不返回 API Key 明文。"""
    cfg = load_config()
    result = []
    for plugin_id in plugin_ids():
        instance = get_plugin(plugin_id)
        if instance is None:
            continue
        entry = cfg["plugins"].get(plugin_id, {})
        item = {
            "id": plugin_id,
            "label": instance.label or plugin_id,
            "capabilities": list(instance.capabilities),
            "implemented": bool(instance.implemented),
            "enabled": bool(entry.get("enabled", False)),
            "is_app": bool(getattr(instance, "is_app", False)),
            "follow_global_behavior": bool(entry.get("follow_global_behavior", True)),
            "action_include_reasoning": bool(entry.get("action_include_reasoning", False)),
            "active": plugin_id == cfg.get("active"),
            "llm": _safe_llm(entry),
            "action_llm": _safe_llm_value(entry.get("action_llm")),
            "description": instance.description,
        }
        try:
            item["status"] = instance.status()
        except Exception as exc:
            item["status"] = {"error": str(exc)}
        result.append(item)
    return result


def _merge_llm(current: dict, patch: dict) -> dict:
    """合并 llm 字段。api_key 为空表示“不修改”，_clear_api_key 才清空。"""
    current = current if isinstance(current, dict) else {}
    patch = patch if isinstance(patch, dict) else {}
    allowed = ("base_url", "model", "temperature", "max_tokens", "timeout")
    for key in allowed:
        if key in patch:
            current[key] = patch[key]
    if patch.get("_clear_api_key"):
        current["api_key"] = ""
    elif patch.get("api_key"):
        current["api_key"] = str(patch["api_key"])
    return current


def update_from_panel(payload: dict) -> dict:
    """面板保存入口。

    支持两种形态：
      {"active": "qq_napcat", "plugins": {"qq_napcat": {"enabled": true, "llm": {...}}}}
      {"plugin_id": "qq_napcat", "enabled": true, "llm": {...}}
    """
    payload = payload if isinstance(payload, dict) else {}
    cfg = load_config()

    if payload.get("active"):
        candidate = str(payload["active"]).strip()
        if candidate in cfg["plugins"]:
            cfg["active"] = candidate

    updates = payload.get("plugins")
    if not isinstance(updates, dict):
        plugin_id = str(payload.get("plugin_id") or "").strip()
        updates = {plugin_id: payload} if plugin_id else {}

    for plugin_id, update in updates.items():
        if plugin_id not in cfg["plugins"] or not isinstance(update, dict):
            continue
        entry = cfg["plugins"][plugin_id]
        if "enabled" in update:
            entry["enabled"] = bool(update["enabled"])
        if "follow_global_behavior" in update:
            entry["follow_global_behavior"] = bool(update["follow_global_behavior"])
        if "action_include_reasoning" in update:
            entry["action_include_reasoning"] = bool(update["action_include_reasoning"])
        if "llm" in update:
            entry["llm"] = _merge_llm(entry.get("llm") or {}, update.get("llm") or {})
        if "action_llm" in update:
            entry["action_llm"] = _merge_llm(
                entry.get("action_llm") or {}, update.get("action_llm") or {}
            )

    if not save_config(cfg):
        raise OSError(f"插件配置写入失败: {CONFIG_FILE}")
    reload()
    return {"active": get_active_id(), "plugins": list_plugins()}


def plugin_config_path() -> str:
    return str(CONFIG_FILE)


__all__ = [
    "Capability",
    "DEFAULT_CONFIG",
    "active_plugin",
    "app_plugins",
    "get_active_id",
    "get_plugin",
    "list_plugins",
    "load_config",
    "plugin_config_path",
    "plugin_ids",
    "reload",
    "save_config",
    "update_from_panel",
]
