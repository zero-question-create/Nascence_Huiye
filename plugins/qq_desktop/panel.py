# plugins/qq_desktop/panel.py
# QQ 桌面读取插件的后台面板。
from __future__ import annotations

from plugins.panel import PanelProvider

_FIELDS = ("qq_desktop_poll_seconds", "qq_desktop_use_vision")


class QQDesktopPanelProvider(PanelProvider):
    hint = "直接读已打开的 QQ 窗口，不需要 NapCat，不会顶号；字段说明点 ? 查看。"

    def schema(self, plugin) -> dict:
        return {
            "status": [
                {"key": "service", "label": "服务状态"},
                {"key": "window", "label": "QQ 窗口"},
                {"key": "unread", "label": "未读总数"},
                {"key": "current", "label": "当前会话"},
            ],
            "fields": [
                {
                    "key": "qq_desktop_poll_seconds",
                    "label": "扫描间隔（秒）",
                    "type": "number",
                    "help": "多久扫一次 QQ 会话列表里的“N条未读”。太短会增加无障碍树读取负担，一般 3~10 秒。",
                },
                {
                    "key": "qq_desktop_use_vision",
                    "label": "读不到时用视觉模型",
                    "type": "bool",
                    "help": "UIA 读不到消息区时，才截图交给视觉模型兜底；关闭后只报“读不到”，不会截图。",
                },
            ],
            "hint": self.hint,
            "save_label": "保存 QQ 桌面读取配置",
        }

    def values(self, plugin) -> dict:
        from config.api_config import config

        values = {key: config.get(key) for key in _FIELDS}
        status = plugin.status()
        values["_status"] = {
            "service": "运行中" if plugin.is_running() else "已停止",
            "window": "已找到" if status.get("window") else "未找到",
            "unread": str(status.get("unread") or 0),
            "current": status.get("current") or "无",
        }
        return values

    def save(self, plugin, values: dict) -> dict:
        import panel_runtime

        values = values if isinstance(values, dict) else {}
        cfg = panel_runtime.load_panel_config()
        if "qq_desktop_poll_seconds" in values:
            try:
                cfg["qq_desktop_poll_seconds"] = max(1, int(float(values["qq_desktop_poll_seconds"])))
            except (TypeError, ValueError):
                return {"ok": False, "error": "扫描间隔需要是数字"}
        if "qq_desktop_use_vision" in values:
            cfg["qq_desktop_use_vision"] = bool(values["qq_desktop_use_vision"])
        panel_runtime.save_panel_config(cfg)
        panel_runtime.apply_config_hot_reload(cfg)
        return {"ok": True}

    def running(self, plugin):
        return bool(plugin.is_running())


PANEL = QQDesktopPanelProvider()
