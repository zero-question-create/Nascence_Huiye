# plugins/bilibili/plugin.py
# ========================================================================
# Bilibili 插件：以账号只读浏览 B 站，并在软件打开后把内容交给主进程。
#
# 参考 dsh-bili-whale 的边界：
#   - 只读推荐流/搜索/视频信息/字幕/弹幕/评论；
#   - 不做批量刷量、不自动三连/投币/关注、不抓全站；
#   - Cookie 只从 config/bilibili_credentials.json 读取，不回显。
#
# 软件模型：
#   open_app   → 拉取推荐流，建立“视频对话列表”
#   open_conversation(bvid) → 取视频信息/字幕/弹幕/热评，交给 AppContext
#   read_earlier → 继续翻评论页
#   close_conversation / close_app → 回首页/退出
# ========================================================================

import asyncio
import re
import threading
import time

from config.api_config import config
from plugins.base import Capability, Plugin
from utils.monitor import append_log

from .client import BiliClient, BiliError, BiliRiskError

_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


def _cfg_int(key: str, default: int) -> int:
    try:
        return max(1, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def _short(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


class BilibiliPlugin(Plugin):
    plugin_id = "bilibili"
    label = "Bilibili"
    description = "以账号只读浏览 B 站；打开软件后由主进程决定看什么、看多久。"
    capabilities = (Capability.ACCOUNT, Capability.FEED, Capability.SEARCH)
    implemented = True
    is_app = True

    def __init__(self, settings: dict = None):
        super().__init__(settings)
        self._lock = threading.RLock()
        self._open = False
        self._current = ""
        self._feed = []
        self._feed_at = 0.0
        self._last_closed = time.time()
        self._comment_page = {}
        self._watched = {}
        self._client = None

    # ---------- 内部 ----------
    def _get_client(self) -> BiliClient:
        with self._lock:
            if self._client is None:
                self._client = BiliClient()
            return self._client

    def _cooldown_left(self) -> int:
        try:
            cooldown = max(0, int(config.get("bilibili_browse_cooldown_seconds", 1800)))
        except (TypeError, ValueError):
            cooldown = 1800
        return max(0, int(cooldown - (time.time() - self._last_closed)))

    def _defer(self):
        """打开失败时进入冷却，避免门控每轮重试。"""
        with self._lock:
            self._last_closed = time.time()

    @staticmethod
    def _conversation_label(title: str) -> str:
        return _short(title or "一个视频", 40)

    def _watch_lines(self, info: dict) -> list:
        lines = [
            f"视频：{info.get('title') or ''}",
            f"UP：{info.get('author') or ''}｜播放 {info.get('views') or 0}｜时长 {info.get('duration') or 0} 秒",
        ]
        if info.get("tags"):
            lines.append("标签：" + "、".join(info["tags"][:8]))
        if info.get("desc"):
            lines.append("简介：" + _short(info["desc"], 220))
        subtitles = str(info.get("subtitles") or "").strip()
        if subtitles:
            lines.append("字幕：" + _short(subtitles, 500))
        danmaku = info.get("danmaku") or []
        if danmaku:
            lines.append("弹幕里反复出现：" + "；".join(str(x) for x in danmaku[:8]))
        comments = info.get("comments") or []
        for idx, comment in enumerate(comments[:4], start=1):
            lines.append(
                f"热评{idx}（{comment.get('author') or '某人'}，{comment.get('like') or 0}赞）："
                + _short(comment.get("text") or "", 120)
            )
        return lines

    # ---------- 对外能力（供后续调度/其他插件调用）----------
    async def observe(self, limit: int = None) -> list:
        return await self._get_client().recommend(limit or _cfg_int("bilibili_feed_limit", 8))

    async def start(self) -> None:
        # 没有常驻连接；打开软件时才访问 B 站。
        return None

    async def stop(self) -> None:
        await self.close_app()

    async def search(self, query: str, limit: int = 10) -> list:
        return await self._get_client().search(query, limit=limit)

    async def watch(self, bvid: str) -> dict:
        return await self._get_client().watch(bvid)

    # ---------- 软件状态 ----------
    def app_status(self) -> dict:
        with self._lock:
            conversations = [
                {"id": item["bvid"], "label": self._conversation_label(item.get("title")), "unread": 0}
                for item in self._feed
            ]
            unread = 0 if self._open or self._cooldown_left() > 0 else 1
            return {
                "id": self.plugin_id,
                "label": self.label,
                "open": bool(self._open),
                "unread": unread,
                "current_conversation": self._current,
                "conversations": conversations,
            }

    async def open_app(self) -> dict:
        with self._lock:
            if self._open:
                return {"ok": True, "detail": "Bilibili 已经打开了"}
        try:
            feed = await self.observe()
        except BiliRiskError as e:
            self._defer()
            return {"ok": False, "detail": f"Bilibili 风控暂停：{e}"}
        except BiliError as e:
            self._defer()
            return {"ok": False, "detail": f"打开 Bilibili 失败：{e}"}
        except Exception as e:
            self._defer()
            return {"ok": False, "detail": f"打开 Bilibili 失败：{e}"}
        if not feed:
            self._defer()
            return {"ok": False, "detail": "Bilibili 没有可浏览的推荐（检查 Cookie 与网络）"}
        with self._lock:
            self._open = True
            self._current = ""
            self._feed = list(feed)
            self._feed_at = time.time()
        append_log(f"[Bilibili] 打开了 Bilibili，推荐 {len(feed)} 条")
        return {
            "ok": True,
            "detail": f"我打开了Bilibili，看到 {len(feed)} 个推荐视频",
            "content_lines": [
                f"{idx}. {item.get('title') or ''}（{item.get('author') or ''}）"
                for idx, item in enumerate(feed[:6], start=1)
            ],
        }

    async def close_app(self) -> dict:
        with self._lock:
            self._open = False
            self._current = ""
            self._feed = []
            self._last_closed = time.time()
            client = self._client
            self._client = None
        try:
            if client is not None:
                await client.close()
        except Exception:
            pass
        return {"ok": True, "detail": "我关掉了Bilibili"}

    async def open_conversation(self, conversation_id: str) -> dict:
        bvid = str(conversation_id or "").strip()
        if not _BVID_RE.match(bvid):
            return {"ok": False, "detail": "这不是一个有效的 BV 号"}
        with self._lock:
            if not self._open:
                return {"ok": False, "detail": "Bilibili 还没有打开"}
        try:
            info = await self.watch(bvid)
        except BiliRiskError as e:
            return {"ok": False, "detail": f"Bilibili 风控暂停：{e}"}
        except BiliError as e:
            return {"ok": False, "detail": f"看视频失败：{e}"}
        except Exception as e:
            return {"ok": False, "detail": f"看视频失败：{e}"}
        with self._lock:
            self._current = bvid
            self._comment_page[bvid] = 1
            self._watched[bvid] = info
        try:
            from core.memory_engine import create_memory
            summary = (
                f"我在B站看了视频「{info.get('title') or bvid}」，"
                f"UP主{info.get('author') or '未知'}。"
                f"{str(info.get('desc') or '')[:120]}"
            )
            await asyncio.to_thread(create_memory, summary)
        except Exception as e:
            append_log(f"[Bilibili] 观看记忆写入失败: {e}")
        lines = self._watch_lines(info)
        append_log(f"[Bilibili] 点开视频 {bvid}: {info.get('title')}")
        return {
            "ok": True,
            "detail": f"我点开了视频「{info.get('title') or bvid}」",
            "conversation": bvid,
            "content_lines": lines,
        }

    async def close_conversation(self) -> dict:
        with self._lock:
            if not self._current:
                return {"ok": True, "detail": "已经在 Bilibili 首页了"}
            self._current = ""
        return {"ok": True, "detail": "我退出了这个视频，回到了Bilibili首页"}

    async def read_earlier(self, conversation_id: str = "") -> dict:
        with self._lock:
            bvid = str(conversation_id or self._current or "").strip()
            info = self._watched.get(bvid) or {}
            if not bvid or not info:
                return {"ok": False, "detail": "还没打开视频，没有评论可翻"}
            if info.get("comments_is_end"):
                return {"ok": False, "detail": "这个视频的评论已经翻到底了"}
            next_page = int(self._comment_page.get(bvid, 1)) + 1
        try:
            data = await self._get_client().comments(
                info.get("aid"), page=next_page,
                page_size=_cfg_int("bilibili_comments_page_size", 20),
            )
        except BiliError as e:
            return {"ok": False, "detail": f"翻评论失败：{e}"}
        replies = data.get("replies") or []
        with self._lock:
            self._comment_page[bvid] = next_page
            info["comments_is_end"] = bool(data.get("is_end"))
            self._watched[bvid] = info
        if not replies:
            return {"ok": False, "detail": "没有更多评论了"}
        lines = [
            f"（{c.get('author') or '某人'}，{c.get('like') or 0}赞）：{_short(c.get('text') or '', 120)}"
            for c in replies[:8]
        ]
        return {
            "ok": True,
            "detail": f"我往下翻了翻评论，第 {next_page} 页",
            "conversation": bvid,
            "content_lines": lines,
        }


PLUGIN = BilibiliPlugin
