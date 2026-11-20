# plugins/desktop/panel.py
# ========================================================================
# 电脑插件的后台面板自述：状态行 + 可编辑字段，全部由 registry 自动发现。
# 配置写入 config/api_config.json 的 desktop_* 字段。
# ========================================================================

from __future__ import annotations

from plugins.panel import PanelProvider

_FIELDS = (
    "desktop_use_real_desktop",
    "desktop_max_steps",
    "desktop_task_timeout",
    "desktop_thought_window",
    "desktop_allow_screenshot",
    "desktop_observe_software",
    "desktop_launch_mode",
    "desktop_window_mode",
    "desktop_reuse_mode",
    "desktop_allow_coordinate_input",
    "desktop_allow_synthetic_keys",
    "desktop_allow_shell_windows",
    "desktop_coordinate_fence",
    "desktop_wallpaper",
    "desktop_notes_dirs",
)


class DesktopPanelProvider(PanelProvider):
    hint = "辉夜看无障碍树、按引用操作；默认不截图。设置项点 ? 看说明。"

    def schema(self, plugin) -> dict:
        return {
            "status": [
                {"key": "service", "label": "服务状态"},
                {"key": "workspace", "label": "工作桌面"},
                {"key": "task", "label": "当前任务"},
                {"key": "window", "label": "思考窗口"},
            ],
            "fields": [
                {
                    "key": "desktop_use_real_desktop",
                    "label": "直接使用真实桌面",
                    "type": "bool",
                    "help": "打开后不再启动虚拟工作桌面，辉夜直接操作真实 Windows 窗口和桌面。更稳定，但任务会动到你正在用的窗口；建议只在明确需要时开启。",
                },
                {
                    "key": "desktop_max_steps",
                    "label": "单任务最多步数",
                    "type": "number",
                    "required": True,
                    "help": "一个电脑任务最多让操作手走几步。步数越多越慢，也越容易反复试探；一般 8~16 步够用。",
                },
                {
                    "key": "desktop_task_timeout",
                    "label": "单任务超时（秒）",
                    "type": "number",
                    "help": "超过这个时长就收手，避免一个任务卡住整个认知循环。",
                },
                {
                    "key": "desktop_thought_window",
                    "label": "思考窗口（9~11 条）",
                    "type": "number",
                    "help": "电脑插件运行期间，把她的短期想法窗口临时扩到 9~11 条；停止后恢复全局设置。",
                },
                {
                    "key": "desktop_allow_screenshot",
                    "label": "允许显式截图（默认关闭）",
                    "type": "bool",
                    "help": "只有画布、游戏、自绘界面这类无障碍树读不到内容时，操作手才可以主动截图；日常操作不会截屏。",
                },
                {
                    "key": "desktop_observe_software",
                    "label": "软件打开后由电脑读窗口（兼容 QQ/微信）",
                    "type": "bool",
                    "help": "平台软件接到未读后，打开窗口时由电脑插件读内容：先读无障碍树，读不到就加深树重试，再退回视觉模型，最后才把窗口移进工作桌面。",
                },
                {
                    "key": "desktop_launch_mode",
                    "label": "打开软件的方式",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "自动：已打开就复用+镜像，否则新开"},
                        {"value": "new", "label": "总是新开进程（失败就镜像）"},
                        {"value": "reuse", "label": "只复用现有窗口并镜像"},
                    ],
                    "help": "auto 适合 QQ/微信这类已有实例：已经打开就复制现有窗口进来，不会把用户的原窗口拖走；没打开才启动新进程。new 会先启动程序；如果它只能复用旧窗口或无法嵌入，就退回复制。reuse 只复制现有窗口，不会新开进程。单个程序可在 config/desktop_apps.json 的条目里用 launch_mode 覆盖。",
                },
                {
                    "key": "desktop_window_mode",
                    "label": "窗口放入方式",
                    "type": "select",
                    "options": [
                        {"value": "float", "label": "浮在工作区内（可键鼠输入）"},
                        {"value": "embed", "label": "真嵌入为子窗口（可能无法键入）"},
                    ],
                    "help": "float：把真实窗口放在工作区坐标里，宿主鼠标/键盘可以直接操作，推荐。embed：用 SetParent 变成工作桌面的子窗口，视觉上真正嵌进去、随桌面移动，但跨进程子窗口通常收不到宿主的键盘输入，记事本等可能无法键入。单个程序可用 window_mode 覆盖。",
                },
                {
                    "key": "desktop_reuse_mode",
                    "label": "复用已有窗口时",
                    "type": "select",
                    "options": [
                        {"value": "mirror", "label": "只复制画面（不抢原窗口）"},
                        {"value": "float", "label": "把真实窗口移进工作区（可交互）"},
                    ],
                    "help": "mirror：已打开的窗口只做镜像，画面可见但不能用宿主键鼠操作，也不会移动原窗口。float：把已打开的真实窗口移进工作区，宿主可直接点击/输入；原窗口会被搬走。单个程序可在 desktop_apps.json 用 reuse_mode 覆盖。",
                },
                {
                    "key": "desktop_allow_coordinate_input",
                    "label": "允许坐标鼠标/键入",
                    "type": "bool",
                    "help": "允许在屏幕坐标上点击、拖拽、滚轮，以及在指定位置键入（Unicode，中文/emoji 都行）。只在按引用操作失败时兜底；关闭后只能按控件引用操作。",
                },
                {
                    "key": "desktop_allow_synthetic_keys",
                    "label": "允许合成按键/键入",
                    "type": "bool",
                    "help": "允许发送 Enter、快捷键，以及把文字送进当前焦点（剪贴板 + Ctrl+V）。关闭后仍可用 set_value 直接写输入框。",
                },
                {
                    "key": "desktop_allow_shell_windows",
                    "label": "放行 shell 窗口（桌面/任务栏）",
                    "type": "bool",
                    "help": "打开后不再把 Progman、WorkerW、任务栏等 shell 窗口当黑名单，允许像普通窗口一样操作。默认开启；如果不想让工作桌面搬动系统桌面，可以关掉。",
                },
                {
                    "key": "desktop_coordinate_fence",
                    "label": "坐标围栏",
                    "type": "bool",
                    "help": "坐标必须落在目标窗口或工作桌面的矩形内，防止误点其他程序。关掉后不再限制，不推荐。",
                },
                {
                    "key": "desktop_wallpaper",
                    "label": "壁纸图片（留空用系统壁纸）",
                    "type": "text",
                    "placeholder": "例如 C:\\Users\\...\\wallpaper.jpg",
                    "help": "工作桌面自己的壁纸。留空时跟随当前 Windows 系统壁纸；换图后点“重载壁纸”立即生效。",
                },
                {
                    "key": "desktop_notes_dirs",
                    "label": "桌面笔记目录（可留空）",
                    "type": "text",
                    "placeholder": "留空=项目所在桌面、用户桌面/文档/下载",
                    "help": "扫描这些目录里的 .txt 文件，把文件名和图标显示在虚拟桌面左侧，双击/点击可在工作区里用记事本打开。多个目录用逗号或分号分隔。",
                },
            ],
            "hint": self.hint,
            "actions": [
                {"key": "self_test", "label": "自测：打开记事本写一行"},
                {"key": "show_workspace", "label": "显示工作桌面"},
                {"key": "hide_workspace", "label": "收起工作桌面"},
                {"key": "reload_wallpaper", "label": "重载壁纸"},
            ],
            "save_label": "保存电脑配置",
        }

    def values(self, plugin) -> dict:
        from config.api_config import config

        values = {key: config.get(key) for key in _FIELDS}
        status = plugin.app_status()
        workspace = status.get("workspace") or {}
        if workspace.get("real"):
            workspace_text = "真实桌面模式（不使用虚拟工作桌面）"
        elif workspace.get("running"):
            workspace_text = "运行中"
            if workspace.get("visible"):
                workspace_text += "（可见）"
            workspace_text += f"，已放入 {int(workspace.get('windows') or 0)} 个窗口"
        else:
            workspace_text = "未启动"
        task = ""
        if status.get("busy"):
            task = f"正在操作：{status.get('task') or ''}（第 {status.get('step') or 0} 步）"
        values["_status"] = {
            "service": "运行中" if plugin.is_running() else "已停止",
            "workspace": workspace_text,
            "task": task or "空闲",
            "window": f"{self._window(plugin)} 条",
        }
        return values

    @staticmethod
    def _window(plugin) -> int:
        try:
            return int(plugin._thought_window())
        except Exception:
            return 10

    def save(self, plugin, values: dict) -> dict:
        import panel_runtime

        values = values if isinstance(values, dict) else {}
        cfg = panel_runtime.load_panel_config()

        for key in ("desktop_max_steps", "desktop_task_timeout", "desktop_thought_window"):
            if key in values and str(values.get(key) or "").strip() != "":
                try:
                    cfg[key] = int(float(values[key]))
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{key} 需要是数字"}

        window = int(cfg.get("desktop_thought_window") or 10)
        cfg["desktop_thought_window"] = max(9, min(11, window))

        if "desktop_launch_mode" in values:
            mode = str(values.get("desktop_launch_mode") or "").strip().lower()
            if mode not in ("auto", "new", "reuse"):
                return {"ok": False, "error": "打开方式只能是 auto / new / reuse"}
            cfg["desktop_launch_mode"] = mode

        if "desktop_window_mode" in values:
            mode = str(values.get("desktop_window_mode") or "").strip().lower()
            if mode not in ("float", "embed"):
                return {"ok": False, "error": "窗口放入方式只能是 float / embed"}
            cfg["desktop_window_mode"] = mode

        if "desktop_reuse_mode" in values:
            mode = str(values.get("desktop_reuse_mode") or "").strip().lower()
            if mode not in ("mirror", "float"):
                return {"ok": False, "error": "复用方式只能是 mirror / float"}
            cfg["desktop_reuse_mode"] = mode

        if "desktop_allow_screenshot" in values:
            cfg["desktop_allow_screenshot"] = bool(values.get("desktop_allow_screenshot"))
        if "desktop_use_real_desktop" in values:
            cfg["desktop_use_real_desktop"] = bool(values.get("desktop_use_real_desktop"))
        if "desktop_observe_software" in values:
            cfg["desktop_observe_software"] = bool(values.get("desktop_observe_software"))
        if "desktop_allow_coordinate_input" in values:
            cfg["desktop_allow_coordinate_input"] = bool(values.get("desktop_allow_coordinate_input"))
        if "desktop_allow_synthetic_keys" in values:
            cfg["desktop_allow_synthetic_keys"] = bool(values.get("desktop_allow_synthetic_keys"))
        if "desktop_allow_shell_windows" in values:
            cfg["desktop_allow_shell_windows"] = bool(values.get("desktop_allow_shell_windows"))
        if "desktop_coordinate_fence" in values:
            cfg["desktop_coordinate_fence"] = bool(values.get("desktop_coordinate_fence"))
        if "desktop_wallpaper" in values:
            cfg["desktop_wallpaper"] = str(values.get("desktop_wallpaper") or "").strip()
        if "desktop_notes_dirs" in values:
            cfg["desktop_notes_dirs"] = str(values.get("desktop_notes_dirs") or "").strip()

        panel_runtime.save_panel_config(cfg)
        panel_runtime.apply_config_hot_reload(cfg)
        return {"ok": True}

    def running(self, plugin):
        return bool(plugin.is_running())

    def run_action(self, plugin, key: str, payload: dict = None) -> dict:
        key = str(key or "")
        if key == "self_test":
            return plugin.self_test()
        if key == "show_workspace":
            return plugin.show_workspace()
        if key == "hide_workspace":
            return plugin.hide_workspace()
        if key == "reload_wallpaper":
            return plugin.reload_wallpaper()
        return {"ok": False, "error": f"未知动作：{key}"}

    def refresh(self, plugin) -> None:
        try:
            plugin.apply_settings()
        except Exception:
            pass


PANEL = DesktopPanelProvider()
