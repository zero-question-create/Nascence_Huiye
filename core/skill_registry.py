# core/skill_registry.py
# ========================================================================
# 辉夜可调用的内置技能注册表。
#
# 设计要点：
# - 技能默认不进入动作提示词；只有 enabled=True 的技能才向动作层暴露；
# - 分级权限：read 类启用即授权；write/exec/install 类必须在面板显式授权；
# - 技能设置/画质/大小询问只存在面板，不写进日常认知提示词；
# - 所有执行写 data/test/skill_audit.jsonl。
# ========================================================================

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from config.api_config import config

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_DIR / "config" / "skills.json"
AUDIT_FILE = PROJECT_DIR / "data" / "test" / "skill_audit.jsonl"

_LOCK = threading.RLock()

SKILL_DEFS = {
    "screenshot": {
        "id": "screenshot",
        "name": "截屏分析",
        "description": "截取当前窗口或全屏，交给视觉模型分析并返回摘要。",
        "action": "screenshot",
        "tier": "read",
        "permissions": ["screen.capture"],
        "default_enabled": False,
        "example": '{"action":"screenshot","target":"active"}',
        "settings": {
            "screenshot_scope": "active",  # active | screen
            "screenshot_max_chars": 1200,
        },
    },
    "bilibili_video": {
        "id": "bilibili_video",
        "name": "B站视频分析",
        "description": "读取 B 站视频信息，下载低清视频并抽取关键帧交给视觉模型分析；也支持字幕/弹幕/评论摘要。",
        "action": "bilibili_video",
        "tier": "read",
        "permissions": ["network.bilibili", "media.analyze"],
        "default_enabled": False,
        "example": '{"action":"bilibili_video","url":"https://www.bilibili.com/video/BV...","quality":"360p"}',
        "settings": {
            "bilibili_quality": "360p",
            "bilibili_max_mb": 80,
            "bilibili_max_seconds": 600,
            "bilibili_frames": 8,
            "bilibili_allow_higher_quality": True,
        },
    },
    "read_media": {
        "id": "read_media",
        "name": "读取媒体全文",
        "description": "按 media_id 返回照片/表情包的完整 LLM 描述和 OCR，不截断。",
        "action": "read_media",
        "tier": "read",
        "permissions": ["media.read"],
        "default_enabled": False,
        "example": '{"action":"read_media","media_id":"..."}',
        "settings": {},
    },
    "plugin_forge": {
        "id": "plugin_forge",
        "name": "自建插件",
        "description": "调用 ZCode/DeepSeek harness 生成一个插件包，暂存到 data/plugin_staging，等待导入。",
        "action": "plugin_forge",
        "tier": "write",
        "permissions": ["filesystem.write", "process.spawn", "plugins.install"],
        "default_enabled": False,
        "example": '{"action":"plugin_forge","request":"做一个能查询天气的插件"}',
        "settings": {
            "forge_backend": "dsh",
            "forge_command": "",
            "forge_timeout": 300,
        },
    },
}

_READ_TIER = "read"


def _default_state() -> dict:
    skills = {}
    for skill_id, definition in SKILL_DEFS.items():
        permissions = list(definition["permissions"])
        skills[skill_id] = {
            "enabled": bool(definition["default_enabled"]),
            # 只读技能启用即视为授权；危险权限留空，必须在面板确认。
            "permissions_granted": permissions if definition["tier"] == _READ_TIER else [],
            "settings": dict(definition["settings"]),
        }
    return {"version": 1, "skills": skills}


