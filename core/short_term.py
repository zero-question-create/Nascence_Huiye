# core/short_term.py
# ========================================================================
# 本地短期记忆：她自己的近期想法（4~6 条）。
#
# 与“软件内容”分开：
#   - 这里只放她自己每轮产生的想法（我说/我想），不乱塞别人的消息；
#   - 软件未读提醒、打开的对话内容等由 core/apps.py 的 AppContext 承载，
#     通过插件传入主进程，再一起作为 pinned 记忆进入行为思考。
#
# 窗口长度默认由 config.short_term_thought_limit 控制（夹在 4~12）；
# 插件/面板可通过 set_limit() / set_limit_provider() 注入（例如电脑插件用 10 条）。
# ========================================================================

import threading
from collections import deque
from typing import List, Tuple

from config.api_config import config
from core.virtual_clock import clock
from utils.time_phrases import get_relative_time_phrase

SHORT_TERM_MIN = 4
SHORT_TERM_MAX = 12
DEFAULT_LIMIT = 5

# 新增接口：允许插件/面板注入窗口长度（例如电脑插件需要 9~11 条）。
# 没有注入时行为与原来完全一致，仍然读 config.short_term_thought_limit。
_limit_provider = None


def set_limit_provider(provider):
    """注入窗口长度提供者：callable() -> int|None。传 None 恢复配置值。"""
    global _limit_provider
    _limit_provider = provider if callable(provider) else None


def set_limit(limit):
    """便捷接口：固定窗口长度；传 None 恢复配置值。"""
    if limit is None:
        set_limit_provider(None)
        return
    value = int(limit)
    set_limit_provider(lambda: value)


def current_limit() -> int:
    """当前生效的窗口长度（配置或注入值，已夹取）。"""
    return _limit_from_config()


def _limit_from_config() -> int:
    if _limit_provider is not None:
        try:
            injected = _limit_provider()
        except Exception:
            injected = None
        if injected is not None:
            try:
                return max(SHORT_TERM_MIN, min(SHORT_TERM_MAX, int(injected)))
            except (TypeError, ValueError):
                pass
    try:
        value = int(config.get("short_term_thought_limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(SHORT_TERM_MIN, min(SHORT_TERM_MAX, value))


class ShortTermMemory:
    """她自己最近的想法，按时间顺序保留。"""

    def __init__(self, limit: int = None):
        self._lock = threading.RLock()
        self._limit = self._clamp(limit) if limit is not None else _limit_from_config()
        self._items: deque = deque(maxlen=self._limit)

    @staticmethod
    def _clamp(limit: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            value = DEFAULT_LIMIT
        return max(SHORT_TERM_MIN, min(SHORT_TERM_MAX, value))

    @property
    def limit(self) -> int:
        return self._limit

    def reset_limit(self):
        """配置热更新后重新读取窗口长度。"""
        with self._lock:
            new_limit = _limit_from_config()
            if new_limit == self._limit:
                return
            items = list(self._items)[-new_limit:]
            self._limit = new_limit
            self._items = deque(items, maxlen=new_limit)

    def add_thought(self, text: str, said: bool, ts: float = None):
        """记录一条自己的近期想法。said=True 表示说出口，False 表示只在心里想。"""
        text = str(text or "").strip()
        if not text:
            return
        virtual_ts = clock.now() if ts is None else ts
        real_ts = clock.to_real_time(virtual_ts)
        phrase = get_relative_time_phrase(real_ts)
        prefix = "我说" if said else "我想"
        line = f"[{phrase}] {prefix}：{text}"
        with self._lock:
            # 近重复念头不再反复占窗口；否则模型会把同一句当作“最近想法”回灌。
            if self._items:
                try:
                    from core.repetition import similarity
                    last_ts, last_line = self._items[-1]
                    if real_ts - float(last_ts) < 180 and similarity(line, last_line) >= 0.90:
                        return
                except Exception:
                    pass
            self._items.append((real_ts, line))

    def recent(self) -> List[Tuple[float, str]]:
        """返回 (真实时间戳, 带时间标签的想法)，按时间升序。"""
        with self._lock:
            return list(self._items)

    def texts(self) -> List[str]:
        return [text for _, text in self.recent()]

    def timestamps(self) -> List[float]:
        return [ts for ts, _ in self.recent()]

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self):
        with self._lock:
            return len(self._items)


SHORT_TERM = ShortTermMemory()
