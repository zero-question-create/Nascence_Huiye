# core/llm_profile.py
# ========================================================================
# 行为 LLM 的“连接配置”描述。
#
# 职责边界：
#   - 本地认知循环（记忆检索、扩散、概念路）不依赖任何 LLM；
#   - 每轮的行为思考由当前插件指定的 LLM 完成；
#   - 插件未填写的字段回落到 config/api_config.json 的 primary_*，
#     因此旧部署即使不配置插件 LLM 也保持原有行为。
#
# 这里只描述“用哪个模型、怎么连”，不包含任何提示词内容。
# ========================================================================

from dataclasses import dataclass
from typing import Any, Optional

from config.api_config import config


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class LLMProfile:
    """一次行为思考所使用的模型连接信息。字段为 None 表示走全局默认。"""

    plugin_id: str = ""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout: Optional[float] = None

    @classmethod
    def from_mapping(cls, data: Any, plugin_id: str = "") -> "LLMProfile":
        """从插件配置的 llm 字典构造；空字符串一律视为未设置。"""
        if not isinstance(data, dict):
            data = {}

        temperature = data.get("temperature")
        try:
            temperature = float(temperature) if temperature not in (None, "") else None
        except (TypeError, ValueError):
            temperature = None

        max_tokens = data.get("max_tokens")
        try:
            max_tokens = int(max_tokens) if max_tokens not in (None, "") else None
        except (TypeError, ValueError):
            max_tokens = None

        timeout = data.get("timeout")
        try:
            timeout = float(timeout) if timeout not in (None, "") else None
        except (TypeError, ValueError):
            timeout = None

        return cls(
            plugin_id=plugin_id,
            api_key=_clean_str(data.get("api_key")),
            base_url=_clean_str(data.get("base_url")),
            model=_clean_str(data.get("model")),
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def resolved(self) -> "LLMProfile":
        """把未设置字段补齐为当前全局 primary 配置（支持热更新）。"""
        return LLMProfile(
            plugin_id=self.plugin_id,
            api_key=self.api_key or _clean_str(config.get("primary_api_key")),
            base_url=self.base_url or _clean_str(config.get("primary_base_url")),
            model=self.model or _clean_str(config.get("primary_model")),
            temperature=self.temperature if self.temperature is not None else 0.1,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
        )

    @property
    def uses_primary(self) -> bool:
        """是否为全局默认模型（用于复用已有客户端）。"""
        return not self.api_key and not self.base_url and not self.model


def global_behavior_profile(plugin_id: str = "") -> LLMProfile:
    """全局行为模型。

    语言模型必须全局一致（始终 primary）；行为模型只负责软件内动作，
    可以由插件选择跟随这里的全局配置，或使用插件自己的 action_llm。
    未配置 behavior_* 时逐字段回落到 primary。
    """
    return LLMProfile.from_mapping(
        {
            "api_key": config.get("behavior_api_key", ""),
            "base_url": config.get("behavior_base_url", ""),
            "model": config.get("behavior_model", ""),
            "temperature": config.get("behavior_temperature"),
            "max_tokens": config.get("behavior_max_tokens"),
            "timeout": config.get("behavior_timeout"),
        },
        plugin_id=plugin_id,
    )
