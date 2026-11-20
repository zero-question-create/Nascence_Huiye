# core/plugin_market.py
# ========================================================================
# 插件社区后端：GitHub topic 检索、清单/README 获取、发布包校验与安装。
#
# 设计原则：
# - 只从 GitHub Release 的 ZIP 安装，不从任意分支/源码目录直接执行；
# - 优先使用 release asset 的 SHA256（GitHub digest 或 SHA256SUMS）；
# - catalog 只缓存元数据，网络失败时回落到上次缓存；
# - 所有写操作最终交给 core.plugin_install（安全解压/回滚/冒烟）。
# ========================================================================

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from config.api_config import config
from core.plugin_install import (
    installed_plugin_dir,
    install_from_git,
    install_from_zip,
    is_installed,
    load_lock,
    sha256_file,
    uninstall_plugin,
)
from core.plugin_manifest import (
    normalize_manifest,
    read_manifest,
    validate_manifest,
    version_at_least,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_DIR / "data" / "test" / "plugin_catalog.json"
README_CACHE_DIR = PROJECT_DIR / "data" / "test" / "plugin_readmes"
DEFAULT_TOPIC = "nhy-plugin"
USER_AGENT = "Nascence-Huiye-Plugin-Market/1.0"


class MarketError(RuntimeError):
    pass


def _cfg(key: str, default=None):
    try:
        value = config.get(key)
    except Exception:
        value = None
    return default if value in (None, "") else value


def _github_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = str(_cfg("community_github_token", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request_json(url: str, timeout: float = 20.0) -> dict:
    request = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise MarketError("GitHub API 限流；请在控制面板配置 community_github_token") from exc
        raise MarketError(f"GitHub 请求失败：HTTP {exc.code}") from exc
    except Exception as exc:
        raise MarketError(f"GitHub 请求失败：{exc}") from exc


def _request_text(url: str, timeout: float = 20.0) -> str:
    request = urllib.request.Request(url, headers=_github_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _raw_url(repo: dict, path: str) -> str:
    branch = str(repo.get("default_branch") or "main")
    full_name = str(repo.get("full_name") or "").strip()
    return f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"


def _repo_expected_id(repo: dict) -> str:
    name = str(repo.get("name") or "").strip()
    for prefix in ("nhy-plugin-", "nhy_plugin_", "nhy-"):
        if name.startswith(prefix):
            return name[len(prefix):].replace("-", "_")
    return ""


def _release_info(repo: dict) -> dict:
    full_name = str(repo.get("full_name") or "").strip()
    if not full_name:
        return {}
    try:
        release = _request_json(f"https://api.github.com/repos/{full_name}/releases/latest")
    except MarketError:
        return {}
    assets = release.get("assets") or []
    zip_asset = next(
        (item for item in assets if str(item.get("name") or "").lower().endswith(".zip")),
        None,
    )
    sums_asset = next(
        (item for item in assets if str(item.get("name") or "").lower() == "sha256sums"),
        None,
    )
    digest = ""
    if zip_asset:
        raw_digest = str(zip_asset.get("digest") or "")
        if raw_digest.startswith("sha256:"):
            digest = raw_digest.split(":", 1)[1].strip().lower()
    sums_text = ""
    if sums_asset:
        try:
            sums_text = _request_text(str(sums_asset.get("browser_download_url") or ""))
        except Exception:
            sums_text = ""
    if zip_asset and not digest and sums_text:
        name = str(zip_asset.get("name") or "")
        for line in sums_text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == name:
                digest = parts[0].lower()
                break
    return {
        "tag": str(release.get("tag_name") or ""),
        "published_at": release.get("published_at") or "",
        "download_url": str(zip_asset.get("browser_download_url") or "") if zip_asset else "",
        "asset_name": str(zip_asset.get("name") or "") if zip_asset else "",
        "sha256": digest,
        "sums_asset": str(sums_asset.get("browser_download_url") or "") if sums_asset else "",
    }


def _candidate_from_repo(repo: dict) -> Optional[dict]:
    full_name = str(repo.get("full_name") or "").strip()
    if not full_name:
        return None
    try:
        manifest_raw = _request_text(_raw_url(repo, "plugin.json"))
        manifest = normalize_manifest(json.loads(manifest_raw))
    except Exception:
        return None
    expected = _repo_expected_id(repo)
    errors = validate_manifest(manifest, expected_id=expected or manifest["id"])
    if errors:
        return None
    readme = ""
    try:
        readme = _request_text(_raw_url(repo, manifest.get("readme") or "README.md"))
    except Exception:
        readme = ""
    release = _release_info(repo)
    return {
        "id": manifest["id"],
        "name": manifest["name"],
        "version": manifest["version"],
        "kind": manifest["kind"],
        "description": manifest["description"],
        "author": manifest["author"],
        "homepage": manifest["homepage"] or str(repo.get("html_url") or ""),
        "repo": str(repo.get("html_url") or ""),
        "full_name": full_name,
        "platforms": manifest["platforms"],
        "permissions": manifest["permissions"],
        "dependencies": manifest["dependencies"],
        "readme": readme[:200000],
        "readme_url": _raw_url(repo, manifest.get("readme") or "README.md"),
        "download_url": release.get("download_url") or "",
        "asset_name": release.get("asset_name") or "",
        "sha256": release.get("sha256") or "",
        "release_tag": release.get("tag") or "",
        "release_at": release.get("published_at") or "",
        "stars": int(repo.get("stargazers_count") or 0),
        "verified": bool(release.get("download_url") and release.get("sha256")),
        "source": "github",
    }


def search_github(topic: str = "", user: str = "", limit: int = 100) -> list[dict]:
    topic = str(topic or _cfg("community_topic", DEFAULT_TOPIC) or DEFAULT_TOPIC).strip()
    user = str(user or _cfg("community_github_user", "") or "").strip()
    query = f"topic:{topic} fork:false archived:false"
    if user:
        query += f" user:{user}"
    url = (
        "https://api.github.com/search/repositories?"
        + urllib.parse.urlencode({
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": max(1, min(int(limit or 100), 100)),
        })
    )
    data = _request_json(url)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    result = []
    for repo in items:
        candidate = _candidate_from_repo(repo)
        if candidate:
            result.append(candidate)
    result.sort(key=lambda item: (-int(item.get("stars") or 0), item.get("name") or ""))
    return result


def _load_cache() -> dict:
    try:
        if CACHE_FILE.is_file():
            with CACHE_FILE.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"plugins": [], "fetched_at": 0}


def _save_cache(plugins: list[dict]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plugins": plugins, "fetched_at": time.time()}
    tmp = CACHE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, CACHE_FILE)


def get_catalog(force: bool = False) -> dict:
    """返回 catalog；网络失败时使用缓存，并标记 stale/error。"""
    cache = _load_cache()
    max_age = int(_cfg("community_cache_seconds", 600) or 600)
    cache_fresh = bool(cache.get("plugins")) and time.time() - float(cache.get("fetched_at") or 0) < max_age
    if cache_fresh and not force:
        return {"plugins": cache["plugins"], "fetched_at": cache.get("fetched_at"), "stale": False}
    try:
        plugins = search_github()
        if plugins:
            _save_cache(plugins)
            return {"plugins": plugins, "fetched_at": time.time(), "stale": False}
        return {"plugins": cache.get("plugins") or [], "fetched_at": cache.get("fetched_at"), "stale": True}
    except Exception as exc:
        return {
            "plugins": cache.get("plugins") or [],
            "fetched_at": cache.get("fetched_at"),
            "stale": True,
            "error": str(exc),
        }


def find_plugin(plugin_id: str, force: bool = False) -> Optional[dict]:
    plugin_id = str(plugin_id or "").strip()
    catalog = get_catalog(force=force)
    for item in catalog.get("plugins") or []:
        if item.get("id") == plugin_id:
            return item
    return None


def read_readme(plugin_id: str) -> str:
    item = find_plugin(plugin_id)
    if not item:
        return ""
    readme = str(item.get("readme") or "")
    if readme:
        return readme
    url = str(item.get("readme_url") or "")
    if not url:
        return ""
    try:
        text = _request_text(url)
        README_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (README_CACHE_DIR / f"{plugin_id}.md").write_text(text, encoding="utf-8")
        return text
    except Exception:
        cached = README_CACHE_DIR / f"{plugin_id}.md"
        return cached.read_text(encoding="utf-8") if cached.is_file() else ""


def _download(url: str, target: Path, timeout: float = 60.0) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as handle:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            handle.write(block)


def install_plugin(plugin_id: str, force: bool = False) -> dict:
    # 优先使用已缓存的 catalog（即使过期也先信它）；安装按钮不应该每次
    # 重新扫全部仓库，否则 GitHub API 一慢就会把安装请求拖到超时。
    item = next(
        (entry for entry in (_load_cache().get("plugins") or [])
         if entry.get("id") == plugin_id),
        None,
    )
    if not item:
        item = find_plugin(plugin_id, force=True)
    if not item:
        return {"ok": False, "error": "社区里找不到这个插件"}
    download_url = str(item.get("download_url") or "")
    expected_sha = str(item.get("sha256") or "").lower()
    if not download_url:
        if bool(_cfg("community_allow_git", True)) and item.get("repo"):
            return install_from_git(
                str(item.get("repo")),
                expected_id=plugin_id,
                ref=str(item.get("release_tag") or ""),
            )
        return {"ok": False, "error": "这个插件还没有发布 Release ZIP，且未启用 git 后备安装"}
    if not expected_sha:
        return {"ok": False, "error": "发布包缺少 SHA256，拒绝安装"}
    if is_installed(plugin_id) and not force:
        installed_version = str((load_lock().get("plugins") or {}).get(plugin_id, {}).get("version") or "")
        if installed_version and not version_at_least(item.get("version") or "0.0.0", installed_version):
            return {"ok": True, "detail": "已经是最新版本", "version": installed_version}

    temp_dir = PROJECT_DIR / "data" / "test" / "plugin_downloads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(str(item.get("asset_name") or "plugin.zip")).suffix or ".zip"
    target = temp_dir / f"{plugin_id}_{int(time.time())}{suffix}"
    try:
        _download(download_url, target)
        actual = sha256_file(target)
        if actual.lower() != expected_sha:
            return {"ok": False, "error": f"SHA256 不匹配（期望 {expected_sha[:12]}…，实际 {actual[:12]}…）"}
        result = install_from_zip(
            target,
            expected_id=plugin_id,
            source=str(item.get("repo") or "github"),
            version=str(item.get("version") or ""),
            sha256=actual,
        )
        if result.get("ok"):
            result["restart_required"] = True
            result["source"] = item.get("repo")
        return result
    except Exception as exc:
        return {"ok": False, "error": f"下载/安装失败：{exc}"}
    finally:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def update_plugin(plugin_id: str) -> dict:
    return install_plugin(plugin_id, force=True)


def uninstall(plugin_id: str, purge: bool = False) -> dict:
    return uninstall_plugin(plugin_id, purge=purge)


def installed_summary() -> dict:
    """本机已安装插件摘要：核心目录里的旧插件也算已安装。"""
    result = {}
    try:
        for child in (PROJECT_DIR / "plugins").iterdir():
            if not child.is_dir():
                continue
            manifest = read_manifest(child)
            if not manifest:
                continue
            data = normalize_manifest(manifest, fallback_id=child.name)
            result[data["id"]] = {
                "version": data.get("version") or "",
                "source": data.get("source") or "local",
                "sha256": "",
                "installed": True,
                "updated_at": None,
                "path": str(child),
            }
    except Exception:
        pass
    lock = load_lock()
    for plugin_id, entry in (lock.get("plugins") or {}).items():
        directory = installed_plugin_dir(plugin_id)
        result[plugin_id] = {
            "version": entry.get("version") or result.get(plugin_id, {}).get("version", ""),
            "source": entry.get("source") or result.get(plugin_id, {}).get("source", ""),
            "sha256": entry.get("sha256") or "",
            "installed": directory.is_dir(),
            "updated_at": entry.get("updated_at"),
            "path": str(directory),
        }
    return result
