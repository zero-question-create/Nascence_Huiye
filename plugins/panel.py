# plugins/panel.py
# ========================================================================
# 后台面板的插件自述协议。
#
# 插件目录下可以放一个 panel.py，导出模块级 PANEL（PanelProvider 实例），
# registry 会自动发现它，并优先用它来渲染“插件配置”详情页：
#   - schema(plugin)   状态行 + 可编辑字段 + 说明
#   - values(plugin)   当前值（密码类字段返回空串 + _has_<key>=True）
#   - save(plugin, values)  保存并热刷新
#   - running(plugin)  返回 True/False 覆盖“是否运行中”；None 表示用默认
#
# 没有 panel.py 的插件自动回落到 Plugin.panel_schema() 等默认方法，
# 面板不会因为新插件而需要改前端代码。
# ========================================================================

from __future__ import annotations


class PanelProvider:
    """插件面板提供者。所有方法都有默认实现，子类按需覆盖。"""

    hint = ""

    def schema(self, plugin) -> dict:
        return plugin.panel_schema()

    def values(self, plugin) -> dict:
        return plugin.panel_values()

    def save(self, plugin, values: dict) -> dict:
        return plugin.panel_save(values)

    def running(self, plugin):
        """返回 None 表示沿用 plugin.is_running()。"""
        return None

    def refresh(self, plugin) -> None:
        """保存后的热刷新钩子；默认什么都不做。"""

    def run_action(self, plugin, key: str, payload: dict = None) -> dict:
        """执行面板上的自述动作（schema["actions"]）。

        返回 {"ok": bool, "detail": str, "error": str}。
        """
        return {"ok": False, "error": "这个插件没有可执行的面板动作"}


def resolve(plugin_id: str):
    """读取 plugins/<id>/panel.py 的 PANEL；没有则返回 None。"""
    plugin_id = str(plugin_id or "").strip()
    if not plugin_id:
        return None
    try:
        import importlib

        module = importlib.import_module(f"plugins.{plugin_id}.panel")
    except ModuleNotFoundError as exc:
        if exc.name == f"plugins.{plugin_id}.panel":
            return None
        raise
    provider = getattr(module, "PANEL", None)
    if provider is None:
        return None
    if isinstance(provider, type):
        return provider()
    return provider
