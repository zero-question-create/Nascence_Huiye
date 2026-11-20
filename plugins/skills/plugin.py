# plugins/skills/plugin.py
# ========================================================================
# 技能桥接插件：把 core.skill_registry 里“已启用”的技能动作暴露给动作层。
# 禁用技能不会出现在 extra_actions 中，因此不会进入日常认知提示词。
# ========================================================================

from __future__ import annotations

from core import skill_registry
from plugins.base import Plugin


class SkillsPlugin(Plugin):
    plugin_id = "skills"
    label = "技能"
    version = "1.0.0"
    author = "Nascence"
    homepage = "https://github.com/zero-question-create/nhy-plugin-skills"
    capabilities = ()
    implemented = True
    is_app = False
    is_skill = False
    description = "截屏、B站视频分析、读取媒体全文、自建插件等分级技能。"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def is_running(self) -> bool:
        return True

    def available_for_actions(self) -> bool:
        return True

    def extra_actions(self) -> dict:
        return skill_registry.enabled_actions()

    async def perform_action(self, kind: str, data: dict) -> dict:
        return skill_registry.run_action(kind, data)

    def panel_schema(self) -> dict:
        return skill_registry.panel_schema()

    def panel_values(self) -> dict:
        return skill_registry.panel_values()

    def panel_save(self, values: dict) -> dict:
        return skill_registry.panel_save(values)


PLUGIN = SkillsPlugin
