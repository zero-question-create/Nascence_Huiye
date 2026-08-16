# utils/event_bus.py
# ========================================================================
# 轻量事件总线（纯 Python 实现，无 PyQt 依赖）
#
# 原实现基于 PyQt5 的 QObject / pyqtSignal，在迁移 WebUI 后不再需要 Qt。
# 本模块提供线程安全的发布-订阅模型，供以下场景使用：
#   - 日志实时推送到 WebUI（WebSocket 广播）
#   - QQ 服务 / 自训练 状态变化
#   - 对话消息气泡推送
#   - 任务错误上报
#
# 用法：
#   BUS.subscribe("log", handler)      # handler(category, line)
#   BUS.publish("log", "runtime", "...")
# ========================================================================

import threading


class EventBus:
    """线程安全的简单发布-订阅事件总线。"""

    def __init__(self):
        # 主题 -> 回调函数列表
        self._subscribers = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscribe(self, topic: str, handler):
        """注册某主题的回调。handler(topic, *args)。返回可取消订阅的函数。"""
        with self._lock:
            self._subscribers.setdefault(topic, []).append(handler)

        def unsubscribe():
            with self._lock:
                handlers = self._subscribers.get(topic)
                if handlers and handler in handlers:
                    handlers.remove(handler)

        return unsubscribe

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(self, topic: str, *args):
        """向某主题的所有订阅者广播参数。回调异常不影响其他订阅者。"""
        with self._lock:
            handlers = list(self._subscribers.get(topic, []))
        for handler in handlers:
            try:
                handler(topic, *args)
            except Exception:
                # 单个订阅者出错不应中断广播链路
                import logging
                logging.getLogger("event_bus").exception("事件订阅者回调异常 topic=%s", topic)

    # ------------------------------------------------------------------
    # 便捷封装（保持与原 pyqtSignal 用法接近）
    # ------------------------------------------------------------------
    def emit_log(self, category: str, line: str):
        """广播一条日志。"""
        self.publish("log", category, line)

    def emit_status(self, text: str):
        """广播状态变化（如 QQ 服务运行中/已停止）。"""
        self.publish("status", text)

    def emit_message(self, sender: str, content: str, source: str):
        """广播一条对话消息（用于 WebUI 聊天气泡）。"""
        self.publish("message", sender, content, source)

    def emit_error(self, details: str):
        """广播任务错误。"""
        self.publish("error", details)


# 全局单例（所有模块 import 此对象使用）
BUS = EventBus()
