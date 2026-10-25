# core/app_router.py
# ========================================================================
# 本地“软件门控”：决定她要不要打开/退出软件。
#
# 这一层不参与语言回复，也不进入行为提示词：
#   - 规则模式：未读持续 N 轮 → 打开；软件/对话空闲 N 轮 → 退出。
#   - 本地 LLM 模式：用 Ollama 的本地 chat 模型，只输出 open_app / close_app /
#     close_conversation / none，输入只有她自己的近期想法与软件状态。
#   - off：不自动开关软件。
#
# 软件打开后的内容行为（点开对话、翻读、发送）仍由插件动作 LLM 负责；
# 这样“打开软件”不依赖插件模型，“全程的语言能力”也保持不变。
# ========================================================================

import json
import logging
import re
import threading
from typing import List, Optional

from config.api_config import config

_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_str(key: str, default: str) -> str:
    value = config.get(key)
    return str(value if value is not None else default).strip()


def _parse_json(text: str) -> dict:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").removeprefix("json").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(cleaned)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


class LocalAppRouter:
    """本地软件门控。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._unread_rounds = {}
        self._idle_rounds = 0
        self._last_content_ts = 0.0
        self._last_signature = None
        self._llm_warned = False
        self._llm_broken = False

    def reset(self):
        with self._lock:
            self._unread_rounds.clear()
            self._idle_rounds = 0
            self._last_content_ts = 0.0
            self._last_signature = None
            self._llm_broken = False

    def note_activity(self):
        """她刚发过消息/做过动作：重置空闲计时，避免被误判为闲置退出。"""
        with self._lock:
            self._idle_rounds = 0

    @staticmethod
    def _signature(statuses: List[dict]) -> tuple:
        return tuple(
            (
                str(app.get("id") or ""),
                bool(app.get("open")),
                int(app.get("unread") or 0),
                str(app.get("current_conversation") or ""),
                tuple((str(c.get("id") or ""), int(c.get("unread") or 0)) for c in (app.get("conversations") or [])),
            )
            for app in statuses
        )

    def _advance_counters(self, statuses: List[dict], content_ts: float):
        signature = self._signature(statuses)
        changed = signature != self._last_signature
        content_changed = content_ts and content_ts != self._last_content_ts
        with self._lock:
            if changed or content_changed:
                self._idle_rounds = 0
            else:
                self._idle_rounds += 1
            self._last_signature = signature
            self._last_content_ts = content_ts

            active_unread = set()
            for app in statuses:
                app_id = str(app.get("id") or "")
                if app.get("unread"):
                    active_unread.add(app_id)
                    self._unread_rounds[app_id] = int(self._unread_rounds.get(app_id, 0)) + 1
                else:
                    self._unread_rounds[app_id] = 0
            for app_id in list(self._unread_rounds):
                if app_id not in active_unread:
                    self._unread_rounds[app_id] = 0
            return self._idle_rounds

    def _rule_decide(self, statuses: List[dict]) -> Optional[dict]:
        open_apps = [app for app in statuses if app.get("open")]
        if not open_apps:
            threshold = max(1, _cfg_int("app_open_after_unread_rounds", 2))
            candidates = [
                app for app in statuses
                if int(app.get("unread") or 0) > 0
                and int(self._unread_rounds.get(str(app.get("id") or ""), 0)) >= threshold
            ]
            if not candidates:
                return None
            target = max(candidates, key=lambda a: int(a.get("unread") or 0))
            return {"action": "open_app", "app_id": str(target.get("id") or "")}

        app = open_apps[0]
        if not app.get("current_conversation"):
            if self._idle_rounds >= max(1, _cfg_int("app_close_idle_rounds", 30)):
                return {"action": "close_app", "app_id": str(app.get("id") or "")}
            return None

        close_conv_at = max(1, _cfg_int("app_close_conversation_idle_rounds", 15))
        close_app_at = max(close_conv_at + 1, _cfg_int("app_close_idle_rounds", 30))
        if self._idle_rounds >= close_app_at:
            return {"action": "close_app", "app_id": str(app.get("id") or "")}
        if self._idle_rounds >= close_conv_at:
            return {"action": "close_conversation", "app_id": str(app.get("id") or "")}
        return None

    @staticmethod
    def _apps_text(statuses: List[dict]) -> str:
        lines = []
        for app in statuses:
            state = "已打开" if app.get("open") else "未打开"
            unread = int(app.get("unread") or 0)
            tail = f"，{unread} 条未读" if unread else ""
            current = app.get("current_conversation")
            if current:
                tail += "，正在看一个对话"
            lines.append(f"- {app.get('label') or app.get('id')}（{app.get('id')}）：{state}{tail}")
        return "\n".join(lines)

    def _llm_decide(self, statuses: List[dict], own_thoughts: List[str]) -> Optional[dict]:
        mode = _cfg_str("app_router_mode", "rule").lower()
        if mode != "local_llm":
            return None
        if self._llm_broken:
            return None
        model = _cfg_str("app_router_model", "")
        if not model:
            if not self._llm_warned:
                logging.warning("[AppRouter] local_llm 模式未配置 app_router_model，改用规则模式")
                self._llm_warned = True
            self._llm_broken = True
            return None

        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=_cfg_str("app_router_api_key", "ollama") or "ollama",
                base_url=_cfg_str("app_router_base_url", "http://127.0.0.1:11434/v1"),
            )
            thoughts = "\n".join(f"- {t}" for t in (own_thoughts or [])[-6:]) or "（此刻没有新的想法）"
            system = (
                "你在决定她要不要打开或退出软件。只输出一个 JSON，不要解释。\n"
                "可选动作：\n"
                '{"action":"open_app","id":"软件id"}\n'
                '{"action":"close_app"}\n'
                '{"action":"close_conversation"}\n'
                '{"action":"none"}\n'
                "规则：只有未读且想查看时才打开；打开软件不等于读消息；"
                "对话内的点开/翻页由插件动作层决定。"
            )
            user = (
                f"【她最近的想法】\n{thoughts}\n\n"
                f"【软件】\n{self._apps_text(statuses)}\n\n"
                f"【空闲轮数】{self._idle_rounds}\n"
                "请输出此刻的动作 JSON。"
            )
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max(32, _cfg_int("app_router_max_tokens", 160)),
                temperature=0.1,
                stream=False,
                timeout=max(1.0, _cfg_float("app_router_timeout", 20.0)),
            )
            content = response.choices[0].message.content or ""
            data = _parse_json(content)
            return self._validate(data, statuses)
        except Exception as e:
            self._llm_broken = True
            if not self._llm_warned:
                logging.warning("[AppRouter] 本地软件门控调用失败，改用规则模式: %s", e)
                self._llm_warned = True
            return None

    @staticmethod
    def _validate(data: dict, statuses: List[dict]) -> Optional[dict]:
        action = str(data.get("action") or "").strip().lower()
        if action == "none":
            return {"action": "none"}
        if action not in ("open_app", "close_app", "close_conversation"):
            return None
        app_ids = {str(app.get("id") or "") for app in statuses}
        app_id = str(data.get("id") or data.get("app_id") or "").strip()
        if action == "open_app":
            if app_id not in app_ids:
                return None
            return {"action": "open_app", "app_id": app_id}
        open_ids = {str(app.get("id") or "") for app in statuses if app.get("open")}
        if app_id and app_id not in open_ids:
            return None
        if not app_id and open_ids:
            app_id = sorted(open_ids)[0]
        return {"action": action, "app_id": app_id}

    def tick(self, statuses: List[dict], own_thoughts: List[str],
             content_ts: float = 0.0) -> Optional[dict]:
        """推进一轮软件门控；返回动作或 None。"""
        mode = _cfg_str("app_router_mode", "rule").lower()
        if mode == "off":
            return None
        with self._lock:
            self._advance_counters(statuses, content_ts)
        decision = self._llm_decide(statuses, own_thoughts)
        if decision is None:
            # 本地模型不可用/输出无效时，规则兜底
            decision = self._rule_decide(statuses)
        if decision and decision.get("action") == "none":
            decision = None
        if decision:
            with self._lock:
                self._idle_rounds = 0
                if decision.get("action") == "close_app":
                    # 退出后重新等待未读累积，避免下一轮立刻又打开
                    self._unread_rounds.clear()
            logging.info("[AppRouter] %s", decision)
        return decision


APP_ROUTER = LocalAppRouter()
