# plugins/web/panel.py
# 网页/小程序插件的面板说明（接口尚未实现）。
from plugins.panel import PanelProvider


class WebPanelProvider(PanelProvider):
    hint = "网页/小程序读取接口尚未实现；read_page / read_mini_program / act_on_page 已预留。"

    def schema(self, plugin) -> dict:
        return {"status": [], "fields": [], "hint": self.hint, "save_label": ""}

    def values(self, plugin) -> dict:
        return {}


PANEL = WebPanelProvider()
