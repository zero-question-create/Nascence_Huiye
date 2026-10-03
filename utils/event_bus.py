# utils/event_bus.py
#
# 事件总线：核心逻辑通过它向外广播日志、状态、消息等事件。
#
# 设计要点：不再依赖 PyQt（原实现继承 QObject 并用 pyqtSignal）。
# 核心进程（qq_bot / 认知循环）不应该为了发一条事件就拖进整个 GUI 库，
# 面板换成 Web 之后更是如此。改为纯 Python 观察者模式，
# 并**保留 .connect(callback) 的调用方式**，使既有代码无需改动。
#
# 订阅方（控制面板 / Web 推送）在自己的线程上调用 connect 注册回调；
# emit 在发布方线程同步调用回调，回调实现需自行保证线程安全与快速返回
# （Web 侧应只做入队，不要在这里写网络 IO）。

import logging
import threading


class _Signal:
    """最小可用的信号：支持 connect / disconnect / emit，兼容原 pyqtSignal 用法。"""

    def __init__(self, name: str):
        self._name = name
        self._slots = []
        self._lock = threading.Lock()

    def connect(self, slot):
        """注册回调。重复注册同一回调会被忽略。"""
        if slot is None:
            return
        with self._lock:
            if slot not in self._slots:
                self._slots.append(slot)

    def disconnect(self, slot=None):
        """移除回调；不传参数时清空全部。"""
        with self._lock:
            if slot is None:
                self._slots.clear()
            elif slot in self._slots:
                self._slots.remove(slot)

    def emit(self, *args):
        """同步调用所有回调。单个回调异常不影响其他回调。"""
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            try:
                slot(*args)
            except Exception:
                # 事件总线绝不应因为某个订阅者出错而拖垮发布方
                logging.exception(f"事件 {self._name} 的订阅者执行失败")


class EventBus:
    log = _Signal("log")                 # (分类, 文本)
    status = _Signal("status")           # (状态文本)
    stage = _Signal("stage")             # (阶段文本, 是否完成) 初始化/关停进度
    chat_reply = _Signal("chat_reply")   # (发送者, 内容)
    task_error = _Signal("task_error")   # (错误文本)
    message = _Signal("message")         # (发送者, 内容, 来源)


BUS = EventBus()
