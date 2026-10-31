# core/apps.py
# ========================================================================
# “软件”上下文：插件以 App 的形式向主进程传递状态与内容。
#
# 未打开软件时：这里只保留“某软件有 N 条未读”的提醒，不塞入消息正文。
# 打开软件/对话后：插件把内容作为 AppContext 条目传进主进程，
# 认知循环再把这些条目作为 pinned 记忆交给行为思考。
#
# 插件不直接改行为提示词，也不把平台元数据写进提示词；AppContext
# 只追加“她现在看到/知道的内容”，与自己的短期想法相互独立。
# ========================================================================

import threading
from collections import deque
from typing import List, Tuple

from config.api_config import config
from core.virtual_clock import clock


def _max_lines_from_config() -> int:
    try:
        return max(5, int(config.get("app_context_max_lines", 40)))
    except (TypeError, ValueError):
        return 40


class AppContext:
    """主进程侧的软件上下文缓冲区（线程安全）。"""

    def __init__(self, max_lines: int = None):
        self._lock = threading.RLock()
        self._max_lines = max(5, int(max_lines)) if max_lines else _max_lines_from_config()
        self._entries: deque = deque(maxlen=self._max_lines)
        self._notifications: dict = {}
        self._current_app = ""
        self._current_conversation = ""

    # ---------- 基础读写 ----------
    def remember(self, text: str):
        """插件/动作把“她现在看到的内容”送进主进程。"""
        text = str(text or "").strip()
        if not text:
            return
        real_ts = clock.to_real_time(clock.now())
        with self._lock:
            self._entries.append((real_ts, f"[现在] {text}"))

    def snapshot(self) -> List[Tuple[float, str]]:
        """返回 (时间戳, 文本)，供行为思考作为 pinned 记忆。"""
        with self._lock:
            real_ts = clock.to_real_time(clock.now())
            lines = [(real_ts, text) for text in self._notifications.values()]
            lines.extend(self._entries)
            return lines

    def texts(self) -> List[str]:
        return [text for _, text in self.snapshot()]

    def timestamps(self) -> List[float]:
        return [ts for ts, _ in self.snapshot()]

    def latest_line(self) -> str:
        with self._lock:
            if self._notifications:
                return next(iter(self._notifications.values()))
            if self._entries:
                return self._entries[-1][1]
        return ""

    def last_content_ts(self) -> float:
        """最近一次软件内容/状态写入时间，供本地路由判断是否还在活动。"""
        with self._lock:
            return float(self._entries[-1][0]) if self._entries else 0.0

    def has_context(self) -> bool:
        with self._lock:
            return bool(self._notifications or self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._notifications.clear()
            self._current_app = ""
            self._current_conversation = ""

    @property
    def current_app(self) -> str:
        with self._lock:
            return self._current_app

    @property
    def current_conversation(self) -> str:
        with self._lock:
            return self._current_conversation

    # ---------- 软件状态 ----------
    @staticmethod
    def _plugins():
        try:
            from plugins import registry
            return registry.app_plugins()
        except Exception:
            return []

    def prompt_state(self) -> List[dict]:
        """给动作 LLM 的软件清单：只含状态，不含消息正文。"""
        result = []
        for plugin in self._plugins():
            try:
                state = plugin.app_status()
            except Exception:
                continue
            if not isinstance(state, dict):
                continue
            item = {
                "id": str(state.get("id") or plugin.plugin_id),
                "label": str(state.get("label") or plugin.label or plugin.plugin_id),
                "open": bool(state.get("open")),
                "unread": int(state.get("unread") or 0),
                "current_conversation": str(state.get("current_conversation") or ""),
                "at_bottom": state.get("at_bottom"),
                "conversations": list(state.get("conversations") or [])[:10],
            }
            result.append(item)
        return result

    def refresh(self):
        """按插件当前状态重建未读/打开提醒；每轮认知循环调用一次。"""
        notifications = {}
        current_app = ""
        current_conversation = ""
        for app in self.prompt_state():
            app_id = app["id"]
            if app["open"]:
                current_app = app_id
                current_conversation = app["current_conversation"]
                state = f"{app['label']}打开着"
                if current_conversation:
                    state += f"，正在看{self._conversation_label(app, current_conversation)}"
                if app["unread"] > 0:
                    state += f"；另外还有 {app['unread']} 条未读"
                notifications[f"__open__{app_id}"] = state
            elif app["unread"] > 0:
                notifications[app_id] = f"{app['label']}有 {app['unread']} 条未读消息"
        with self._lock:
            self._notifications = notifications
            self._current_app = current_app
            self._current_conversation = current_conversation

    @staticmethod
    def _conversation_label(app: dict, conversation_id: str) -> str:
        for conv in app.get("conversations") or []:
            if str(conv.get("id")) == str(conversation_id):
                return str(conv.get("label") or conversation_id)
        return str(conversation_id)

    def has_actions(self) -> bool:
        """是否有值得让动作层决断的软件事项（未读或已打开）。"""
        return any(app.get("unread") or app.get("open") for app in self.prompt_state())

    def any_app_open(self) -> bool:
        """当前是否有软件处于打开状态。"""
        return any(bool(app.get("open")) for app in self.prompt_state())

    def open_apps(self) -> List[dict]:
        return [app for app in self.prompt_state() if app.get("open")]

    def current_plugin(self):
        """当前打开软件的插件实例；没有打开软件时返回 None。"""
        app_id = self.current_app
        if not app_id:
            return None
        try:
            from plugins import registry
            return registry.get_plugin(app_id)
        except Exception:
            return None

    def current_channel(self):
        """当前打开软件的行为通道；没有打开软件/插件无通道时返回 None。

        认知循环每轮都通过它取通道，因此“打开哪个软件，行为就落到哪个
        插件”；语言模型不受影响。
        """
        plugin = self.current_plugin()
        if plugin is None:
            return None
        try:
            return plugin.channel()
        except Exception:
            return None

    def action_llm_profile(self, fallback=None):
        """当前软件的动作 LLM；没有打开软件时用调用方给的 fallback。"""
        plugin = self.current_plugin()
        if plugin is not None:
            try:
                return plugin.action_llm()
            except Exception:
                pass
        return fallback

    def action_include_reasoning(self, fallback: bool = False) -> bool:
        """当前软件是否要求动作模型接收语言层思考过程。"""
        plugin = self.current_plugin()
        if plugin is not None:
            try:
                return bool(plugin.action_include_reasoning())
            except Exception:
                pass
        return bool(fallback)

    # ---------- 动作执行 ----------
    @staticmethod
    def _plugin_for(app_id: str):
        try:
            from plugins import registry
            if app_id:
                plugin = registry.get_plugin(app_id)
                if plugin is not None:
                    return plugin
                # 容忍模型把软件名当 id 输出
                for candidate in registry.app_plugins():
                    if app_id in (candidate.plugin_id, candidate.label):
                        return candidate
            # 没有有效 id 时优先留在当前软件，其次选第一个已启用软件；
            # 不再使用旧 active 字段，避免 active 与并行软件语义冲突。
            if self.current_app:
                plugin = registry.get_plugin(self.current_app)
                if plugin is not None:
                    return plugin
            for candidate in registry.app_plugins():
                return candidate
        except Exception:
            pass
        return None

    def _remember_result(self, res: dict) -> str:
        if not isinstance(res, dict):
            return ""
        detail = str(res.get("detail") or "").strip()
        content = res.get("content_lines") or []
        if isinstance(content, str):
            content = [content]
        try:
            content = list(content)
        except TypeError:
            content = []
        if detail:
            self.remember(detail)
        for line in content[:12]:
            text = str(line or "").strip()
            if text:
                self.remember(text)
        pieces = [detail] if detail else []
        pieces.extend(str(x) for x in content[:3] if str(x or "").strip())
        return "；".join(p for p in pieces if p) or "没有新内容"

    def _fail(self, message: str) -> str:
        """动作失败也作为“她看到的结果”进入上下文，下一轮可感知。"""
        self.remember(message)
        return message

    async def execute(self, action: str, data: dict) -> str:
        """执行动作层选出的软件动作，并把结果写入软件上下文。"""
        data = data if isinstance(data, dict) else {}
        action = str(action or "").strip().lower()
        try:
            if action == "open_app":
                app_id = str(data.get("app_id") or data.get("id") or "").strip()
                plugin = self._plugin_for(app_id)
                if plugin is None:
                    return self._fail("找不到这个软件")
                res = await plugin.open_app()
                if isinstance(res, dict) and res.get("ok", True):
                    with self._lock:
                        self._current_app = plugin.plugin_id
                    current = str(res.get("current_conversation") or "").strip()
                    if current:
                        with self._lock:
                            self._current_conversation = current
                return self._remember_result(res)

            if action == "close_app":
                app_id = str(data.get("app_id") or data.get("id") or self.current_app).strip()
                plugin = self._plugin_for(app_id)
                if plugin is None:
                    return self._fail("没有打开着的软件")
                res = await plugin.close_app()
                # 退出软件后不再把刚才的对话内容当作“眼前正在发生的事”；
                # 只保留一条“我关掉了软件”的状态。长期记忆不受影响。
                with self._lock:
                    self._entries.clear()
                    self._notifications.clear()
                    self._current_app = ""
                    self._current_conversation = ""
                try:
                    from core.app_router import APP_ROUTER
                    APP_ROUTER.reset()
                except Exception:
                    pass
                return self._remember_result(res)

            if action == "open_conversation":
                plugin = self._plugin_for(self.current_app)
                if plugin is None:
                    return self._fail("还没有打开软件")
                conversation_id = str(
                    data.get("conversation") or data.get("conversation_id")
                    or data.get("id") or ""
                ).strip()
                res = await plugin.open_conversation(conversation_id)
                if isinstance(res, dict) and res.get("ok", True):
                    with self._lock:
                        self._current_conversation = (
                            str(res.get("conversation") or "").strip() or conversation_id
                        )
                return self._remember_result(res)

            if action == "close_conversation":
                plugin = self._plugin_for(self.current_app)
                if plugin is None:
                    return self._fail("还没有打开软件")
                res = await plugin.close_conversation()
                with self._lock:
                    self._current_conversation = ""
                return self._remember_result(res)

            if action == "read_earlier":
                plugin = self._plugin_for(self.current_app)
                if plugin is None:
                    return self._fail("还没有打开软件")
                res = await plugin.read_earlier(self.current_conversation)
                return self._remember_result(res)

            if action == "scroll_to_bottom":
                plugin = self._plugin_for(self.current_app)
                if plugin is None:
                    return self._fail("还没有打开软件")
                res = await plugin.scroll_to_bottom()
                return self._remember_result(res)

            return ""
        except NotImplementedError:
            return self._fail("这个软件还不支持这个动作")
        except Exception as e:
            return self._fail(f"软件操作失败：{e}")


APP_CONTEXT = AppContext()
