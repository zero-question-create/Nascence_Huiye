# core/plugin_manifest.py
# ========================================================================
# 插件清单（plugin.json）的读取、规范化与兼容性校验。
#
# 独立插件仓库和本地安装目录都以 plugin.json 为唯一元数据来源；
# 旧插件没有清单时，registry 会用插件类的属性合成一份 legacy 清单，
# 保证已有插件继续可用。
# ========================================================================

from __future__ import annotations

import json
import re
from pathlib import Path

from config.constants import NHY_VERSION, PLUGIN_API_VERSION

MANIFEST_NAME = "plugin.json"
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

_ALLOWED_KINDS = {"app", "skill", "tool", "bridge"}
_ALLOWED_PLATFORMS = {"windows", "linux", "darwin", "any"}


def _version_tuple(value: str) -> tuple:
    core = str(value or "").split("-", 1)[0].split("+", 1)[0]
    parts = []
    for item in core.split("."):
        try:
            parts.append(int(item))
        except (TypeError, ValueError):
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def version_at_least(current: str, required: str) -> bool:
    return _version_tuple(current) >= _version_tuple(required)


def read_manifest(plugin_dir: Path | str) -> dict:
    """读取目录下的 plugin.json；不存在或损坏时返回空字典。"""
    path = Path(plugin_dir) / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_manifest(data: dict, fallback_id: str = "") -> dict:
    data = data if isinstance(data, dict) else {}
    plugin_id = str(data.get("id") or fallback_id or "").strip()
    kind = str(data.get("kind") or "app").strip().lower()
    if kind not in _ALLOWED_KINDS:
        kind = "app"
    platforms = data.get("platforms")
    if isinstance(platforms, str):
        platforms = [platforms]
    platforms = [
        str(item).strip().lower() for item in (platforms or ["any"])
        if str(item).strip()
    ]
    platforms = [item for item in platforms if item in _ALLOWED_PLATFORMS] or ["any"]
    permissions = data.get("permissions")
    if isinstance(permissions, str):
        permissions = [permissions]
    dependencies = data.get("dependencies")
    if isinstance(dependencies, str):
        dependencies = [dependencies]
    try:
        api_version = int(data.get("api_version") or PLUGIN_API_VERSION)
    except (TypeError, ValueError):
        api_version = PLUGIN_API_VERSION
    return {
        "id": plugin_id,
        "name": str(data.get("name") or data.get("label") or plugin_id).strip(),
        "version": str(data.get("version") or "0.0.0").strip(),
        "api_version": api_version,
        "min_host_version": str(data.get("min_host_version") or "0.0.0").strip(),
        "kind": kind,
        "entry": str(data.get("entry") or "plugin.py:PLUGIN").strip(),
        "panel": str(data.get("panel") or "panel.py:PANEL").strip(),
        "capabilities": [str(item) for item in (data.get("capabilities") or []) if str(item)],
        "platforms": platforms,
        "permissions": [str(item) for item in (permissions or []) if str(item)],
        "dependencies": [str(item) for item in (dependencies or []) if str(item)],
        "description": str(data.get("description") or "").strip(),
        "readme": str(data.get("readme") or "README.md").strip(),
        "homepage": str(data.get("homepage") or "").strip(),
        "author": str(data.get("author") or "").strip(),
        "license": str(data.get("license") or "").strip(),
        "source": str(data.get("source") or "").strip(),
        "trusted": bool(data.get("trusted", False)),
    }


def build_legacy_manifest(plugin_id: str, plugin_class) -> dict:
    """把旧插件的类属性合成为清单，供面板/社区统一展示。"""
    capabilities = []
    for item in getattr(plugin_class, "capabilities", ()) or ():
        capabilities.append(getattr(item, "value", str(item)))
    return normalize_manifest(
        {
            "id": plugin_id,
            "name": getattr(plugin_class, "label", "") or plugin_id,
            "version": getattr(plugin_class, "version", "0.0.0"),
            "kind": "skill" if getattr(plugin_class, "is_skill", False) else "app",
            "capabilities": capabilities,
            "description": getattr(plugin_class, "description", ""),
            "readme": "README.md",
            "author": getattr(plugin_class, "author", ""),
            "homepage": getattr(plugin_class, "homepage", ""),
            "license": getattr(plugin_class, "license", "MIT"),
            "source": "builtin",
        },
        fallback_id=plugin_id,
    )


def validate_manifest(manifest: dict, expected_id: str = "") -> list[str]:
    """返回错误列表；空列表表示可以安装/加载。"""
    data = normalize_manifest(manifest, fallback_id=expected_id)
    errors = []
    plugin_id = data["id"]
    if not plugin_id or not PLUGIN_ID_RE.match(plugin_id):
        errors.append("id 必须是小写字母开头的 a-z0-9_ 标识")
    if expected_id and plugin_id != expected_id:
        errors.append(f"manifest id（{plugin_id}）与目录/仓库名（{expected_id}）不一致")
    if not SEMVER_RE.match(data["version"]):
        errors.append("version 必须是 x.y.z 形式")
    if int(data["api_version"]) != PLUGIN_API_VERSION:
        errors.append(
            f"plugin api_version={data['api_version']}，宿主只支持 {PLUGIN_API_VERSION}"
        )
    if not version_at_least(NHY_VERSION, data["min_host_version"]):
        errors.append(
            f"需要宿主 >= {data['min_host_version']}，当前 {NHY_VERSION}"
        )
    if ":" not in data["entry"]:
        errors.append("entry 必须是 '文件:变量' 形式")
    return errors


def manifest_for_panel(manifest: dict) -> dict:
    """只暴露面板需要的字段，避免把内部路径/未来字段直接透出。"""
    data = normalize_manifest(manifest)
    return {
        "id": data["id"],
        "name": data["name"],
        "version": data["version"],
        "api_version": data["api_version"],
        "min_host_version": data["min_host_version"],
        "kind": data["kind"],
        "capabilities": data["capabilities"],
        "platforms": data["platforms"],
        "permissions": data["permissions"],
        "dependencies": data["dependencies"],
        "description": data["description"],
        "readme": data["readme"],
        "homepage": data["homepage"],
        "author": data["author"],
        "license": data["license"],
        "source": data["source"],
        "trusted": data["trusted"],
    }
