# plugins/base.py
# ========================================================================
# 插件契约（平台接入层）。
#
# 设计原则：
#   - 本地认知循环（core/cognition.py）只负责记忆检索、扩散与概念路；
#   - 插件负责“从平台读入”“把行为发回平台”，并通过 behavior_llm()
#     指定该插件的行为 LLM；
#   - 行为思考的提示词由 core/behavior.py 统一提供，插件不得改写，
#     也不得向提示词注入平台名、群号、用户 ID 等外部信息。
#
# 一个插件最少需要：
#   1. 继承 Plugin，声明 plugin_id / label / capabilities；
#   2. 在 channel() 返回一个 BehaviorChannel（发送文本/媒体、解析目标）；
#   3. 把平台消息写入记忆与关键词信箱（沿用 core.memory_engine /
#      core.cognition 的公开入口），并启动 cognitive_loop(channel=...)；
#   4. 在 config/plugins.json 中登记（registry 会自动补默认项）。
#
# 微信 / 网页 / Bilibili 等后续插件只需实现同一契约，认知与记忆层无需改动。
# ========================================================================

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from config.constants import BOT_NAME
from core.llm_profile import LLMProfile
from utils.event_bus import BUS


class Capability:
    """插件能力标签。用于面板展示与未来调度，不参与提示词。"""

    CHAT = "chat"                  # 收发文本
    GROUP_CHAT = "group_chat"      # 群聊
    DIRECT_CHAT = "direct_chat"    # 私聊
    MEDIA = "media"                # 图片/表情包/语音/视频
    PAGE_READ = "page_read"        # 读取网页正文
    MINI_PROGRAM = "mini_program"  # 读取微信/QQ 小程序
    ACCOUNT = "account"            # 账号态（登录、主页、动态）
    FEED = "feed"                  # 信息流浏览
    SEARCH = "search"              # 站内搜索
    PROACTIVE = "proactive"        # 支持主动发言


@dataclass
class MediaItem:
    """平台侧的一条媒体（预留：未来插件用它把媒体交给认知层）。"""

    kind: str                      # image | sticker | record | video
    description: str = ""
    path: str = ""
    ref: str = ""


@dataclass
class Perception:
    """平台侧的一条感知事件（预留的标准化输入）。

    QQ 插件目前沿用既有消息处理路径；后续插件可直接构造 Perception，
    再写入记忆/关键词，避免各自重复解析逻辑。
    """

    plugin_id: str
    text: str = ""
    speaker: str = ""
    speaker_id: str = ""
    channel_id: str = ""
    target: str = ""
    reply_to: str = ""
    media: List[MediaItem] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class BehaviorAction:
    """行为层可执行的动作描述（预留：给网页/Bilibili 等非聊天插件用）。"""

    kind: str                      # text | media | navigate | click | read
    text: str = ""
    path: str = ""
    meta: dict = field(default_factory=dict)


def resolve_llm_profile(settings: dict, plugin_id: str = "") -> LLMProfile:
    """从插件配置的 llm 段解析行为 LLM；未填字段回落到 primary。"""
    settings = settings if isinstance(settings, dict) else {}
    return LLMProfile.from_mapping(settings.get("llm"), plugin_id=plugin_id)


class BehaviorChannel(ABC):
    """插件提供给本地认知循环的行为通道。

    认知循环只调用这些方法，不关心底层是 QQ、微信还是网页。
    """

    plugin_id: str = "core"
    source: str = "Core"

    def llm_profile(self) -> LLMProfile:
        """语言/行为思考使用的 LLM。"""
        return LLMProfile(plugin_id=self.plugin_id)

    def action_llm_profile(self) -> LLMProfile:
        """软件内动作使用的 LLM；默认沿用语言 LLM。"""
        return self.llm_profile()

    def action_include_reasoning(self) -> bool:
        """动作模型是否接收语言层的思考过程。"""
        return False

    def resolve_target(self, fallback: Optional[str] = None) -> Optional[str]:
        """本轮行为的落点（群号/会话 ID/页面 ID）。"""
        return fallback

    def outbound_options(self, target: Optional[str]) -> dict:
        """本轮发送的附加参数（引用、@ 等）；默认无。"""
        return {}

    @abstractmethod
    async def send_text(self, target: str, text: str, **kwargs) -> bool:
        """发送一条文本，返回是否确认送达。"""
        raise NotImplementedError

    async def send_media(self, target: str, path: str, as_sticker: bool = False) -> bool:
        """发送一张图片/表情包，返回是否确认送达。"""
        raise NotImplementedError

    def emit_outbound(self, text: str, kind: str = "text") -> None:
        """向事件总线广播“她说出口了”，供面板显示。"""
        BUS.message.emit(BOT_NAME, text, self.source or "Core")

    def status(self) -> dict:
        return {"plugin_id": self.plugin_id, "source": self.source}


