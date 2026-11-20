# core/skills/builtin/plugin_forge.py
# 自建插件：调用 ZCode/DeepSeek harness 在 staging 目录生成插件包。
# 不直接执行生成代码；只做清单/语法/导入校验，随后由插件页导入。
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config.api_config import config
from core import skill_registry
from core.plugin_manifest import normalize_manifest, validate_manifest

PROJECT_DIR = Path(__file__).resolve().parents[3]
STAGING_ROOT = PROJECT_DIR / "data" / "plugin_staging"
SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(text: str) -> str:
    raw = str(text or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_]+", "_", raw)
    raw = raw.strip("_")
    if not raw:
        raw = "self_made_skill"
    if not raw[0].isalpha():
        raw = "skill_" + raw
    return raw[:40]


def _command_parts(template: str) -> list[str]:
    template = str(template or "").strip()
    if not template:
        return []
    try:
        return shlex.split(template, posix=(os.name != "nt"))
    except Exception:
        return []


def _run_harness(command: str, prompt_file: Path, out_dir: Path, timeout: int) -> tuple[bool, str]:
    parts = _command_parts(command)
    if not parts:
        return False, (
            "没有配置 harness 命令。请在“技能”面板填写 forge_command，"
            "支持 {prompt_file} 和 {out_dir} 占位符，例如："
            "dsh --profile acp --prompt {prompt_file} --cwd {out_dir}"
        )
    parts = [
        part.replace("{prompt_file}", str(prompt_file)).replace("{out_dir}", str(out_dir))
        for part in parts
    ]
    env = dict(os.environ)
    env["NHY_PLUGIN_STAGING"] = str(STAGING_ROOT)
    try:
        result = subprocess.run(
            parts,
            cwd=str(out_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout or 300)),
        )
    except subprocess.TimeoutExpired:
        return False, "harness 超时，未生成可用插件包"
    except Exception as exc:
        return False, f"harness 启动失败：{exc}"
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    if result.returncode != 0:
        return False, f"harness 退出码 {result.returncode}\n{output[-4000:]}"
    return True, output[-4000:]


def _find_manifest(root: Path) -> Path | None:
    direct = root / "plugin.json"
    if direct.is_file():
        return direct
    for path in root.rglob("plugin.json"):
        if path.is_file():
            return path
    return None


def _prompt(request: str, plugin_id: str, out_dir: Path) -> str:
    return f"""你要为 Nascence Huiye（NHY）生成一个可安装的插件包。

用户需求：
{request}

输出目录：{out_dir}
插件 id：{plugin_id}

必须遵守 NHY 插件契约：
1. 在输出目录生成 plugin.json，字段：
   id={plugin_id}, name, version=0.1.0, api_version=1, min_host_version=0.9.0,
   kind=skill, entry="plugin.py:PLUGIN", panel="panel.py:PANEL",
   capabilities=[], platforms=["any"], permissions=[], description, readme="README.md",
   author, license="MIT"。
2. plugin.py 必须导出模块级 PLUGIN = 一个继承 plugins.base.Plugin 的类。
3. 如果提供动作，实现 extra_actions() 和 async perform_action(kind, data)；
   动作名要以插件 id 为前缀，避免和其他技能冲突。
4. 不要读取/上传 NHY 的 API Key、config/api_config.json 或 data/ 下的隐私数据。
5. 生成 README.md，说明用途、安装、权限和配置。
6. 只写输出目录内的文件，不要修改宿主仓库。
7. 完成后用 python -m py_compile 检查 plugin.py 语法。
"""


def _validate_package(root: Path, plugin_id: str) -> tuple[bool, str, dict]:
    manifest_path = _find_manifest(root)
    if manifest_path is None:
        return False, "生成结果里没有 plugin.json", {}
    try:
        manifest = normalize_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8-sig")),
            fallback_id=plugin_id,
        )
    except Exception as exc:
        return False, f"plugin.json 解析失败：{exc}", {}
    errors = validate_manifest(manifest, expected_id=manifest["id"])
    if errors:
        return False, "；".join(errors), manifest
    entry_name = manifest["entry"].split(":", 1)[0]
    entry = manifest_path.parent / entry_name
    if not entry.is_file():
        return False, f"入口文件不存在：{entry_name}", manifest
    # 只做语法检查，不执行生成代码。
    try:
        check = subprocess.run(
            [sys.executable, "-m", "py_compile", str(entry)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return False, f"语法检查异常：{exc}", manifest
    if check.returncode != 0:
        return False, f"语法检查失败：{(check.stderr or check.stdout)[-2000:]}", manifest
    return True, "", manifest


def run(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    request = str(data.get("request") or data.get("task") or "").strip()
    if not request:
        return {"ok": False, "detail": "请告诉我要生成什么插件/技能。"}
    settings = skill_registry.skill_config("plugin_forge")
    backend = str(data.get("backend") or settings.get("forge_backend") or "dsh").lower()
    command = str(data.get("command") or settings.get("forge_command") or "").strip()
    plugin_id = _slug(data.get("id") or request[:30])
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    out_dir = STAGING_ROOT / f"{plugin_id}_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = out_dir / "REQUEST.md"
    prompt_file.write_text(_prompt(request, plugin_id, out_dir), encoding="utf-8")

    ok, output = _run_harness(command, prompt_file, out_dir, int(settings.get("forge_timeout") or 300))
    if not ok:
        return {
            "ok": False,
            "detail": output,
            "staging": str(out_dir),
            "backend": backend,
            "content_lines": [],
        }
    valid, error, manifest = _validate_package(out_dir, plugin_id)
    if not valid:
        return {
            "ok": False,
            "detail": f"harness 已结束，但插件包校验失败：{error}",
            "staging": str(out_dir),
            "harness_output": output,
            "content_lines": [],
        }
    # 把 manifest 所在目录统一成包根，并打 zip 方便面板导入。
    package_root = _find_manifest(out_dir).parent
    zip_path = STAGING_ROOT / f"{plugin_id}_{int(time.time())}.zip"
    from core.plugin_install import zip_directory
    zip_directory(package_root, zip_path, root_name=manifest["id"])
    return {
        "ok": True,
        "detail": (
            f"已生成插件 {manifest['name']} v{manifest['version']}，暂存在 {out_dir}。"
            f"请打开控制面板“插件社区 → 导入本地插件”，选择 {zip_path} 导入，然后一键重启。"
        ),
        "staging": str(out_dir),
        "zip": str(zip_path),
        "plugin_id": manifest["id"],
        "manifest": manifest,
        "content_lines": [],
    }
