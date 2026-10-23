# utils/metrics_probe.py
# ========================================================================
# 轻量运行时观测探针。
#
# 只做“记录”，不做任何决策：
#   - counter: 单调递增的调用次数/事件次数
#   - timing:  有界样本的耗时分布（p50/p95/max）
#   - gauge:   即时值（队列长度、热记忆数量等）
#
# 采样有界，长时间运行不会无限增长；reset() 供测试或面板清零。
# ========================================================================

import threading
import time
from contextlib import contextmanager
from collections import deque

_MAX_SAMPLES = 1000


class MetricsProbe:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {}
        self._timings = {}
        self._gauges = {}

    # ---------- 写入 ----------
    def bump(self, name: str, n: int = 1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + int(n)

    def gauge(self, name: str, value):
        with self._lock:
            self._gauges[name] = value

    def record_ms(self, name: str, ms: float):
        samples = self._timings.get(name)
        if samples is None:
            samples = deque(maxlen=_MAX_SAMPLES)
            self._timings[name] = samples
        samples.append(float(ms))

    @contextmanager
    def timed(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record_ms(name, (time.perf_counter() - started) * 1000.0)

    async def timed_async(self, name: str):
        """异步上下文管理器：async with PROBE.timed_async("x"): ..."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record_ms(name, (time.perf_counter() - started) * 1000.0)

    # ---------- 读取 ----------
    @staticmethod
    def _percentile(values, q):
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
        return ordered[idx]

    def snapshot(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            timings = {}
            for name, samples in self._timings.items():
                values = list(samples)
                if not values:
                    continue
                timings[name] = {
                    "count": len(values),
                    "avg_ms": round(sum(values) / len(values), 2),
                    "p50_ms": round(self._percentile(values, 0.50), 2),
                    "p95_ms": round(self._percentile(values, 0.95), 2),
                    "max_ms": round(max(values), 2),
                }
        return {"counters": counters, "gauges": gauges, "timings": timings}

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._timings.clear()
            self._gauges.clear()


class TimedRLock:
    """带等待时间观测的可重入锁。

    只记录最外层 acquire 的等待时间；重入时等待为 0，不污染样本。
    """

    def __init__(self, name: str):
        self._lock = threading.RLock()
        self._name = name
        self._depth = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1):
        started = time.perf_counter()
        if timeout >= 0:
            acquired = self._lock.acquire(blocking, timeout)
        else:
            acquired = self._lock.acquire(blocking)
        if acquired:
            depth = getattr(self._depth, "value", 0)
            if depth == 0:
                PROBE.record_ms(f"lock.{self._name}.wait", (time.perf_counter() - started) * 1000.0)
            self._depth.value = depth + 1
        return acquired

    def release(self):
        depth = getattr(self._depth, "value", 1)
        self._depth.value = max(0, depth - 1)
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


PROBE = MetricsProbe()
