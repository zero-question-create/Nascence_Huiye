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
import re
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
        self._history_complete = False
        self._current_explicit = False
        self._current_is_group = False
        self._self_name = ""
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

    async def send_clipboard_to_current(self) -> dict:
        """把当前剪贴板文本原样发到已打开的 QQ 对话。

        这是“剪贴板 → 当前聊天”的正规入口：复用 QQ 插件自己的输入/发送
        链路，不需要电脑插件去猜输入框位置，也不会在聊天页反复 snapshot。
        """
        with self._lock:
            hwnd = int(self._hwnd or 0)
            current = str(self._current_label or "").strip()
        if not hwnd or not current:
            return {
                "ok": False,
                "detail": "QQ 还没有打开具体对话；先点开要发送的会话，再用 send_clipboard。",
            }
        loop = asyncio.get_running_loop()

        def _read_clipboard():
            return engine.uia.backend().clipboard_get()

        try:
            clip = await loop.run_in_executor(None, _read_clipboard)
        except Exception as exc:
            return {"ok": False, "detail": f"读剪贴板失败：{exc}"}
        if not isinstance(clip, dict) or not clip.get("ok"):
            return {
                "ok": False,
                "detail": f"读剪贴板失败：{(clip or {}).get('error') or '未知原因'}",
            }
        text = str(clip.get("text") or "").strip()
        if not text:
            return {"ok": False, "detail": "剪贴板是空的，没有可发送的内容。"}
        preview = " ".join(text.split())[:100]
        sent = await self.send_text_to_current(current, text)
        if not sent:
            return {
                "ok": False,
                "detail": f"剪贴板内容没能发到{current}，QQ 输入框可能没有聚焦。",
            }
        return {
            "ok": True,
            "detail": f"我把剪贴板内容发给了{current}：{preview}",
            "content_lines": [],
        }

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
                unread = engine.effective_unread(text, conversations)
                self_name = engine.parse_self_name(text)
                with self._lock:
                    self._hwnd = hwnd
                    self._conversations = conversations
                    self._unread = unread
                    current = self._current
                # QQ 在前台/窗口开着时，新消息会被客户端直接标成已读，
                # unread 可能是 0；这时要靠聊天区标题识别当前对话并主动轮询。
                header = engine.parse_current_conversation(text, self_name)
                is_group = engine.conversation_is_group(text, self_name) if header else False
                with self._lock:
                    explicit = bool(self._current_explicit)
                allow_group = bool(is_group and explicit and header == current)
                if header and bool(is_group) and not allow_group:
                    # 群聊不自动接管：群里每句话都触发动作会把她拖进别人的话题。
                    if current:
                        current = ""
                        with self._lock:
                            self._current = ""
                            self._current_label = ""
                            self._current_is_group = False
                            self._current_explicit = False
                elif header and header != current:
                    current = header
                    with self._lock:
                        self._open = True
                        self._current = current
                        self._current_label = current
                        self._current_is_group = bool(is_group)
                        self._current_explicit = False
                        self._self_name = self_name
                elif not header and 'Pane "消息列表"' not in text and current:
                    # 聊天区被收起/切回会话列表：清掉当前对话，恢复未读摘要。
                    current = ""
                    with self._lock:
                        self._current = ""
                        self._current_label = ""
                        self._current_is_group = False
                        self._current_explicit = False
                self._push_unread_preview(conversations, active=bool(current))
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
            self._history_complete = False
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
        initial_text = engine.snapshot_text(hwnd)
        self_name = engine.parse_self_name(initial_text)
        is_group = engine.conversation_is_group(initial_text, self_name)
        view = engine.extract_messages(hwnd, name, self_name)
        initial_count = len(view)
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
            # 群聊一开始只有当前窗口、翻页也没加载出更多时，不能声称“完整”。
            self._history_complete = (len(transcript) > initial_count) or not is_group
        self._publish_transcript(name, transcript)
        return transcript

    def _publish_transcript(self, current: str, messages: list) -> None:
        """把当前窗口的对话摘录写成独立 state（……表示未显示的消息）。"""
        with self._lock:
            viewport = list(self._last_messages)
            history_complete = bool(self._history_complete)
            is_group = bool(self._current_is_group)
            self_name = str(self._self_name or "")
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
        clipped_above = start > 0 or not history_complete
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
        history_note = "；历史未完全加载" if not history_complete else ""
        group_note = ""
        if is_group:
            who = self_name or "我"
            group_note = (
                f"；这是群聊：只有明确 @{who} 或点名需要{who}回应时才处理，"
                "其他人的聊天只当背景，不要主动接任务"
            )
        header = (
            f"{current} 对话摘录（独立记忆，不是逐条消息；"
            f"当前窗口 {len(viewport)}/{len(transcript)} 条{history_note}；"
            f"……表示未显示{group_note}）："
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
            with self._lock:
                is_group = bool(self._current_is_group)
                self_name = str(self._self_name or "")
            if is_group:
                # 群聊里只有明确点名/艾特她时才产生“新消息”冲动；
                # 其他人的闲聊仍会进摘录做背景，但不触发动作。
                incoming = [
                    message for message in incoming
                    if self_name and self_name in str(message.get("text") or "")
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

    def _push_unread_preview(self, conversations: list, active: bool = False) -> None:
        """进入某个会话后清掉未读提醒；其余交给 AppContext 的“变化提醒”。

        数量的首次通知由 AppContext.refresh() 负责，且只在数量变化时
        重新进入提示词。这里不能每 5 秒覆盖一次状态，否则同一句
        “90 条未读”会一直摆在辉夜眼前，把她带进反复念叨的循环。
        """
        try:
            from core.apps import APP_CONTEXT

            with self._lock:
                if active or (self._open and self._current_label):
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
            {
                "id": item["id"],
                "label": item["label"],
                "unread": min(int(item.get("unread") or 0), 99),
            }
            for item in conversations
        ]
        return {
            "id": self.plugin_id,
            "label": self.label,
            "open": bool(self._open and hwnd),
            # 只把“有未读”作为门控信号，不把数万条原始计数灌给语言层。
            "unread": min(int(unread or 0), 99),
            "current_conversation": label if current else "",
            "conversations": conversations_out,
            "at_bottom": True,
        }

    async def open_app(self) -> dict:
        with self._lock:
            if self._open:
                return {"ok": True, "detail": "QQ 已经打开了"}
        hwnd = engine.find_window()
        if not hwnd:
            return {"ok": False, "detail": "QQ 窗口没有打开"}
        text = engine.snapshot_text(hwnd)
        conversations = engine.parse_conversations(text)
        unread = engine.effective_unread(text, conversations)
        with self._lock:
            self._open = True
            self._hwnd = hwnd
            self._conversations = conversations
            self._unread = unread
        if unread:
            detail = f"我打开了QQ，有 {engine.format_unread(unread)} 条未读"
        else:
            detail = "我打开了QQ，没有未读消息"
        return {
            "ok": True,
            "detail": detail,
            "conversations": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "unread": min(int(item.get("unread") or 0), 99),
                }
                for item in conversations
            ],
            "unread": min(int(unread or 0), 99),
        }

    async def open_conversation(self, conversation_id: str) -> dict:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return {"ok": False, "detail": "没有指定要打开哪个对话"}
        hwnd = engine.find_window()
        if not hwnd:
            return {"ok": False, "detail": "QQ 窗口没有打开"}
        use_vision = bool(config.get("qq_desktop_use_vision", True))
        # 动作层有时把“名字（id）”整串传来；拆成候选再匹配。
        candidates = [conversation_id]
        match = re.match(r"^(.+?)[（(]([^）)]+)[）)]$", conversation_id)
        if match:
            candidates.extend([match.group(1).strip(), match.group(2).strip()])
        candidates = list(dict.fromkeys(c for c in candidates if c))
        result = {}
        used = conversation_id
        for candidate in candidates:
            used = candidate
            result = await engine.open_conversation(
                hwnd, candidate, name=candidate, use_vision=use_vision
            )
            if result.get("ok"):
                break
        if not result.get("ok"):
            return result
        name = str(result.get("name") or conversation_id)
        content = str(result.get("content") or "")
        text = engine.snapshot_text(hwnd)
        conversations = engine.parse_conversations(text)
        self_name = engine.parse_self_name(text)
        is_group = engine.conversation_is_group(text, self_name)
        with self._lock:
            stored = self._transcript_conversation
            existing = len(self._transcript)
            # 先把当前对话标记写好，_load_history 发布摘录时才能带上群聊提示。
            self._open = True
            self._hwnd = hwnd
            self._current = name
            self._current_label = name
            self._current_explicit = True
            self._current_is_group = bool(is_group)
            self._self_name = self_name
            self._conversations = conversations
            self._unread = engine.effective_unread(text, conversations)
        if stored != name or existing < 5 or not self._history_complete:
            transcript = self._load_history(hwnd, name)
        else:
            messages = engine.extract_messages(hwnd, name, self_name)
            self._merge_transcript(name, messages)
            with self._lock:
                transcript = list(self._transcript)
        lines = engine.format_messages(transcript, limit=20).splitlines()
        if not lines:
            lines = [ln for ln in content.splitlines() if ln.strip()][:20]
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
            self._current_explicit = False
            self._current_is_group = False
        return {"ok": True, "detail": "我退出了这个对话"}

    async def close_app(self) -> dict:
        with self._lock:
            self._open = False
            self._current = ""
            self._current_label = ""
            self._last_messages = []
            self._transcript = []
            self._transcript_conversation = ""
            self._current_explicit = False
            self._current_is_group = False
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
