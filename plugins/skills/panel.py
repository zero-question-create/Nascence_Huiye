# plugins/skills/panel.py
from plugins.panel import PanelProvider
from core import skill_registry


class SkillsPanel(PanelProvider):
    def schema(self, plugin):
        return skill_registry.panel_schema()

    def values(self, plugin):
        return skill_registry.panel_values()

    def save(self, plugin, values):
        return skill_registry.panel_save(values)

    def running(self, plugin):
        return True


PANEL = SkillsPanel()