class Plugin(ABC):
    """一个平台接入插件。"""

    plugin_id: str = ""
    label: str = ""
    capabilities: tuple = ()
    implemented: bool = True
    is_app: bool = False          # 是否是“她可以打开/关闭的软件”
    description: str = ""

    def __init__(self, settings: dict = None):
        self.settings = dict(settings or {})

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    def behavior_llm(self) -> LLMProfile:
        """语言/行为思考 LLM；空配置自动回落到 primary。"""
        return resolve_llm_profile(self.settings, plugin_id=self.plugin_id)

    def action_llm(self) -> LLMProfile:
        """软件内动作 LLM；插件配置了 action_llm 时使用它，否则沿用语言 LLM。"""
        raw = self.settings.get("action_llm")
        if isinstance(raw, dict) and any(
            str(raw.get(k) or "").strip() for k in ("api_key", "base_url", "model")
        ):
            return LLMProfile.from_mapping(raw, plugin_id=self.plugin_id)
        return self.behavior_llm()

    def action_include_reasoning(self) -> bool:
        """是否把语言层的思考过程交给动作模型（插件设置）。"""
        return bool(self.settings.get("action_include_reasoning", False))

    def channel(self) -> Optional[BehaviorChannel]:
        """返回行为通道；未实现的插件返回 None。"""
        return None

    # ---------- 软件（App）接口 ----------
    # 插件不只是一个发送通道，也是她可以打开/关闭的“软件”。
    # 未打开时插件只累积未读数；打开后才把内容交回主进程。
    def app_status(self) -> dict:
        """软件当前状态：open / unread / current_conversation / conversations。"""
        return {
            "id": self.plugin_id,
            "label": self.label or self.plugin_id,
            "open": False,
            "unread": 0,
            "current_conversation": "",
            "conversations": [],
        }

    async def open_app(self) -> dict:
        """打开软件，返回 {"ok": bool, "detail": str, "content_lines": [...]}。"""
        if not self.implemented:
            raise NotImplementedError(f"软件 {self.plugin_id} 尚未实现")
        return {"ok": False, "detail": f"{self.label or self.plugin_id}还不能打开"}

    async def close_app(self) -> dict:
        if not self.implemented:
            raise NotImplementedError(f"软件 {self.plugin_id} 尚未实现")
        return {"ok": True, "detail": f"关掉了{self.label or self.plugin_id}"}

    async def open_conversation(self, conversation_id: str) -> dict:
        """在已打开软件里点开某个对话，把最近内容交回主进程。"""
        if not self.implemented:
            raise NotImplementedError(f"软件 {self.plugin_id} 尚未实现")
        return {"ok": False, "detail": "这个软件没有可打开的对话"}

    async def close_conversation(self) -> dict:
        """退出当前对话，回到软件首页。"""
        if not self.implemented:
            raise NotImplementedError(f"软件 {self.plugin_id} 尚未实现")
        return {"ok": True, "detail": "退出了对话"}

    async def read_earlier(self, conversation_id: str = "") -> dict:
        """在当前对话里尝试翻看更早的内容。"""
        if not self.implemented:
            raise NotImplementedError(f"软件 {self.plugin_id} 尚未实现")
        return {"ok": False, "detail": "已经是最早的内容了"}

    async def scroll_to_bottom(self) -> dict:
        """回到当前对话/页面的最新消息底部。"""
        if not self.implemented:
            raise NotImplementedError(f"软件 {self.plugin_id} 尚未实现")
        return {"ok": False, "detail": "这个软件没有可回到底部的内容"}

    async def start(self) -> None:
        if not self.implemented:
            raise NotImplementedError(f"插件 {self.plugin_id} 尚未实现")
        raise NotImplementedError(f"插件 {self.plugin_id} 未提供 start()")

    async def stop(self) -> None:
        return None

    def status(self) -> dict:
        data = {
            "id": self.plugin_id,
            "label": self.label or self.plugin_id,
            "capabilities": list(self.capabilities),
            "implemented": bool(self.implemented),
            "enabled": self.enabled,
            "is_app": bool(self.is_app),
            "action_include_reasoning": self.action_include_reasoning(),
            "description": self.description,
        }
        channel = self.channel()
        if channel is not None:
            try:
                data["channel"] = channel.status()
            except Exception as exc:
                data["channel"] = {"error": str(exc)}
        try:
            data["app"] = self.app_status()
        except Exception as exc:
            data["app"] = {"error": str(exc)}
        return data
