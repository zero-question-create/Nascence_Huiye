# plugins/wechat/panel.py
# 微信插件的面板说明（没有额外可编辑字段，配置在“配置”页与 manifest）。
from plugins.panel import PanelProvider


class WeChatPanelProvider(PanelProvider):
    hint = (
        "微信桥接地址、Token 与会话白名单在“配置”页和 config/wechat_manifest.json；"
        "桥接协议见 plugins/wechat/BRIDGE.md。"
    )

    def schema(self, plugin) -> dict:
        return {
            "status": [{"key": "running", "label": "桥接状态"}],
            "fields": [],
            "hint": self.hint,
            "save_label": "",
        }

    def values(self, plugin) -> dict:
        return {"_status": {"running": "运行中" if plugin.is_running() else "未运行"}}


PANEL = WeChatPanelProvider()
