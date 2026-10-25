# plugins/bilibili/client.py
# ========================================================================
# Bilibili 只读客户端（Python 版）。
#
# 参考 dsh-bili-whale 的边界：以使用者自己的账号只读浏览/搜索/看视频，
# 不做批量刷量、不自动三连/投币/关注；写操作一律不在这里实现。
#
# 能力：
#   - Cookie：SESSDATA / bili_jct / buvid3（从 config/bilibili_credentials.json 读取）
#   - WBI 签名：nav 取 img/sub key → mixin key → w_rid
#   - 推荐流、搜索、视频元信息、官方字幕、弹幕热词、热门评论
#   - 请求间隔与 412/429 暂停
# ========================================================================

import asyncio
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import List, Optional
from urllib.parse import quote, urlencode

import aiohttp

from config.api_config import config
from utils.monitor import append_log

_MIXIN_KEY_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
_TAG_RE = re.compile(r"<[^>]+>")
_API = "https://api.bilibili.com"


class BiliError(RuntimeError):
    pass


class BiliRiskError(BiliError):
    """412/429 等风控响应。"""


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return max(1, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def _cfg_str(key: str, default: str) -> str:
    value = config.get(key)
    return str(value if value is not None else default).strip()


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", str(text or "")).strip()


def _mixin_key(img_url: str, sub_url: str) -> str:
    def filename(url: str) -> str:
        return str(url or "").rsplit("/", 1)[-1].split(".")[0]

    raw = filename(img_url) + filename(sub_url)
    if len(raw) < 64:
        raise BiliError("WBI key 不完整")
    return "".join(raw[i] for i in _MIXIN_KEY_TABLE[:32])


class BiliClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._wbi_keys = None
        self._wbi_at = 0.0
        self._risk_until = 0.0
        self.credentials = self._load_credentials()
        self.cookie = self._cookie_header()
        self.headers = {
            "User-Agent": _cfg_str(
                "browser_user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            ),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            self.headers["Cookie"] = self.cookie

    # ---------- 凭据 ----------
    @staticmethod
    def _credentials_path() -> str:
        return _cfg_str("bilibili_credentials_file", "config/bilibili_credentials.json")

    def _load_credentials(self) -> dict:
        path = self._credentials_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            normalized = {str(k).lower(): str(v or "").strip() for k, v in data.items()}
            return normalized
        except Exception as e:
            append_log(f"[Bilibili] 凭据读取失败: {e}")
            return {}

    def _cookie_header(self) -> str:
        creds = self.credentials
        parts = []
        for key, name in (
            ("sessdata", "SESSDATA"),
            ("bili_jct", "bili_jct"),
            ("buvid3", "buvid3"),
        ):
            value = creds.get(key)
            if value:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    @property
    def mid(self) -> str:
        return self.credentials.get("mid", "")

    @property
    def name(self) -> str:
        return self.credentials.get("name", "")

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=_cfg_int("bilibili_timeout", 20)),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---------- 请求 ----------
    def _check_risk(self):
        now = time.time()
        if now < self._risk_until:
            raise BiliRiskError(f"B 站风控暂停中，剩余 {int(self._risk_until - now)} 秒")

    async def _throttle(self):
        interval = max(0.0, _cfg_float("bilibili_request_interval", 0.6))
        async with self._lock:
            wait = interval - (time.time() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.time()

    async def _get_json(self, url: str, params: dict = None, wbi: bool = False) -> dict:
        self._check_risk()
        await self._throttle()
        params = dict(params or {})
        if wbi:
            params = await self._sign_wbi(params)
        session = await self._ensure_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status in (412, 429):
                    self._risk_until = time.time() + 300
                    raise BiliRiskError(f"B 站风控 HTTP {resp.status}，暂停 5 分钟")
                if resp.status != 200:
                    raise BiliError(f"HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except BiliRiskError:
            raise
        except Exception as e:
            raise BiliError(f"请求失败：{e}") from e
        if not isinstance(payload, dict):
            raise BiliError("返回不是 JSON 对象")
        code = payload.get("code")
        if code not in (0, None):
            message = payload.get("message") or payload.get("msg") or ""
            if code in (-101, -111):
                raise BiliError(f"登录态无效（{code}）：{message}")
            raise BiliError(f"B 站返回 {code}：{message}")
        return payload.get("data") or {}

    async def _wbi_mixin(self) -> str:
        now = time.time()
        if self._wbi_keys and now - self._wbi_at < 600:
            return self._wbi_keys
        data = await self._get_json(f"{_API}/x/web-interface/nav")
        wbi = data.get("wbi_img") or {}
        img = str(wbi.get("img_url") or "")
        sub = str(wbi.get("sub_url") or "")
        if not img or not sub:
            raise BiliError("nav 未返回 WBI key")
        self._wbi_keys = _mixin_key(img, sub)
        self._wbi_at = now
        return self._wbi_keys

    async def _sign_wbi(self, params: dict) -> dict:
        mixin = await self._wbi_mixin()
        signed = {k: str(v) for k, v in params.items() if v is not None}
        signed["wts"] = str(int(time.time()))
        signed = {
            k: v for k, v in signed.items()
            if not any(ch in k for ch in "!'()*")
        }
        query = urlencode(sorted(signed.items()), quote_via=quote)
        signed["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return signed

    # ---------- 账号 ----------
    async def nav(self) -> dict:
        data = await self._get_json(f"{_API}/x/web-interface/nav")
        return {
            "is_login": bool(data.get("isLogin")),
            "mid": str(data.get("mid") or self.mid),
            "name": str(data.get("uname") or self.name),
            "face": str(data.get("face") or ""),
            "level": data.get("level_info", {}).get("current_level"),
        }

    # ---------- 内容 ----------
    async def recommend(self, limit: int = None) -> List[dict]:
        limit = limit or _cfg_int("bilibili_feed_limit", 8)
        data = await self._get_json(
            f"{_API}/x/web-interface/wbi/index/top/feed/rcmd",
            {"ps": max(1, min(20, limit)), "fresh_type": 3, "feed_version": "V8"},
            wbi=True,
        )
        items = []
        for raw in data.get("item") or []:
            if not isinstance(raw, dict):
                continue
            bvid = str(raw.get("bvid") or "").strip()
            if not bvid:
                continue
            owner = raw.get("owner") or {}
            stat = raw.get("stat") or {}
            items.append({
                "id": bvid,
                "bvid": bvid,
                "title": _strip_html(raw.get("title") or ""),
                "author": str(owner.get("name") or ""),
                "mid": str(owner.get("mid") or ""),
                "desc": _strip_html(raw.get("desc") or "")[:300],
                "duration": raw.get("duration"),
                "views": stat.get("view"),
                "pic": str(raw.get("pic") or ""),
                "url": f"https://www.bilibili.com/video/{bvid}",
            })
            if len(items) >= limit:
                break
        return items

    async def search(self, keyword: str, page: int = 1, limit: int = 10) -> List[dict]:
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        data = await self._get_json(
            f"{_API}/x/web-interface/wbi/search/type",
            {"search_type": "video", "keyword": keyword, "page": max(1, int(page))},
            wbi=True,
        )
        items = []
        for raw in data.get("result") or []:
            if not isinstance(raw, dict):
                continue
            bvid = str(raw.get("bvid") or "").strip()
            if not bvid:
                continue
            items.append({
                "id": bvid,
                "bvid": bvid,
                "title": _strip_html(raw.get("title") or ""),
                "author": _strip_html(raw.get("author") or ""),
                "desc": _strip_html(raw.get("description") or "")[:300],
                "duration": raw.get("duration"),
                "views": raw.get("play"),
                "tags": _strip_html(raw.get("tag") or ""),
                "url": f"https://www.bilibili.com/video/{bvid}",
            })
            if len(items) >= limit:
                break
        return items

    async def video_info(self, bvid: str) -> dict:
        bvid = str(bvid or "").strip()
        if not bvid:
            raise BiliError("缺少 BV 号")
        data = await self._get_json(f"{_API}/x/web-interface/view", {"bvid": bvid})
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        tags = []
        try:
            tag_data = await self._get_json(f"{_API}/x/tag/archive/tags", {"bvid": bvid})
            tags = [str(t.get("tag_name") or "") for t in (tag_data or []) if isinstance(t, dict)]
        except Exception:
            pass
        return {
            "id": bvid,
            "bvid": bvid,
            "aid": data.get("aid"),
            "cid": data.get("cid"),
            "title": _strip_html(data.get("title") or ""),
            "desc": _strip_html(data.get("desc") or ""),
            "author": str(owner.get("name") or ""),
            "mid": str(owner.get("mid") or ""),
            "duration": data.get("duration"),
            "pubdate": data.get("pubdate"),
            "tags": tags,
            "views": stat.get("view"),
            "likes": stat.get("like"),
            "coins": stat.get("coin"),
            "favorites": stat.get("favorite"),
            "url": f"https://www.bilibili.com/video/{bvid}",
        }

    async def subtitles(self, bvid: str, cid: int) -> str:
        data = await self._get_json(
            f"{_API}/x/player/wbi/v2",
            {"bvid": bvid, "cid": cid},
            wbi=True,
        )
        subtitle = data.get("subtitle") or {}
        tracks = subtitle.get("subtitles") or []
        if not isinstance(tracks, list) or not tracks:
            return ""
        # 优先人工中文，其次 AI 中文，最后第一个
        def rank(track):
            lan = str(track.get("lan") or "").lower()
            lan_doc = str(track.get("lan_doc") or "")
            if lan.startswith("zh") and "ai" not in lan:
                return 0
            if lan.startswith("ai-zh") or "ai" in lan_doc.lower():
                return 1
            return 2

        track = sorted(tracks, key=rank)[0]
        url = str(track.get("subtitle_url") or "")
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://www.bilibili.com" + url
        try:
            session = await self._ensure_session()
            await self._throttle()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return ""
                payload = await resp.json(content_type=None)
            body = payload.get("body") if isinstance(payload, dict) else None
            lines = []
            for item in body or []:
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append(content)
            return "\n".join(lines)
        except Exception as e:
            append_log(f"[Bilibili] 字幕获取失败: {e}")
            return ""

    async def danmaku(self, cid: int, limit: int = 12) -> List[str]:
        if not cid:
            return []
        url = f"https://comment.bilibili.com/{int(cid)}.xml"
        try:
            session = await self._ensure_session()
            await self._throttle()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                raw = await resp.text(errors="replace")
        except Exception:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        counts = {}
        for node in root.findall("d"):
            text = (node.text or "").strip()
            if not text or len(text) > 50:
                continue
            counts[text] = counts.get(text, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))[:limit]
        return [text for text, _ in top]

    async def comments(self, aid: int, page: int = 1, page_size: int = None,
                       sort: str = "hot") -> dict:
        page_size = page_size or _cfg_int("bilibili_comments_page_size", 20)
        sort_value = 2 if sort == "hot" else 0
        data = await self._get_json(
            f"{_API}/x/v2/reply",
            {
                "type": 1,
                "oid": aid,
                "sort": sort_value,
                "ps": max(1, min(20, page_size)),
                "pn": max(1, int(page)),
            },
        )
        replies = []
        for raw in data.get("replies") or []:
            if not isinstance(raw, dict):
                continue
            member = raw.get("member") or {}
            content = raw.get("content") or {}
            replies.append({
                "rpid": raw.get("rpid"),
                "author": str(member.get("uname") or ""),
                "text": str(content.get("message") or ""),
                "like": raw.get("like"),
                "ctime": raw.get("ctime"),
                "reply_count": raw.get("rcount"),
            })
        return {
            "replies": replies,
            "page": page,
            "page_size": page_size,
            "total": (data.get("page") or {}).get("count"),
            "is_end": not bool(data.get("replies")),
        }

    async def watch(self, bvid: str, comment_page: int = 1) -> dict:
        info = await self.video_info(bvid)
        try:
            subtitles = await self.subtitles(bvid, info.get("cid"))
        except Exception as e:
            append_log(f"[Bilibili] 字幕获取失败（可无登录继续）: {e}")
            subtitles = ""
        danmaku = await self.danmaku(info.get("cid"))
        comments_data = {"replies": [], "is_end": True}
        if info.get("aid"):
            try:
                comments_data = await self.comments(
                    info["aid"], page=comment_page,
                    page_size=_cfg_int("bilibili_comments_page_size", 20),
                )
            except Exception as e:
                append_log(f"[Bilibili] 评论获取失败: {e}")
        return {
            **info,
            "subtitles": subtitles,
            "danmaku": danmaku,
            "comments": comments_data.get("replies") or [],
            "comments_page": comment_page,
            "comments_is_end": bool(comments_data.get("is_end")),
        }
