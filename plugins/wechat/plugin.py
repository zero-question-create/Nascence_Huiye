# plugins/wechat/plugin.py
# ========================================================================
# 微信插件适配层（参考 QQ 插件的软件模型）。
#
# 微信没有统一 OneBot 协议，所以本插件内置一个“微信桥接 WebSocket 服务端”：
# 使用者选择的微信桥（WeChatFerry / ntchat / 其他本地桥）作为客户端连入，
# 按 plugins/wechat/BRIDGE.md 收发 JSON。
#
# 与 QQ 一致：
#   - 默认关闭，未打开时只累计未读；
#   - 打开软件/对话后内容才进入主进程；
#   - 微信昵称由桥自动上报，后台不配置平台昵称。
# ========================================================================

import asyncio
import threading

from plugins.base import BehaviorChannel, Capability, Plugin
from utils.event_bus import BUS

from . import runtime


class WeChatChannel(BehaviorChannel):
    plugin_id = "wechat"
    source = "WeChat"

    def _plugin(self):
        from plugins.registry import get_plugin
        return get_plugin(self.plugin_id)

    def llm_profile(self):
        plugin = self._plugin()
        return plugin.behavior_llm() if plugin else super().llm_profile()

    def action_llm_profile(self):
        plugin = self._plugin()
        return plugin.action_llm() if plugin else super().action_llm_profile()

    def action_include_reasoning(self):
        plugin = self._plugin()
        return bool(plugin and plugin.action_include_reasoning())

    def resolve_target(self, fallback=None):
        if not runtime.is_app_open():
            return None
        return runtime.get_current_conversation() or None

    def outbound_options(self, target):
        return {}

    async def send_text(self, target: str, text: str, **kwargs) -> bool:
        return await runtime.send_text(target, text)

    async def send_media(self, target: str, path: str, as_sticker: bool = False) -> bool:
        return await runtime.send_media(target, path, as_sticker=as_sticker)

    def emit_outbound(self, text: str, kind: str = "text") -> None:
        BUS.message.emit(runtime.get_self_name() or "微信", text, self.source)

    def status(self) -> dict:
        return runtime.app_status()


class WeChatPlugin(Plugin):
    plugin_id = "wechat"
    label = "微信"
    description = "微信桥接插件：默认关闭，未读缓冲；打开软件/对话后才读取内容。"
    capabilities = (
        Capability.CHAT,
        Capability.DIRECT_CHAT,
        Capability.GROUP_CHAT,
        Capability.MEDIA,
        Capability.MINI_PROGRAM,
    )
    implemented = True
    is_app = True

    def __init__(self, settings: dict = None):
        super().__init__(settings)
        self._thread = None
        self._loop = None
        self._task = None
        self._thread_lock = threading.RLock()

    def channel(self):
        return WeChatChannel()

    # ---------- 软件接口 ----------
    def app_status(self) -> dict:
        return runtime.app_status()

    async def open_app(self) -> dict:
        return await runtime.app_open()

    async def close_app(self) -> dict:
        return await runtime.app_close()

    async def open_conversation(self, conversation_id: str) -> dict:
        return await runtime.app_open_conversation(conversation_id)

    async def close_conversation(self) -> dict:
        return await runtime.app_close_conversation()

    async def read_earlier(self, conversation_id: str = "") -> dict:
        return await runtime.app_read_earlier(conversation_id)

    async def scroll_to_bottom(self) -> dict:
        return await runtime.app_scroll_to_bottom()

    async def read_mini_program(self, app_id: str, path: str = "") -> str:
        """预留：微信小程序读取（也可交给 browser/web 插件统一实现）。"""
        raise NotImplementedError("微信小程序读取尚未实现")

    # ---------- 生命周期 ----------
    def _run_server_thread(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._task = self._loop.create_task(runtime.start_server())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception:
            import logging
            logging.getLogger("WeChat").exception("微信桥服务异常退出")
        finally:
            if self._loop:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()
            self._loop = None
            self._task = None

    async def start(self) -> None:
        # 与 QQ 服务一致：先确保记忆/时钟/Ollama 已初始化，再起桥接服务。
        try:
            import panel_runtime
            panel_runtime.RUNTIME.initialize()
        except Exception:
            pass
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_server_thread, name="WeChatBridge", daemon=True,
            )
            self._thread.start()

    async def stop(self) -> None:
        with self._thread_lock:
            thread = self._thread
            loop = self._loop
            if not thread:
                return
            try:
                if loop:
                    loop.call_soon_threadsafe(runtime.request_shutdown)
                else:
                    thread.join(timeout=2)
            except Exception:
                pass
        thread.join(timeout=15)
        with self._thread_lock:
            self._thread = None

    def status(self) -> dict:
        data = super().status()
        data.update(runtime.app_status())
        data["bridge_connected"] = runtime._napcat_like_ws is not None
        data["identity"] = runtime.get_self_identity()
        return data


PLUGIN = WeChatPlugin


def get_channel() -> WeChatChannel:
    return WeChatChannel()
