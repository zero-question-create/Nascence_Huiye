# core/plugin_install.py
# ========================================================================
# 插件安装/更新/卸载的底层文件操作。
#
# 约束：
# - 只接受 plugin.json 完整、id 合法的包；
# - ZIP 解压必须防 zip-slip / 符号链接；
# - 先 staging，再原子替换；导入冒烟失败自动回滚；
# - 安装记录写 config/plugins.lock.json。
# ========================================================================

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from config.constants import NHY_VERSION, PLUGIN_API_VERSION
from core.plugin_manifest import (
    MANIFEST_NAME,
    normalize_manifest,
    read_manifest,
    validate_manifest,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = PROJECT_DIR / "plugins"
CONFIG_DIR = PROJECT_DIR / "config"
LOCK_FILE = CONFIG_DIR / "plugins.lock.json"
STAGING_ROOT = PROJECT_DIR / "data" / "test" / "plugin_staging"
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


def git_executable() -> str:
    """优先使用 NHY 自带 MinGit，其次系统 git。"""
    candidates = []
    env = os.environ.get("NHY_GIT", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.extend([
        PROJECT_DIR / "run" / "lib" / "git" / "cmd" / "git.exe",
        PROJECT_DIR / "run" / "lib" / "git" / "bin" / "git",
        PROJECT_DIR / "run" / "lib" / "git" / "cmd" / "git",
        Path(r"F:\Program Files\Git\cmd\git.exe"),
        Path(r"C:\Program Files\Git\cmd\git.exe"),
    ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("git") or ""


def sha256_file(path: Path | str, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def load_lock() -> dict:
    try:
        if LOCK_FILE.is_file():
            with LOCK_FILE.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("plugins", {})
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "plugins": {}}


def save_lock(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOCK_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, LOCK_FILE)


def lock_entry(plugin_id: str) -> dict:
    return dict((load_lock().get("plugins") or {}).get(plugin_id) or {})


def update_lock(plugin_id: str, **fields) -> None:
    data = load_lock()
    entry = dict(data["plugins"].get(plugin_id) or {})
    entry.update(fields)
    entry["updated_at"] = time.time()
    data["plugins"][plugin_id] = entry
    save_lock(data)


def remove_lock(plugin_id: str) -> None:
    data = load_lock()
    data["plugins"].pop(plugin_id, None)
    save_lock(data)


def installed_plugin_dir(plugin_id: str) -> Path:
    return PLUGIN_ROOT / str(plugin_id)


def is_installed(plugin_id: str) -> bool:
    return (installed_plugin_dir(plugin_id) / MANIFEST_NAME).is_file()


def _safe_extract(zip_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"压缩包包含非法路径：{name}")
            # 拒绝符号链接：external_attr 高 16 位为 Unix mode，0xA000 是 symlink。
            mode = (info.external_attr >> 16) & 0xF000
            if mode == 0xA000:
                raise ValueError(f"压缩包包含符号链接：{name}")
            target = (destination / name).resolve()
            if destination != target and destination not in target.parents:
                raise ValueError(f"压缩包路径越界：{name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_package_root(directory: Path) -> Optional[Path]:
    """ZIP 可能把插件放在单层子目录里，自动找到含 plugin.json 的根。"""
    if (directory / MANIFEST_NAME).is_file():
        return directory
    children = [item for item in directory.iterdir() if item.is_dir()]
    if len(children) == 1 and (children[0] / MANIFEST_NAME).is_file():
        return children[0]
    for item in children:
        if (item / MANIFEST_NAME).is_file():
            return item
    return None


def _smoke_import(plugin_id: str) -> tuple[bool, str]:
    """在子进程里导入刚安装的插件，避免坏插件污染当前进程。"""
    code = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(PROJECT_DIR)!r})\n"
        f"m = importlib.import_module('plugins.{plugin_id}.plugin')\n"
        "from plugins.base import Plugin\n"
        "cls = getattr(m, 'PLUGIN', None)\n"
        "assert isinstance(cls, type) and issubclass(cls, Plugin), 'PLUGIN 不是 Plugin 子类'\n"
        "print('ok')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["NO_PROXY"] = env.get("NO_PROXY", "")
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except Exception as exc:
        return False, f"导入检查异常：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, f"导入检查失败：{detail[-1200:]}"
    return True, ""


def install_from_zip(
    zip_path: Path | str,
    expected_id: str = "",
    source: str = "",
    version: str = "",
    sha256: str = "",
) -> dict:
    """安装/更新一个插件 ZIP。返回 {ok, id, version, error}。"""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return {"ok": False, "error": "安装包不存在"}
    try:
        if zip_path.stat().st_size > MAX_ARCHIVE_BYTES:
            return {"ok": False, "error": "安装包超过 200MB 限制"}
    except OSError as exc:
        return {"ok": False, "error": f"读取安装包失败：{exc}"}

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="nhy_plugin_", dir=str(STAGING_ROOT)))
    try:
        _safe_extract(zip_path, work)
        package_root = _find_package_root(work)
        if package_root is None:
            return {"ok": False, "error": "安装包里找不到 plugin.json"}
        manifest = read_manifest(package_root)
        normalized = normalize_manifest(manifest)
        errors = validate_manifest(normalized, expected_id=expected_id)
        if errors:
            return {"ok": False, "error": "；".join(errors)}
        plugin_id = normalized["id"]
        entry_file = package_root / normalized["entry"].split(":", 1)[0]
        if not entry_file.is_file():
            return {"ok": False, "error": f"入口文件不存在：{entry_file.name}"}
        # 插件目录必须是可导入的包；生成器/第三方包经常漏 __init__.py。
        if not (package_root / "__init__.py").is_file():
            (package_root / "__init__.py").write_text("", encoding="utf-8")

        # 依赖先检查本机是否已安装；不自动递归拉取，避免意外装一堆东西。
        missing = [
            dep for dep in normalized.get("dependencies") or []
            if not is_installed(dep)
        ]
        if missing:
            return {
                "ok": False,
                "error": "缺少依赖插件：" + "、".join(missing),
            }

        target = installed_plugin_dir(plugin_id)
        backup = None
        if target.exists():
            # 备份必须放在 work 之外：work 本身可能就是 package_root，
            # 随后会被整体移动到 target；备份放在里面会被一起搬进新插件。
            backup = STAGING_ROOT / "replaced" / f"{plugin_id}_{int(time.time() * 1000)}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        try:
            shutil.move(str(package_root), str(target))
            ok, error = _smoke_import(plugin_id)
            if not ok:
                shutil.rmtree(target, ignore_errors=True)
                if backup is not None:
                    shutil.move(str(backup), str(target))
                return {"ok": False, "error": error}
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            if backup is not None and backup.exists():
                shutil.move(str(backup), str(target))
            raise

        update_lock(
            plugin_id,
            version=normalized["version"] or version or "0.0.0",
            source=source or normalized.get("source") or "local",
            sha256=sha256 or sha256_file(zip_path),
            api_version=PLUGIN_API_VERSION,
            host_version=NHY_VERSION,
            path=str(target),
        )
        return {
            "ok": True,
            "id": plugin_id,
            "version": normalized["version"],
            "name": normalized["name"],
            "restart_required": True,
        }
    except Exception as exc:
        return {"ok": False, "error": f"安装失败：{exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def zip_directory(source: Path | str, output: Path | str, root_name: str = "") -> Path:
    """把一个插件目录打成 ZIP（用于本地导入/发布）。"""
    source = Path(source)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            arcname = Path(root_name) / relative if root_name else relative
            archive.write(path, arcname.as_posix())
    return output


def install_from_directory(
    directory: Path | str,
    expected_id: str = "",
    source: str = "local",
) -> dict:
    directory = Path(directory)
    if not directory.is_dir():
        return {"ok": False, "error": "目录不存在"}
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nhy_plugin_zip_", dir=str(STAGING_ROOT)) as tmp:
        zip_path = Path(tmp) / "plugin.zip"
        zip_directory(directory, zip_path)
        return install_from_zip(zip_path, expected_id=expected_id, source=source)


def install_from_git(repo_url: str, expected_id: str = "", ref: str = "") -> dict:
    """用 NHY 内部 Git clone 仓库并安装；release ZIP 不可用时的后备。"""
    git = git_executable()
    if not git:
        return {"ok": False, "error": "找不到 git；请先运行 run/install_git.ps1"}
    repo_url = str(repo_url or "").strip()
    if not repo_url:
        return {"ok": False, "error": "缺少仓库地址"}
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="nhy_git_", dir=str(STAGING_ROOT)))
    clone_dir = work / "repo"
    try:
        args = [git, "clone", "--depth", "1"]
        if ref:
            args.extend(["--branch", str(ref)])
        args.extend([repo_url, str(clone_dir)])
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"ok": False, "error": f"git clone 失败：{(result.stderr or result.stdout)[-1200:]}"}
        clean_dir = work / "clean"
        shutil.copytree(
            clone_dir, clean_dir,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        return install_from_directory(
            clean_dir, expected_id=expected_id, source=repo_url
        )
    except Exception as exc:
        return {"ok": False, "error": f"git 安装失败：{exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def uninstall_plugin(plugin_id: str, purge: bool = False) -> dict:
    plugin_id = str(plugin_id or "").strip()
    if not plugin_id:
        return {"ok": False, "error": "缺少插件 id"}
    target = installed_plugin_dir(plugin_id)
    if not target.exists():
        remove_lock(plugin_id)
        return {"ok": True, "detail": "插件本来就没有安装"}
    trash = STAGING_ROOT / "removed" / f"{plugin_id}_{int(time.time())}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(target), str(trash))
    except Exception as exc:
        return {"ok": False, "error": f"卸载失败：{exc}"}
    remove_lock(plugin_id)
    return {
        "ok": True,
        "detail": f"已卸载 {plugin_id}（备份在 {trash}）" if not purge else f"已卸载 {plugin_id}",
        "restart_required": True,
    }
