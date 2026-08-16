# core/cognition_runner.py
# ========================================================================
# 认知循环独立运行器（已与 qq_bot / NapCat 解耦）
#
# 背景：NapCat 已弃用，认知循环不再依赖任何外部连接。本模块提供
# 一个独立的线程 + 事件循环来常驻运行 cognitive_loop，并支持优雅
# 启动 / 停止（只停止认知循环，不影响主程序与面板）。
#
# 发言去向（与 QQ 逻辑保持一致）：
#   - 写入历史对话与记忆库（add_to_history）
#   - 推送到对话测试页（BUS.emit_message）
#
# 用法：
#   from core.cognition_runner import COGNITION
#   COGNITION.start()      # 启动/重启认知循环（异步线程）
#   COGNITION.stop()       # 优雅停止（当前轮完成后退出）
#   COGNITION.is_running() # 是否运行中
#   COGNITION.shutdown()   # 主程序退出时调用
# ========================================================================

import asyncio
import logging
import threading

from core.cognition import cognitive_loop, request_graceful_stop

logger = logging.getLogger("CognitionRunner")


class CognitionRunner:
    """认知循环线程管理器。

    - 独立线程运行自己的 asyncio 事件循环
    - start() 幂等：已在运行则直接返回
    - stop() 优雅停止：请求认知循环完成当前轮后退出，不阻塞调用方
    """

    def __init__(self):
        self._thread = None
        self._loop = None
        self._task = None
        self._lock = threading.RLock()

    # ---------------- 状态 ----------------
    def is_running(self) -> bool:
        """认知循环是否运行中。"""
        return self._task is not None and not self._task.done()

    # ---------------- 启动 ----------------
    def start(self):
        """启动（或重启）认知循环。幂等：已在运行则忽略。"""
        with self._lock:
            if self.is_running():
                logger.info("认知循环已在运行")
                return
            # 清理可能残留的旧实例
            if self._thread and self._thread.is_alive():
                # 旧线程尚未完全退出，先请求停止等它退出
                request_graceful_stop()
                self._thread.join(timeout=5)
            self._thread = threading.Thread(
                target=self._run, name="CognitionLoop", daemon=True
            )
            self._thread.start()
            logger.info("认知循环已启动")

    def _run(self):
        """线程入口：创建独立事件循环运行 cognitive_loop。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            # cognitive_loop 不再传 send_func/target_group_id：
            # 发言只写入历史/记忆库并推送到对话测试页（与 NapCat 解耦）
            self._task = loop.create_task(cognitive_loop())
            logger.info("认知循环线程已就绪")
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("认知循环异常退出")
        finally:
            # 清理事件循环
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._task = None
            logger.info("认知循环已停止")

    # ---------------- 停止 ----------------
    def stop(self):
        """优雅停止认知循环。

        请求当前轮完成（跳过复搜）后退出；不在此阻塞等待，
        由后台线程自然收尾，避免阻塞调用方（如 aiohttp 事件循环）。
        """
        with self._lock:
            if not self.is_running():
                logger.info("认知循环未在运行")
                return
            request_graceful_stop()
            logger.info("认知循环停止信号已发送（当前轮完成后退出）")

    # ---------------- 退出清理 ----------------
    def shutdown(self):
        """主程序退出时调用：停止认知循环并等待线程完全结束。"""
        with self._lock:
            if self.is_running():
                request_graceful_stop()
        if self._thread:
            try:
                self._thread.join(timeout=15)
            except Exception:
                pass
        logger.info("认知循环已清理")


# 全局单例
COGNITION = CognitionRunner()
