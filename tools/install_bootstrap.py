# tools/install_bootstrap.py
# 首次安装/迁移时安装默认插件。优先 GitHub Release，本地开发时回落到 dist/plugin_repos。
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from core import plugin_install, plugin_market  # noqa: E402

CONFIG = PROJECT_DIR / "config" / "plugin_bootstrap.json"
LOCAL_ROOT = PROJECT_DIR / "dist" / "plugin_repos"


def _platform_ok(entry: dict) -> bool:
    wanted = str(entry.get("platform") or "").strip().lower()
    if not wanted:
        return True
    current = "windows" if sys.platform.startswith("win") else (
        "darwin" if sys.platform == "darwin" else "linux"
    )
    return wanted == current


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--required-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    data = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    entries = data.get("plugins") or []
    # 依赖顺序：desktop 先于 qq_desktop
    entries.sort(key=lambda item: 0 if item.get("id") == "desktop" else 1)
    failed = []
    for entry in entries:
        if not _platform_ok(entry):
            continue
        if args.required_only and not entry.get("required"):
            continue
        plugin_id = str(entry.get("id") or "").strip()
        repo = str(entry.get("repo") or "").strip()
        if not plugin_id:
            continue
        if plugin_install.is_installed(plugin_id):
            print(f"[OK] {plugin_id} already installed")
            continue
        result = None
        if not args.offline:
            try:
                result = plugin_market.install_plugin(plugin_id)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
        if not result or not result.get("ok"):
            local = LOCAL_ROOT / repo
            if local.is_dir():
                result = plugin_install.install_from_directory(
                    local, expected_id=plugin_id, source="local-bootstrap"
                )
        if result and result.get("ok"):
            print(f"[OK] {plugin_id} {result.get('version') or ''}")
        else:
            print(f"[!] {plugin_id} install failed: {(result or {}).get('error')}")
            failed.append(plugin_id)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
