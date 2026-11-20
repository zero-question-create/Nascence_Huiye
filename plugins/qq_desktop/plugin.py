# plugins/qq_desktop/plugin.py
# ========================================================================
# QQ 桌面读取插件：不登录协议、不使用 NapCat，直接读已打开的 QQ 窗口。
#
# 为什么需要它：NapCat 登录会顶号，官方客户端又无法被协议直接读。
# 这里改用 QQ 自己的无障碍树：
#   - 会话列表里的 Group "N条未读" 就是未读数；
#   - 点开会话后读消息区；树里没有就用截图 + 视觉模型兜底。
# ========================================================================

from __future__ import annotations

import asyncio
import threading
import time

from config.api_config import config
from plugins.base import BehaviorChannel, Capability, Plugin

from . import desktop_engine as engine


class QQDesktopChannel(BehaviorChannel):
    """把语言层的回复送进 QQ 当前会话。"""

    plugin_id = "qq_desktop"
    source = "QQ（桌面读取）"

    def __init__(self, plugin):
        self.plugin = plugin

    def resolve_target(self, fallback=None):
        current = self.plugin.current_conversation()
        if current:
            return current
        if self.plugin.window_foreground():
            return "__current__"
        return fallback or ""

    def outbound_options(self, target=None) -> dict:
        return {}

    async def send_text(self, target: str, text: str, **kwargs) -> bool:
        return await self.plugin.send_text_to_current(target, text)

    async def send_media(self, target: str, path: str, as_sticker: bool = False) -> bool:
        return False

    def status(self) -> dict:
        return {
            "plugin_id": self.plugin_id,
            "source": self.source,
            "conversation": self.plugin.current_conversation(),
        }


