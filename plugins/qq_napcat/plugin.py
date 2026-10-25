# plugins/qq_napcat/plugin.py
# ========================================================================
# QQ（NapCat）插件适配层。
#
# - 读入：runtime 里的 NapCat 反向 WebSocket 服务（原有全部逻辑）；
# - 行为：本插件在 config/plugins.json 中配置的 LLM；
# - 身份：QQ 昵称/QQ 号由 NapCat get_login_info 自动获取，
#   与意识体名字 BOT_NAME（辉夜）严格分开。
# ========================================================================

from plugins.base import BehaviorChannel, Capability, Plugin

from . import runtime


class QQNapCatChannel(BehaviorChannel):
    """把本地认知循环的行为请求翻译成 NapCat 发送。"""

    plugin_id = "qq_napcat"
    source = "QQ"

    def llm_profile(self):
        # 每轮重新取插件实例：面板热保存后无需重启即可换模型
        from plugins.registry import get_plugin
        plugin = get_plugin(self.plugin_id)
        if plugin is None:
            return super().llm_profile()
        return plugin.behavior_llm()

    def action_llm_profile(self):
        """软件内动作模型：优先使用插件的 action_llm，未配置时沿用语言 LLM。"""
        from plugins.registry import get_plugin
        plugin = get_plugin(self.plugin_id)
        if plugin is None:
            return super().action_llm_profile()
        return plugin.action_llm()

    def action_include_reasoning(self):
        from plugins.registry import get_plugin
        plugin = get_plugin(self.plugin_id)
        return bool(plugin and plugin.action_include_reasoning())

    def resolve_target(self, fallback=None):
        # 只有 QQ 软件打开且停在一个对话里时才有发送目标。
        # 未打开软件时，行为想法仍可产生，但不往平台上发。
        if not runtime.is_app_open():
            return None
        return runtime.get_current_conversation() or None

    def outbound_options(self, target):
        return runtime.reply_options(target)

    async def send_text(self, target: str, text: str, **kwargs) -> bool:
        return await runtime.send_group_msg(target, text, **kwargs)

    async def send_media(self, target: str, path: str, as_sticker: bool = False) -> bool:
        return await runtime.send_group_media(target, path, as_sticker=as_sticker)

    def emit_outbound(self, text: str, kind: str = "text") -> None:
        runtime.emit_outbound(text, kind)

    def status(self) -> dict:
        return runtime.service_status()


class QQNapCatPlugin(Plugin):
    plugin_id = "qq_napcat"
    label = "QQ（NapCat）"
    description = "NapCat 反向 WebSocket 接入；QQ 昵称在连接后自动获取。"
    capabilities = (
        Capability.CHAT,
        Capability.GROUP_CHAT,
        Capability.MEDIA,
        Capability.MINI_PROGRAM,
        Capability.PROACTIVE,
    )
    implemented = True
    is_app = True

    def channel(self):
        return QQNapCatChannel()

    async def start(self) -> None:
        # 复用面板已有的线程管理（初始化 Ollama/记忆、启动后台事件循环）
        import panel_runtime
        panel_runtime.RUNTIME.start_qq()

    async def stop(self) -> None:
        import panel_runtime
        panel_runtime.RUNTIME.stop_qq()

    # ---------- 软件（App）接口 ----------
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

    def status(self) -> dict:
        data = super().status()
        data.update(runtime.service_status())
        return data


PLUGIN = QQNapCatPlugin


def get_channel() -> QQNapCatChannel:
    """本地认知循环使用的行为通道。"""
    return QQNapCatChannel()
