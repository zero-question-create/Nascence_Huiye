# core/parse_worker.py
# ========================================================================
# 解析单线程：把“理解层 + 写入记忆 + 概念归档”从事件循环里挪走。
#
# 为什么需要它：
#   decompose_input 是同步 LLM 调用，create_memory 还要同步请求 Ollama 嵌入。
#   直接在 handle_group_message 里跑，会把 asyncio 事件循环整段堵住——
#   期间收不到新消息、发不出回执、生物钟 tick 也停摆。
#
# 为什么是“单线程”而不是线程池：
#   并发会让消息乱序：后到的消息可能先写记忆，对话状态与概念事件段也随之错乱。
#   这里用一条常驻工作线程 + 有界队列，保证处理顺序与到达顺序一致；
#   事件循环侧通过 future 等待结果，等待期间照常处理其他协程。
# ========================================================================

import asyncio
import queue
import threading

from utils.metrics_probe import PROBE
from utils.monitor import append_log


class ParseWorker:
    def __init__(self, max_queue: int = 200):
        self._queue = queue.Queue(maxsize=max(1, int(max_queue)))
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ParseWorker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        # 放一个哨兵，避免线程卡在 get 上
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    async def run(self, fn, *args, **kwargs):
        """在解析线程里执行 fn，异步等待结果；队列满时施加背压而不是丢消息。"""
        if self._thread is None or not self._thread.is_alive():
            self.start()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        item = (fn, args, kwargs, loop, future)
        PROBE.gauge("parse.queue", self._queue.qsize())
        await loop.run_in_executor(None, self._enqueue_blocking, item)
        return await future

    def _enqueue_blocking(self, item):
        # 在默认线程池里阻塞入队，避免事件循环被 queue.put 的等待卡住
        self._queue.put(item)

    def _run(self):
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            fn, args, kwargs, loop, future = item
            try:
                result = fn(*args, **kwargs)
                loop.call_soon_threadsafe(self._resolve, future, result, None)
            except Exception as e:
                loop.call_soon_threadsafe(self._resolve, future, None, e)
            finally:
                PROBE.gauge("parse.queue", self._queue.qsize())

    @staticmethod
    def _resolve(future, result, error):
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)


WORKER = ParseWorker()


def start_parse_worker():
    WORKER.start()


def stop_parse_worker():
    WORKER.stop()


async def run_in_parse_worker(fn, *args, **kwargs):
    """模块级快捷入口。"""
    return await WORKER.run(fn, *args, **kwargs)
