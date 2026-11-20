# plugins/qq_napcat/panel.py
# ========================================================================
# QQ 插件的后台面板自述（registry 自动发现 panel.py 里的 PANEL）。
#
# 这里只做“面板字段的读写与展示”，不碰 NapCat 收发、富消息、未读缓冲等
# 运行逻辑；配置仍然写在 config/api_config.json，保存后走原有的热刷新。
# ========================================================================

from __future__ import annotations

import logging

from plugins.panel import PanelProvider

_CFG_FIELDS = {
    "bot_qq": "bot_qq",
    "active_group_id": "active_group_id",
    "napcat_token": "napcat_token",
    "napcat_http_url": "napcat_http_url",
    "qq_reader_mode": "qq_reader_mode",
}

_SECRET_FIELDS = {"napcat_token"}
_READER_MODES = {"auto", "desktop", "self"}


class QQPanelProvider(PanelProvider):
    hint = "保存后立即生效；字段说明点 ? 查看。"

    def schema(self, plugin) -> dict:
        return {
            "status": [
                {"key": "service", "label": "服务状态"},
                {"key": "napcat", "label": "NapCat 连接"},
                {"key": "identity", "label": "QQ 身份（自动获取）"},
                {"key": "app", "label": "软件状态"},
                {"key": "conversation", "label": "当前对话"},
            ],
            "fields": [
                {
                    "key": "bot_qq",
                    "label": "机器人 QQ",
                    "type": "text",
                    "placeholder": "连接后自动获取；此处仅作未连接时兜底",
                    "help": "真正使用的 QQ 号以 NapCat 登录后自动获取的为准；这里只是未连接时的兜底，可留空。",
                },
                {
                    "key": "active_group_id",
                    "label": "主动发言群号",
                    "type": "text",
                    "required": True,
                    "help": "主动发言和消息路由使用的群号，必须是她能被允许说话的群。",
                },
                {
                    "key": "napcat_token",
                    "label": "NapCat 鉴权 Token",
                    "type": "password",
                    "required": True,
                    "help": "NapCat 的 WebSocket/HTTP 鉴权 Token，要和 NapCat 那边的配置一致。",
                },
                {
                    "key": "napcat_http_url",
                    "label": "NapCat HTTP 接口地址",
                    "type": "text",
                    "placeholder": "http://127.0.0.1:5700",
                    "required": True,
                    "help": "NapCat 的正向 HTTP 服务地址，用来查历史、下载语音等文件；不是 WebSocket 地址。",
                },
                {
                    "key": "qq_reader_mode",
                    "label": "打开对话时谁来读",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "自动（平台自己读，不顶号）"},
                        {"value": "desktop", "label": "用电脑插件读窗口（需窗口已打开）"},
                        {"value": "self", "label": "QQ 自己解析"},
                    ],
                    "help": "auto：默认由平台自己读；desktop：改由电脑插件读已打开的 QQ 窗口（不登录、不顶号，窗口不存在时自动退回）；self：只用 QQ 插件自己的协议解析。",
                },
            ],
            "hint": self.hint,
            "save_label": "保存 QQ 配置",
        }

    def values(self, plugin) -> dict:
        from config.api_config import config

        values = {key: str(config.get(cfg_key) or "") for key, cfg_key in _CFG_FIELDS.items()}
        for key in _SECRET_FIELDS:
            values[key] = ""
            values[f"_has_{key}"] = bool(str(config.get(_CFG_FIELDS[key]) or "").strip())
        values["_status"] = self._live_status()
        return values

    def save(self, plugin, values: dict) -> dict:
        import panel_runtime

        values = values if isinstance(values, dict) else {}
        cfg = panel_runtime.load_panel_config()

        schema = self.schema(plugin)
        missing = []
        for field in schema.get("fields") or []:
            key = str(field.get("key") or "")
            if not field.get("required") or key not in values:
                continue
            if key in _SECRET_FIELDS and not str(values.get(key) or "").strip():
                # 密码留空表示不修改；旧值已存在时不算缺。
                if str(cfg.get(_CFG_FIELDS[key]) or "").strip():
                    continue
            if not str(values.get(key) or "").strip():
                missing.append(str(field.get("label") or key))
        if missing:
            return {"ok": False, "error": "还需要填写：" + "、".join(missing)}

        mode = str(values.get("qq_reader_mode") or "").strip().lower()
        if mode and mode not in _READER_MODES:
            return {"ok": False, "error": "读取方式只能是 auto / desktop / self"}

        for key, cfg_key in _CFG_FIELDS.items():
            if key not in values:
                continue
            value = str(values.get(key) or "").strip()
            if key in _SECRET_FIELDS and not value:
                continue
            if not value:
                continue
            cfg[cfg_key] = value

        panel_runtime.save_panel_config(cfg)
        try:
            panel_runtime.apply_config_hot_reload(cfg)
        except Exception as exc:
            logging.getLogger("Plugins").warning("QQ 面板保存后的热刷新失败: %s", exc)
        return {"ok": True}

    def running(self, plugin):
        try:
            import panel_runtime

            thread = panel_runtime.RUNTIME.qq_thread
            return bool(thread and thread.is_alive())
        except Exception:
            return False

    # ---------- 内部 ----------
    @staticmethod
    def _live_status() -> dict:
        status = {
            "service": "未运行",
            "napcat": "未连接",
            "identity": "未获取",
            "app": "未打开",
            "conversation": "无",
        }
        try:
            import panel_runtime

            thread = panel_runtime.RUNTIME.qq_thread
            running = bool(thread and thread.is_alive())
        except Exception:
            running = False
        status["service"] = "运行中" if running else "未运行"
        try:
            import qq_bot

            connected = qq_bot._napcat_websocket is not None
            status["napcat"] = "已连接" if connected else ("等待 NapCat 连入…" if running else "未连接")
            identity = qq_bot.get_qq_identity() or {}
            if identity.get("nickname"):
                source = "NapCat 自动获取" if identity.get("source") == "napcat" else "兜底"
                status["identity"] = f"{identity['nickname']}（{identity.get('user_id') or '?'}，{source}）"
            elif identity.get("user_id"):
                status["identity"] = f"{identity['user_id']}（未连接，使用配置兜底）"
            app = qq_bot.app_status() or {}
            if app.get("open"):
                unread = int(app.get("unread") or 0)
                status["app"] = "已打开" + (f"，另有 {unread} 条未读" if unread else "")
            else:
                unread = int(app.get("unread") or 0)
                status["app"] = f"未打开{f'，{unread} 条未读' if unread else ''}"
            status["conversation"] = str(app.get("current_conversation") or "无")
        except Exception:
            pass
        return status


PANEL = QQPanelProvider()