def _load_state() -> dict:
    state = _default_state()
    if STATE_FILE.is_file():
        try:
            with STATE_FILE.open("r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict) and isinstance(raw.get("skills"), dict):
                for skill_id, entry in raw["skills"].items():
                    if skill_id not in state["skills"] or not isinstance(entry, dict):
                        continue
                    state["skills"][skill_id]["enabled"] = bool(entry.get("enabled"))
                    granted = entry.get("permissions_granted")
                    if isinstance(granted, list):
                        state["skills"][skill_id]["permissions_granted"] = [
                            str(item) for item in granted if str(item)
                        ]
                    settings = entry.get("settings")
                    if isinstance(settings, dict):
                        state["skills"][skill_id]["settings"].update(settings)
        except (OSError, json.JSONDecodeError):
            pass
    return state


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def get_state() -> dict:
    with _LOCK:
        return _load_state()


def skill_config(skill_id: str) -> dict:
    state = get_state()
    entry = (state.get("skills") or {}).get(skill_id) or {}
    definition = SKILL_DEFS.get(skill_id) or {}
    settings = dict(definition.get("settings") or {})
    settings.update(entry.get("settings") or {})
    # 面板/全局配置是默认值来源；技能 state 可覆盖。
    legacy_map = {
        "screenshot_max_chars": "skill_screenshot_max_chars",
        "bilibili_quality": "skill_bilibili_quality",
        "bilibili_max_mb": "skill_bilibili_max_mb",
        "bilibili_max_seconds": "skill_bilibili_max_seconds",
        "bilibili_frames": "skill_bilibili_frames",
        "bilibili_allow_higher_quality": "skill_bilibili_allow_higher_quality",
        "forge_backend": "skill_forge_backend",
        "forge_command": "skill_forge_command",
        "forge_timeout": "skill_forge_timeout",
    }
    for key, config_key in legacy_map.items():
        value = config.get(config_key)
        if value not in (None, ""):
            settings.setdefault(key, value)
    settings["enabled"] = bool(entry.get("enabled"))
    settings["permissions_granted"] = list(entry.get("permissions_granted") or [])
    return settings


def is_enabled(skill_id: str) -> bool:
    entry = (get_state().get("skills") or {}).get(skill_id) or {}
    return bool(entry.get("enabled"))


def set_enabled(skill_id: str, enabled: bool) -> dict:
    if skill_id not in SKILL_DEFS:
        return {"ok": False, "error": "未知技能"}
    with _LOCK:
        state = _load_state()
        entry = state["skills"].setdefault(
            skill_id,
            {"enabled": False, "permissions_granted": [], "settings": {}},
        )
        entry["enabled"] = bool(enabled)
        definition = SKILL_DEFS.get(skill_id) or {}
        if enabled and definition.get("tier") == _READ_TIER:
            for permission in definition.get("permissions") or []:
                if permission not in entry["permissions_granted"]:
                    entry["permissions_granted"].append(permission)
        _save_state(state)
    audit(skill_id, "enable" if enabled else "disable", "", bool(enabled))
    return {"ok": True, "enabled": bool(enabled)}


def grant_permission(skill_id: str, permission: str, granted: bool = True) -> dict:
    if skill_id not in SKILL_DEFS:
        return {"ok": False, "error": "未知技能"}
    definition = SKILL_DEFS.get(skill_id) or {}
    if permission not in (definition.get("permissions") or []):
        return {"ok": False, "error": "这个技能没有声明该权限"}
    with _LOCK:
        state = _load_state()
        entry = state["skills"].setdefault(
            skill_id, {"enabled": False, "permissions_granted": [], "settings": {}}
        )
        granted_list = entry.setdefault("permissions_granted", [])
        if granted and permission not in granted_list:
            granted_list.append(permission)
        if not granted and permission in granted_list:
            granted_list.remove(permission)
        _save_state(state)
    audit(skill_id, "grant" if granted else "revoke", permission, True)
    return {"ok": True, "permissions_granted": list(granted_list)}



def update_settings(skill_id: str, values: dict) -> dict:
    if skill_id not in SKILL_DEFS:
        return {"ok": False, "error": "未知技能"}
    values = values if isinstance(values, dict) else {}
    with _LOCK:
        state = _load_state()
        entry = state["skills"].setdefault(
            skill_id, {"enabled": False, "permissions_granted": [], "settings": {}}
        )
        settings = entry.setdefault("settings", {})
        known = SKILL_DEFS[skill_id].get("settings") or {}
        for key, value in values.items():
            if key in known:
                settings[key] = value
        _save_state(state)
    return {"ok": True}


def enabled_actions() -> dict:
    """只返回已启用技能的动作名→提示示例；禁用技能不进入提示词。"""
    if not bool(config.get("skills_enabled", True)):
        return {}
    result = {}
    state = get_state().get("skills") or {}
    for skill_id, definition in SKILL_DEFS.items():
        entry = state.get(skill_id) or {}
        if not entry.get("enabled"):
            continue
        result[definition["action"]] = definition.get("example") or (
            '{"action":"%s"}' % definition["action"]
        )
    return result


def check_permission(skill_id: str, permission: str) -> bool:
    definition = SKILL_DEFS.get(skill_id) or {}
    if permission not in (definition.get("permissions") or []):
        return False
    entry = (get_state().get("skills") or {}).get(skill_id) or {}
    return bool(entry.get("enabled")) and permission in (entry.get("permissions_granted") or [])


def audit(skill_id: str, action: str, detail: str = "", ok: bool = True) -> None:
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "skill": skill_id,
            "action": action,
            "detail": str(detail or "")[:500],
            "ok": bool(ok),
        }
        with AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def panel_schema() -> dict:
    """技能配置面板：每个技能一个开关 + 已声明权限 + 设置。"""
    state = get_state().get("skills") or {}
    fields = []
    for skill_id, definition in SKILL_DEFS.items():
        entry = state.get(skill_id) or {}
        fields.append({
            "key": f"{skill_id}__enabled",
            "label": f"{definition['name']}（{'已启用' if entry.get('enabled') else '未启用'}）",
            "type": "bool",
            "hint": definition.get("description") or "",
        })
        for permission in definition.get("permissions") or []:
            fields.append({
                "key": f"{skill_id}__perm__{permission}",
                "label": f"{definition['name']} · 权限 {permission}",
                "type": "bool",
                "hint": "只读技能启用即授权；写入/执行/安装类必须在这里确认。",
            })
        for key, default in (definition.get("settings") or {}).items():
            field_type = "number" if isinstance(default, (int, float)) else "text"
            if isinstance(default, bool):
                field_type = "bool"
            field = {
                "key": f"{skill_id}__set__{key}",
                "label": f"{definition['name']} · {key}",
                "type": field_type,
                "placeholder": str(default),
            }
            if key == "forge_command":
                field["hint"] = (
                    "支持 {prompt_file} 与 {out_dir} 占位符，例如："
                    "dsh --profile acp --prompt {prompt_file} --cwd {out_dir}"
                )
            fields.append(field)
    return {
        "status": [{"key": "service", "label": "技能服务"}],
        "fields": fields,
        "hint": "技能设置只在启用后生效；这些参数不会进入日常认知提示词。",
        "save_label": "保存技能配置",
    }


