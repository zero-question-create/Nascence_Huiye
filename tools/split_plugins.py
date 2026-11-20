# tools/split_plugins.py
# 把当前 plugins/<id> 复制成独立的插件仓库（本地）。
#
# 用法：python tools/split_plugins.py
# 输出：dist/plugin_repos/nhy-plugin-<id>/
# 每个目录已 git init + commit + tag v1.0.0，推送见 tools/push_plugin_repos.ps1。
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = PROJECT_DIR / "plugins"
OUTPUT_ROOT = PROJECT_DIR / "dist" / "plugin_repos"

REPOS = {
    "qq_napcat": "nhy-plugin-qq-napcat",
    "wechat": "nhy-plugin-wechat",
    "web": "nhy-plugin-web",
    "bilibili": "nhy-plugin-bilibili",
    "browser": "nhy-plugin-browser",
    "desktop": "nhy-plugin-desktop",
    "qq_desktop": "nhy-plugin-qq-desktop",
    "skills": "nhy-plugin-skills",
}

REQUIREMENTS = {
    "desktop": [
        "uiautomation==2.0.29",
        "comtypes==1.4.17",
        "pillow==12.3.0",
        "PyQt5==5.15.11",
    ],
    "qq_desktop": [
        "uiautomation==2.0.29",
        "comtypes==1.4.17",
        "pillow==12.3.0",
    ],
    "qq_napcat": ["aiohttp==3.14.1", "websockets==16.1"],
    "wechat": ["websockets==16.1"],
    "bilibili": ["aiohttp==3.14.1"],
    "browser": ["aiohttp==3.14.1"],
    "web": [],
    "skills": [],
}

WORKFLOW = """name: release
on:
  push:
    tags: ["v*"]
  workflow_dispatch:
permissions:
  contents: write
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Package plugin
        run: |
          zip -r "${{ github.event.repository.name }}.zip" . \
            -x ".git/*" -x ".github/*" -x "tests/*" -x "*.pyc" -x "__pycache__/*"
          sha256sum *.zip > SHA256SUMS
      - name: Publish release
        uses: softprops/action-gh-release@v2
        with:
          files: |
            *.zip
            SHA256SUMS
      - name: Add nhy-plugin topic
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh api --method PUT "repos/${{ github.repository }}/topics" \\
            -f names='["nhy-plugin"]' || true
"""

TEST_MANIFEST = """import json
from pathlib import Path


def test_manifest():
    data = json.loads(Path("plugin.json").read_text(encoding="utf-8"))
    assert data["id"]
    assert data["version"].count(".") == 2
    assert data["api_version"] == 1
    assert data["entry"].endswith(":PLUGIN")
"""


def _read_manifest(directory: Path, plugin_id: str) -> dict:
    path = directory / "plugin.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {
        "id": plugin_id,
        "name": plugin_id,
        "version": "1.0.0",
        "api_version": 1,
        "min_host_version": "0.9.0",
        "kind": "app",
        "entry": "plugin.py:PLUGIN",
        "description": "",
    }


def _make_readme(directory: Path, manifest: dict, repo_name: str) -> None:
    path = directory / "README.md"
    if path.is_file():
        return
    path.write_text(
        f"# {manifest.get('name') or manifest['id']}\n\n"
        f"{manifest.get('description') or ''}\n\n"
        "## 安装\n\n"
        "在 NHY 控制面板「插件社区」中检索 `nhy-plugin` topic，或使用「导入本地插件」安装 Release ZIP。\n\n"
        f"仓库：https://github.com/zero-question-create/{repo_name}\n\n"
        "## 说明\n\n"
        "插件由 NHY 宿主提供 `plugins.base`、`core` 与 `panel_runtime` API；"
        "安装到 `plugins/<id>` 后由 NHY 自动发现。\n",
        encoding="utf-8",
    )


def _remove_tree(path: Path) -> None:
    def _onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


def _git(directory: Path, *args: str) -> None:
    git = (
        os.environ.get("NHY_GIT")
        or shutil.which("git")
        or r"F:\Program Files\Git\cmd\git.exe"
    )
    subprocess.run(
        [git, *args],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )


def build_repo(plugin_id: str, repo_name: str) -> Path:
    source = PLUGIN_ROOT / plugin_id
    if not source.is_dir():
        raise SystemExit(f"缺少插件目录：{source}")
    target = OUTPUT_ROOT / repo_name
    if target.exists():
        _remove_tree(target)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"),
    )
    manifest = _read_manifest(target, plugin_id)
    manifest.setdefault("id", plugin_id)
    manifest.setdefault("version", "1.0.0")
    manifest.setdefault("api_version", 1)
    manifest.setdefault("min_host_version", "0.9.0")
    manifest.setdefault("entry", "plugin.py:PLUGIN")
    manifest.setdefault("readme", "README.md")
    manifest.setdefault("author", "Nascence")
    manifest.setdefault("license", "MIT")
    manifest["source"] = f"https://github.com/zero-question-create/{repo_name}"
    manifest["homepage"] = manifest["source"]
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "__init__.py").touch(exist_ok=True)
    _make_readme(target, manifest, repo_name)

    req = REQUIREMENTS.get(plugin_id) or []
    (target / "requirements.txt").write_text(
        "\n".join(req) + ("\n" if req else ""), encoding="utf-8"
    )

    tests = target / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_manifest.py").write_text(TEST_MANIFEST, encoding="utf-8")
    (tests / "__init__.py").touch(exist_ok=True)
    workflows = target / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "release.yml").write_text(WORKFLOW, encoding="utf-8")

    _git(target, "init")
    _git(target, "config", "user.name", "Nascence Bot")
    _git(target, "config", "user.email", "nascence@example.invalid")
    _git(target, "add", ".")
    _git(target, "commit", "-m", f"Initial {plugin_id} plugin v{manifest['version']}")
    _git(target, "tag", f"v{manifest['version']}")
    return target


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for plugin_id, repo_name in REPOS.items():
        target = build_repo(plugin_id, repo_name)
        print(f"[OK] {plugin_id} -> {target}")
    print(f"\nDone. {len(REPOS)} local plugin repos in {OUTPUT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
