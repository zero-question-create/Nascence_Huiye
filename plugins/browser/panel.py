# plugins/browser/panel.py
# 浏览器插件的面板说明（解析参数在“配置”页的浏览器插件区域）。
from plugins.panel import PanelProvider


class BrowserPanelProvider(PanelProvider):
    hint = "网页解析模式（static/dynamic）、DeepSeek Fetch 地址与摘要模型在“配置”页的浏览器插件区域。"

    def schema(self, plugin) -> dict:
        return {"status": [], "fields": [], "hint": self.hint, "save_label": ""}

    def values(self, plugin) -> dict:
        return {}


PANEL = BrowserPanelProvider()