def panel_values() -> dict:
    state = get_state().get("skills") or {}
    values = {}
    for skill_id, definition in SKILL_DEFS.items():
        entry = state.get(skill_id) or {}
        values[f"{skill_id}__enabled"] = bool(entry.get("enabled"))
        granted = entry.get("permissions_granted") or []
        for permission in definition.get("permissions") or []:
            values[f"{skill_id}__perm__{permission}"] = permission in granted
        settings = skill_config(skill_id)
        for key, default in (definition.get("settings") or {}).items():
            values[f"{skill_id}__set__{key}"] = settings.get(key, default)
    values["_status"] = {
        "service": "已启用" if any(
            (entry or {}).get("enabled") for entry in state.values()
        ) else "全部关闭"
    }
    return values


def panel_save(values: dict) -> dict:
    values = values if isinstance(values, dict) else {}
    with _LOCK:
        for skill_id, definition in SKILL_DEFS.items():
            enabled_key = f"{skill_id}__enabled"
            if enabled_key in values:
                set_enabled(skill_id, bool(values.get(enabled_key)))
            for permission in definition.get("permissions") or []:
                key = f"{skill_id}__perm__{permission}"
                if key in values:
                    grant_permission(skill_id, permission, bool(values.get(key)))
            settings = {}
            for key in (definition.get("settings") or {}):
                form_key = f"{skill_id}__set__{key}"
                if form_key in values:
                    settings[key] = values.get(form_key)
            if settings:
                update_settings(skill_id, settings)
    return {"ok": True}


def run_action(action: str, data: dict) -> dict:
    """执行已启用技能；未启用/未授权直接拒绝。"""
    data = data if isinstance(data, dict) else {}
    action = str(action or "").strip()
    definition = next(
        (item for item in SKILL_DEFS.values() if item.get("action") == action),
        None,
    )
    if definition is None:
        return {"ok": False, "detail": "未知技能动作"}
    skill_id = definition["id"]
    if not is_enabled(skill_id):
        return {"ok": False, "detail": f"{definition['name']} 还没有启用"}
    for permission in definition.get("permissions") or []:
        if not check_permission(skill_id, permission):
            return {
                "ok": False,
                "detail": f"{definition['name']} 缺少权限 {permission}，请先在插件面板授权。",
            }
    if not bool(config.get("skills_enabled", True)):
        return {"ok": False, "detail": "技能总开关已关闭"}
    try:
        if action == "screenshot":
            from core.skills.builtin import screenshot as handler
            result = handler.run(data)
        elif action == "bilibili_video":
            from core.skills.builtin import bilibili_video as handler
            result = handler.run(data)
        elif action == "read_media":
            from core.skills.builtin import read_media as handler
            result = handler.run(data)
        elif action == "plugin_forge":
            from core.skills.builtin import plugin_forge as handler
            result = handler.run(data)
        else:
            return {"ok": False, "detail": "技能没有实现"}
    except Exception as exc:
        audit(skill_id, action, str(exc), False)
        return {"ok": False, "detail": f"{definition['name']} 执行失败：{exc}"}
    audit(skill_id, action, str(result.get("detail") or result.get("summary") or ""), bool(result.get("ok")))
    return result if isinstance(result, dict) else {"ok": True, "detail": str(result)}
