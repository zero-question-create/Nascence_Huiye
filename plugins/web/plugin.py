# plugins/web/plugin.py
# ========================================================================
# 网页 / 小程序插件（接口占位，尚未实现）。
#
# 预留两类能力，二者接口分开，便于后续独立实现：
#   1. 网页：自动读取网页正文、跟随链接；
#   2. 小程序：读取微信/QQ 小程序页面（与网页共用“页面内容”表示）。
#
# 非聊天插件同样可以驱动本地认知循环：把读到的页面内容写成记忆，
# 再由插件配置的 LLM 做行为思考。动作不是只有“说话”，也可以是
# BehaviorAction(kind="navigate"/"read"/"click")——接口已预留。
# ========================================================================

from plugins.base import Capability, Plugin


class WebPlugin(Plugin):
    plugin_id = "web"
    label = "网页 / 小程序"
    description = "预留：自动读取网页或微信/QQ 小程序内容；两种读取接口分开。"
    capabilities = (
        Capability.PAGE_READ,
        Capability.MINI_PROGRAM,
    )
    implemented = False

    async def read_page(self, url: str, *, render: bool = False) -> str:
        """预留：读取网页正文。

        render=True 时允许使用无头浏览器渲染；返回纯文本，调用方负责写记忆。
        """
        raise NotImplementedError("网页插件尚未实现")

    async def read_mini_program(self, platform: str, app_id: str, path: str = "") -> str:
        """预留：读取微信/QQ 小程序页面。

        platform: "wechat" | "qq"；app_id/path 由平台侧提供。
        """
        raise NotImplementedError("网页/小程序插件尚未实现")

    async def act_on_page(self, action: str, **kwargs) -> dict:
        """预留：在页面上执行动作（打开链接、滚动、点击、读取）。

        返回 {"ok": bool, "detail": str}；具体语义由实现方定义。
        """
        raise NotImplementedError("网页插件尚未实现")


PLUGIN = WebPlugin
