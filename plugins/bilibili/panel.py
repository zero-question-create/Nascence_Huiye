# plugins/bilibili/panel.py
# Bilibili 插件的面板说明（凭据与参数在配置页/凭据文件）。
from plugins.panel import PanelProvider


class BilibiliPanelProvider(PanelProvider):
    hint = "Bilibili Cookie 在 config/bilibili_credentials.json；推荐、搜索、评论等参数在“配置”页。"

    def schema(self, plugin) -> dict:
        return {"status": [], "fields": [], "hint": self.hint, "save_label": ""}

    def values(self, plugin) -> dict:
        return {}


PANEL = BilibiliPanelProvider()