class QQDesktopPlugin(Plugin):
    plugin_id = "qq_desktop"
    label = "QQ（桌面读取）"
    description = "不登录 QQ 协议，直接读已打开的 QQ 窗口；未读来自窗口里的“N条未读”。"
    capabilities = (Capability.CHAT, Capability.GROUP_CHAT)
    implemented = True
    is_app = True

    def __init__(self, settings: dict = None):
        super().__init__(settings)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._open = False
        self._hwnd = 0
        self._unread = 0
        self._conversations = []
        self._current = ""
        self._current_label = ""
        self._last_messages = []
        self._transcript = []
        self._transcript_conversation = ""
        self._last_image_sig = None
        self._channel = QQDesktopChannel(self)

    # ---------- 服务 ----------
    async def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="QQDesktopReader", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        with self._lock:
            self._open = False

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def current_conversation(self) -> str:
        with self._lock:
            return self._current_label

    def window_hwnd(self) -> int:
        with self._lock:
            return int(self._hwnd or 0)

    def window_foreground(self) -> bool:
        hwnd = self.window_hwnd()
        try:
            return bool(hwnd and engine.uia.foreground_window() == hwnd)
        except Exception:
            return False

    def channel(self):
        if not (self._open or self._hwnd):
            return None
        return self._channel

    async def send_text_to_current(self, target: str, text: str) -> bool:
        with self._lock:
            hwnd = self._hwnd
            current = self._current_label
        if not hwnd:
            return False
        target = str(target or "").strip()
        loop = asyncio.get_running_loop()
        if target and target != "__current__" and target != current:
            activated = await loop.run_in_executor(
                None, engine.activate_conversation, hwnd, target
            )
            if not activated:
                return False
            with self._lock:
                self._current = target
                self._current_label = target
        sent = await loop.run_in_executor(None, engine.send_text, hwnd, text)
        if not sent:
            return False
        with self._lock:
            self._open = True
            if target:
                self._current = target
                self._current_label = target
        try:
            messages = await loop.run_in_executor(
                None, engine.extract_messages, hwnd, target or current, ""
            )
            self._merge_transcript(target or current, messages)
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember(f"我在QQ里对{target or current}说：{text}")
        except Exception:
            pass
        return True

    # ---------- 监控 ----------
    @staticmethod
    def _poll_seconds() -> float:
        try:
            return max(1.0, float(config.get("qq_desktop_poll_seconds", 5)))
        except (TypeError, ValueError):
            return 5.0

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                hwnd = engine.find_window()
                if not hwnd:
                    with self._lock:
                        self._hwnd = 0
                        self._unread = 0
                        self._conversations = []
                    self._stop.wait(self._poll_seconds())
                    continue
                text = engine.snapshot_text(hwnd)
                conversations = engine.parse_conversations(text)
                unread = engine.total_unread(conversations)
                self_name = engine.parse_self_name(text)
                with self._lock:
                    self._hwnd = hwnd
                    self._conversations = conversations
                    self._unread = unread
                    current = self._current
                self._push_unread_preview(conversations)
                # QQ 在前台/窗口开着时，新消息会被客户端直接标成已读，
                # unread 可能是 0；这时要靠聊天区标题识别当前对话并主动轮询。
                header = engine.parse_current_conversation(text, self_name)
                if header and header != current:
                    current = header
                    with self._lock:
                        self._open = True
                        self._current = current
                        self._current_label = current
                if current:
                    messages = engine.extract_messages(hwnd, current, self_name)
                    with self._lock:
                        stored = self._transcript_conversation
                    if messages and stored != current:
                        self._seed_transcript(current, messages)
                    else:
                        self._poll_new_messages(messages, current)
                    self._maybe_describe_image(hwnd, current, messages)
            except Exception:
                pass
            self._stop.wait(self._poll_seconds())

    def _seed_transcript(self, current: str, messages: list) -> None:
        """第一次识别到当前对话时先建基线，不能把整段历史当新消息。"""
        if not messages:
            return
        transcript = list(messages)[-500:]
        with self._lock:
            self._transcript = transcript
            self._transcript_conversation = current
            self._last_messages = list(messages)
        self._publish_transcript(current, transcript)

    def _merge_transcript(self, current: str, messages: list) -> list:
        """把当前消息窗口并入完整记录，返回真正新增的尾部消息。"""
        with self._lock:
            existing = list(self._transcript)
            stored = self._transcript_conversation
        if stored and stored != current:
            existing = []
        merged, appended = engine.merge_messages(existing, messages)
        merged = merged[-500:]
        with self._lock:
            self._transcript = merged
            self._transcript_conversation = current
            self._last_messages = list(messages)
        self._publish_transcript(current, merged)
        return appended

    def _load_history(self, hwnd: int, name: str) -> list:
        """向上逐页加载历史并合并，避免上下文只拿到当前可视窗口。"""
        try:
            pages = int(config.get("qq_desktop_history_pages", 12))
        except (TypeError, ValueError):
            pages = 12
        pages = max(1, min(40, pages))
        self_name = engine.parse_self_name(engine.snapshot_text(hwnd))
        view = engine.extract_messages(hwnd, name, self_name)
        transcript = list(view)
        stale = 0
        for _ in range(pages):
            engine.scroll_chat(hwnd, "up", 1)
            time.sleep(0.15)
            view = engine.extract_messages(hwnd, name, self_name)
            if not view:
                stale += 1
            else:
                merged, _appended = engine.merge_messages(transcript, view)
                if len(merged) == len(transcript):
                    stale += 1
                else:
                    transcript = merged
                    stale = 0
            if stale >= 2:
                break
        engine.scroll_chat(hwnd, "down", pages + 2)
        time.sleep(0.3)
        view = engine.extract_messages(hwnd, name, self_name)
        merged, _appended = engine.merge_messages(transcript, view)
        transcript = merged[-500:]
        with self._lock:
            self._transcript = transcript
            self._transcript_conversation = name
            self._last_messages = list(view)
        self._publish_transcript(name, transcript)
        return transcript

    def _publish_transcript(self, current: str, messages: list) -> None:
        """把当前窗口的对话摘录写成独立 state（……表示未显示的消息）。"""
        with self._lock:
            viewport = list(self._last_messages)
        transcript = list(messages or [])
        if not transcript and not viewport:
            try:
                from core.apps import APP_CONTEXT

                APP_CONTEXT.remember_state("qq_desktop_dialog_excerpt", "")
                APP_CONTEXT.remember_state("qq_desktop_transcript", "")
            except Exception:
                pass
            return

        old_keys = [engine.message_key(item) for item in transcript]
        new_keys = [engine.message_key(item) for item in viewport]
        start = end = None
        if new_keys and old_keys:
            for index in range(len(old_keys) - len(new_keys) + 1):
                if old_keys[index:index + len(new_keys)] == new_keys:
                    start, end = index, index + len(new_keys)
        if start is None:
            # 读不到窗口位置时，至少展示最后一段。
            end = len(transcript)
            start = max(0, end - 20)
            viewport = transcript[start:end]
        else:
            viewport = transcript[start:end]

        max_lines = 40
        clipped_above = start > 0
        if len(viewport) > max_lines:
            viewport = viewport[-max_lines:]
            clipped_above = True
        clipped_below = end < len(transcript)

        lines = engine.format_messages(
            viewport, limit=max(1, len(viewport))
        ).splitlines()
        parts = []
        if clipped_above:
            parts.append("……")
        parts.extend(lines)
        if clipped_below:
            parts.append("……")
        if not parts:
            return
        header = (
            f"{current} 对话摘录（独立记忆，不是逐条消息；"
            f"当前窗口 {len(viewport)}/{len(transcript)} 条；……表示未显示）："
        )
        body = header + "\n" + "\n".join(parts)
        if len(body) > 7000:
            body = header + "\n" + body[-(7000 - len(header) - 1):]
        try:
            from core.apps import APP_CONTEXT

            # 旧的完整记录 state 一并清掉，避免同一段对话出现两份。
            APP_CONTEXT.remember_state("qq_desktop_transcript", "")
            APP_CONTEXT.remember_state("qq_desktop_dialog_excerpt", body)
        except Exception:
            pass

    def _poll_new_messages(self, messages: list, current: str) -> None:
        # 聊天区临时收起/窗口重绘时会读成空列表；这时不能把基线清空，
        # 否则下次恢复会把整段历史当成“新消息”再灌一遍。
        if not messages:
            return
        new_items = self._merge_transcript(current, messages)
        if not new_items:
            return
        try:
            from core.apps import APP_CONTEXT

            incoming = [
                message for message in new_items
                # 自己发出去的消息已经由发送动作记过“我在QQ里说”，
                # 绝不能反过来当成对方的新消息再喂给理解层。
                if message.get("direction") == "other"
                and str(message.get("text") or "").strip()
                and message.get("text") != "【图片】"
            ]
            if incoming:
                senders = []
                for message in incoming:
                    sender = str(message.get("sender") or current).strip()
                    if sender and sender not in senders:
                        senders.append(sender)
                # 消息内容只放进独立的“对话摘录”，普通记忆里只留一条提示，
                # 避免同一段聊天既进 state 又逐条灌进 40 条追加区。
                APP_CONTEXT.remember(
                    f"QQ 对话有新消息（{len(incoming)} 条，来自"
                    f"{'、'.join(senders) or current}；内容见 QQ 对话摘录）"
                )
        except Exception:
            pass

    def _maybe_describe_image(self, hwnd: int, current: str, messages: list) -> None:
        """当前会话里出现图片时，主动截图交给视觉模型，而不是只报文字。"""
        if not bool(config.get("qq_desktop_use_vision", True)):
            return
        try:
            if not any(m.get("text") == "【图片】" for m in messages):
                return
            signature = (current, tuple(engine.message_key(m) for m in messages[-5:]))
            if signature == self._last_image_sig:
                return
            self._last_image_sig = signature
            content = asyncio.run(
                engine.read_current_chat(hwnd, current, use_vision=True)
            )
            if not content:
                return
            # 图片描述只代表“这一刻看到的画面”，用 state 覆盖，不追加，
            # 否则每次轮询都会把同一段视觉描述再灌进上下文。
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember_state("qq_desktop_image", content[:1800])
        except Exception:
            pass

    def _push_unread_preview(self, conversations: list) -> None:
        """把未读会话的名字和最后一条预览写进软件上下文。

        UIA 的会话列表本身带 preview；这样不用点开会话，辉夜也能知道
        “谁有未读、最后一条预览是什么”，完整消息再由 open_conversation 读取。
        preview 不保证来自对方，所以不能当成对方原话注入。
        """
        try:
            from core.apps import APP_CONTEXT

            lines = []
            for item in conversations:
                unread = int(item.get("unread") or 0)
                if unread <= 0:
                    continue
                label = str(item.get("label") or item.get("id") or "QQ")
                preview = str(item.get("preview") or "").strip()
                line = f"{label} 有 {unread} 条未读"
                if preview:
                    # 会话列表的 preview 只代表“最后一条”，不保证是对方发的；
                    # 直接写成“对方：…”会让理解层把自己的最后一句当成新消息。
                    line += f"；最后一条预览（不一定是对方）：{preview}"
                lines.append(line)
            if lines:
                APP_CONTEXT.remember_state(
                    "qq_desktop_unread",
                    "QQ 未读预览：\n" + "\n".join(lines[:10]),
                )
            else:
                APP_CONTEXT.remember_state("qq_desktop_unread", "")
        except Exception:
            pass

    # ---------- 软件状态 ----------
    def app_status(self) -> dict:
        with self._lock:
            hwnd = self._hwnd
            unread = self._unread
            conversations = list(self._conversations)
            current = self._current
            label = self._current_label
        conversations_out = [
            {"id": item["id"], "label": item["label"], "unread": item["unread"]}
            for item in conversations
        ]
        return {
            "id": self.plugin_id,
            "label": self.label,
            "open": bool(self._open and hwnd),
            "unread": unread,
            "current_conversation": label if current else "",
            "conversations": conversations_out,
            "at_bottom": True,
        }

    async def open_app(self) -> dict:
        hwnd = engine.find_window()
        if not hwnd:
            return {"ok": False, "detail": "QQ 窗口没有打开"}
        text = engine.snapshot_text(hwnd)
        conversations = engine.parse_conversations(text)
        unread = engine.total_unread(conversations)
        with self._lock:
            self._open = True
            self._hwnd = hwnd
            self._conversations = conversations
            self._unread = unread
        if unread:
            detail = f"我打开了QQ，有 {unread} 条未读"
        else:
            detail = "我打开了QQ，没有未读消息"
        return {
            "ok": True,
            "detail": detail,
            "conversations": [
                {"id": item["id"], "label": item["label"], "unread": item["unread"]}
                for item in conversations
            ],
            "unread": unread,
        }

    async def open_conversation(self, conversation_id: str) -> dict:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return {"ok": False, "detail": "没有指定要打开哪个对话"}
        hwnd = engine.find_window()
        if not hwnd:
            return {"ok": False, "detail": "QQ 窗口没有打开"}
        use_vision = bool(config.get("qq_desktop_use_vision", True))
        result = await engine.open_conversation(
            hwnd, conversation_id, name=conversation_id, use_vision=use_vision
        )
        if not result.get("ok"):
            return result
        name = str(result.get("name") or conversation_id)
        content = str(result.get("content") or "")
        text = engine.snapshot_text(hwnd)
        conversations = engine.parse_conversations(text)
        with self._lock:
            stored = self._transcript_conversation
            existing = len(self._transcript)
        if stored != name or existing < 5:
            transcript = self._load_history(hwnd, name)
        else:
            self_name = engine.parse_self_name(text)
            messages = engine.extract_messages(hwnd, name, self_name)
            self._merge_transcript(name, messages)
            with self._lock:
                transcript = list(self._transcript)
        lines = engine.format_messages(transcript, limit=20).splitlines()
        if not lines:
            lines = [ln for ln in content.splitlines() if ln.strip()][:20]
        with self._lock:
            self._open = True
            self._hwnd = hwnd
            self._current = name
            self._current_label = name
            self._conversations = conversations
            self._unread = engine.total_unread(conversations)
        return {
            "ok": True,
            "detail": f"我点开了{name}的对话，整理了 {len(transcript)} 条对话摘录",
            "conversation": name,
            "content_lines": [
                "详细内容已放入独立的 QQ 对话摘录（……表示未显示部分）"
            ],
        }

    async def read_earlier(self, conversation_id: str = "") -> dict:
        with self._lock:
            hwnd = self._hwnd
            name = self._current_label
        if not hwnd or not name:
            return {"ok": False, "detail": "还没有打开 QQ 对话"}
        engine.scroll_chat(hwnd, "up", 1)
        use_vision = bool(config.get("qq_desktop_use_vision", True))
        content = await engine.read_current_chat(hwnd, name, use_vision=use_vision)
        lines = [ln for ln in content.splitlines() if ln.strip()][:20]
        self_name = engine.parse_self_name(engine.snapshot_text(hwnd))
        messages = engine.extract_messages(hwnd, name, self_name)
        self._merge_transcript(name, messages)
        with self._lock:
            total = len(self._transcript)
        return {
            "ok": True,
            "detail": f"我往上翻了一页{name}，对话摘录已有 {total} 条",
            "content_lines": [
                "更早内容已合并到独立的 QQ 对话摘录（……表示未显示部分）"
            ],
        }

    async def scroll_to_bottom(self) -> dict:
        with self._lock:
            hwnd = self._hwnd
        if not hwnd:
            return {"ok": False, "detail": "QQ 窗口没有打开"}
        engine.scroll_chat(hwnd, "down", 10)
        return {"ok": True, "detail": "我滚回最新消息附近"}

    async def close_conversation(self) -> dict:
        with self._lock:
            self._current = ""
            self._current_label = ""
            self._last_messages = []
        return {"ok": True, "detail": "我退出了这个对话"}

    async def close_app(self) -> dict:
        with self._lock:
            self._open = False
            self._current = ""
            self._current_label = ""
            self._last_messages = []
            self._transcript = []
            self._transcript_conversation = ""
        try:
            from core.apps import APP_CONTEXT

            APP_CONTEXT.remember_state("qq_desktop_transcript", "")
            APP_CONTEXT.remember_state("qq_desktop_dialog_excerpt", "")
        except Exception:
            pass
        return {"ok": True, "detail": "我关掉了 QQ（窗口还在）"}

    # ---------- 面板 ----------
    def conflict_note(self, others: list) -> str:
        for item in others or []:
            if getattr(item, "plugin_id", "") == "qq_napcat":
                return "QQ 桌面读取和 NapCat 都会读同一个账号：同时运行会重复，NapCat 登录还可能顶号。建议只开一个。"
        return ""

    def status(self) -> dict:
        data = super().status()
        with self._lock:
            data.update({
                "running": self.is_running(),
                "window": bool(self._hwnd),
                "unread": self._unread,
                "current": self._current_label,
            })
        return data


PLUGIN = QQDesktopPlugin
