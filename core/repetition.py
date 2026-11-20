# core/repetition.py
# ========================================================================
# 重复念头/重复动作的硬保护。
#
# 反刍抑制只处理关键词，仍可能让模型输出同一句话；这里在输出后、
# 写记忆/发送前做一次文本相似度检查，并在动作层做动作签名冷却。
# ========================================================================

from __future__ import annotations

import re
import time
from collections import deque
from difflib import SequenceMatcher
from threading import RLock

_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    content = _PREFIX_RE.sub("", str(text or ""))
    content = content.replace("我想：", "").replace("我说：", "")
    content = _PUNCT_RE.sub("", content)
    return content.strip().lower()


def _grams(text: str, size: int = 2) -> set:
    content = normalize(text)
    if len(content) <= size:
        return {content} if content else set()
    return {content[i:i + size] for i in range(len(content) - size + 1)}


def similarity(left: str, right: str) -> float:
    """2-gram Jaccard 与 SequenceMatcher 取较大值，兼顾换皮和语序变化。"""
    a, b = _grams(left), _grams(right)
    if not a or not b:
        return 1.0 if normalize(left) == normalize(right) else 0.0
    jaccard = len(a & b) / max(1, len(a | b))
    ratio = SequenceMatcher(None, normalize(left), normalize(right)).ratio()
    return max(jaccard, ratio)


class RecentOutputGuard:
    """记住最近生成的想法；同一句话/近重复在窗口内只允许一次。"""

    def __init__(self, window: int = 6, threshold: float = 0.82):
        self.window = max(1, int(window))
        self.threshold = float(threshold)
        self._items = deque(maxlen=self.window)
        self._lock = RLock()

    def is_repeat(self, text: str) -> bool:
        with self._lock:
            return any(similarity(text, item) >= self.threshold for item in self._items)

    def add(self, text: str) -> None:
        if not str(text or "").strip():
            return
        with self._lock:
            self._items.append(str(text))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def texts(self) -> list[str]:
        with self._lock:
            return list(self._items)


class ActionGuard:
    """动作签名冷却：同一动作在 ttl 内只执行一次。"""

    def __init__(self, window: int = 40):
        self.window = max(1, int(window))
        self._items = deque(maxlen=self.window)
        self._lock = RLock()

    def is_duplicate(self, signature: str, ttl: float = 120.0) -> bool:
        signature = str(signature or "")
        if not signature:
            return False
        now = time.time()
        with self._lock:
            self._items = deque(
                ((sig, at) for sig, at in self._items if now - at < max(1.0, ttl)),
                maxlen=self.window,
            )
            for sig, _ in self._items:
                if sig == signature:
                    return True
                # app_do 的自然语言任务经常换皮重复；用文本相似度挡近义改写。
                if signature.startswith("app_do:") and sig.startswith("app_do:"):
                    if similarity(signature[7:], sig[7:]) >= 0.72:
                        return True
            return False

    def add(self, signature: str) -> None:
        signature = str(signature or "")
        if not signature:
            return
        with self._lock:
            self._items.append((signature, time.time()))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


OUTPUT_GUARD = RecentOutputGuard()
ACTION_GUARD = ActionGuard()
