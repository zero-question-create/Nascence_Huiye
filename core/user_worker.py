# core/user_worker.py
# ========================================================================
# 单用户专属独立子进程 Worker
#
# 架构设计：
#   1. 每个注册/登录的用户拥有专属的独立子进程（UserWorkerProcess）。
#   2. 子进程内拥有独立的 Python 进程空间、独立的数据目录沙箱（data/<username>/）、
#      独立的 SQLite 连接、独立的 FAISS 向量索引与独立的词网倒排索引。
#   3. 子进程在收到 start_cognition 指令后，在其子进程内部启动属于该用户的
#      认知循环（core.cognition_runner.COGNITION），主进程绝不运行认知循环。
#   4. 所有子进程的模型调用统一向主进程的共享模型后端（llama-server 11437 / API）
#      发起 HTTP 请求，排队共用同一套显存与权重。
#   5. 所有子进程的日志通过 multiprocessing.Queue 统一发回主进程打印并推送前端。
# ========================================================================

import asyncio
import logging
import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
from logging.handlers import QueueHandler
from typing import Dict, Optional

# Windows Job Object：统一由 core.win_job 提供单一 Job，主进程退出（含强杀）时
# 操作系统自动销毁所有绑定到该 Job 的用户子进程，杜绝孤儿进程。
from core.win_job import assign_process as _assign_process


def _handle_user_message(username: str, sender: str, text: str):
    """后台线程处理用户消息 —— 复刻 0.6.6 qq_bot 的「理解层」消息处理架构：

    1. 消息解读 LLM（decompose_input）将输入拆分为 记忆片段 / 模式 / 新状态 / 关键词
    2. 记忆在解读完直接入库（create_memory，按模式半衰期）
    3. 更新对话状态
    4. 消息 jieba 分词关键词注入认知循环队列 —— 下一轮认知循环会将其与
       「上一轮回复结果分词得到的关键词」合并后一起检索
    5. 用户消息写入对话历史（前端即时同步）
    回复由该用户的认知循环异步产出（消费队列关键词 → 检索 → verbalize →
    发言写入历史并推送），前端轮询历史即可获取。
    """
    logger = logging.getLogger(f"UserWorker[{username}]")
    try:
        from core.cognition import inject_message_keywords, extract_keywords_jieba, MODE_HALF_LIFE
        from core.llm_interface import decompose_input
        from core.memory_engine import create_memory
        from utils.dialogue_state import set_state
        from utils.message_history import add_message as _add_message
        from utils.message_history import get_all as _get_messages
        from utils.persistence import save_all_data, save_state

        augmented = f"{sender}说：{text}"
        logger.info(f"[用户对话] 发送者={sender} 输入={augmented}")

        # 发送的消息立即加入历史对话（前端可即时同步到"已发送"状态）
        _prev = _get_messages()
        prev_time = _prev[-1]["time"] if _prev else None
        _add_message(sender, text, "客户端")
        _cur = _get_messages()
        cur_time = _cur[-1]["time"]
        think_seconds = round(cur_time - prev_time, 2) if prev_time is not None else 0.0

        # ===== 理解层：消息解读 LLM 拆分为记忆 / 关键词 / 状态 =====
        mem_fragments, mode, new_state, _keywords = decompose_input(augmented)
        if new_state:
            set_state(new_state)
            save_state()
        # 记忆在解读完直接入库（不去重，按模式半衰期）
        half_life = MODE_HALF_LIFE.get(mode, 2 * 24 * 3600)
        for frag in mem_fragments:
            create_memory(frag, half_life=half_life)

        # 消息 jieba 分词关键词注入认知循环队列
        # （下一轮认知循环与上一轮回复分词合并后一起搜索）
        msg_keywords = extract_keywords_jieba(text)
        if msg_keywords:
            inject_message_keywords(msg_keywords)

        save_all_data()
        save_state()
        logger.info(f"[用户对话] 理解层完成，记忆入库 {len(mem_fragments)} 条，"
                    f"jieba关键词: {msg_keywords} (思考时间={think_seconds}s)")
        # 注意：这里不负责启动认知循环——认知循环由 start_cognition 指令统一管理。
        # 若在停止后这里又启动，会把刚停止的循环复活（实测复现过）。
        # 回复由"正在运行"的认知循环消费队列关键词后异步产出并写入历史。
    except Exception as e:
        logger.error(f"消息理解处理异常: {e}\n{traceback.format_exc()}")


def _worker_main(
    username: str,
    req_queue: multiprocessing.Queue,
    resp_queue: multiprocessing.Queue,
    log_queue: multiprocessing.Queue,
    project_dir: str,
):
    """子进程顶层入口函数（兼容 Windows spawn 与 Linux fork）。"""
    # 0. 标记为子进程环境：本进程绝不启动本地 llama-server（模型统一由主进程
    #    启动并共享，所有认知循环排队共用同一个 llama-server，防止多实例争抢端口）。
    os.environ["NASCENCE_IS_WORKER"] = "1"

    # 1. 确保 sys.path 包含项目根目录
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)

    # 2. 配置子进程日志：将子进程所有日志通过 QueueHandler 发往主进程
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = []
    q_handler = QueueHandler(log_queue)
    root_logger.addHandler(q_handler)

    logger = logging.getLogger(f"UserWorker[{username}]")
    logger.info(f"用户沙箱子进程已启动 (PID={os.getpid()}, User={username})")

    try:
        # 3. 先导入所有数据模块（它们会在导入时向 runtime_paths 注册路径刷新回调）
        from core import runtime_paths
        from utils.message_history import load_state as load_history_state
        from utils.persistence import load_all_data, load_state, save_all_data, save_state
        from core.cognition_runner import COGNITION
        from core.cognition import process_dialogue
        from config.constants import BOT_NAME
        from utils.event_bus import BUS

        # 4. 再隔离数据目录路径（导入完成后切换，保证各模块路径常量刷新到该用户目录）
        runtime_paths.set_user(username, base_dir=project_dir)

        # 挂载事件总线转发：子进程产生的 message/status 转发回主进程推送前端
        def _ipc_event_forwarder(topic, *args):
            try:
                log_queue.put_nowait({
                    "_ipc_type": "bus_event",
                    "username": username,
                    "topic": topic,
                    "args": args,
                })
            except Exception:
                pass

        BUS.subscribe("message", _ipc_event_forwarder)
        BUS.subscribe("status", _ipc_event_forwarder)

        # 加载沙箱历史与记忆
        load_history_state()
        load_state()
        load_all_data()

        logger.info(f"用户沙箱环境初始化完成，开始监听指令队列")

        # 5. 主循环：监听处理主进程发来的请求
        while True:
            try:
                msg = req_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if msg is None:
                logger.info(f"收到终止指令，正在安全关闭沙箱子进程...")
                # 优雅停止认知循环：完成当前轮（含复搜上限）后再退出，
                # 避免强杀导致认知状态/记忆不一致。
                try:
                    from core.cognition_runner import COGNITION
                    COGNITION.shutdown()
                    logger.info(f"认知循环已优雅停止")
                except Exception:
                    logger.exception("优雅停止认知循环失败")
                # 最终保存一次沙箱数据
                try:
                    from utils.persistence import save_all_data, save_state
                    save_all_data()
                    save_state()
                    logger.info(f"沙箱数据已最终保存")
                except Exception:
                    logger.exception("最终保存失败")
                break

            action = msg.get("action")
            req_id = msg.get("req_id")

            if action == "ping":
                resp_queue.put_nowait({"req_id": req_id, "ok": True, "pong": True, "pid": os.getpid()})

            elif action == "chat":
                sender = msg.get("sender", username)
                text = msg.get("text", "")
                mentioned = msg.get("mentioned", True)

                # 立即确认收到（不阻塞 worker 主循环：停止指令可即时处理，认知循环能及时刹停）
                resp_queue.put_nowait({
                    "req_id": req_id,
                    "ok": True,
                    "reply": "",  # 回复由认知循环异步产出并写入历史，前端轮询获取
                    "think_seconds": 0.0,
                })
                # 理解层（消息解读 LLM 拆记忆/关键词/状态 + 记忆入库 + 关键词入队认知循环）
                # 在后台线程执行，避免长时间阻塞本进程指令队列
                threading.Thread(
                    target=_handle_user_message,
                    args=(username, sender, text),
                    daemon=True,
                ).start()

            elif action == "start_cognition":
                try:
                    if not COGNITION.is_running():
                        COGNITION.start()
                        logger.info(f"用户子进程专属认知循环已启动")
                    resp_queue.put_nowait({"req_id": req_id, "ok": True, "running": True})
                except Exception as e:
                    logger.error(f"启动认知循环失败: {e}\n{traceback.format_exc()}")
                    resp_queue.put_nowait({"req_id": req_id, "ok": False, "error": str(e)})

            elif action == "stop_cognition":
                try:
                    # 优雅停止：设置停止标志（request_graceful_stop），当前轮跑完即退出。
                    # 立即返回，不等待——等待会阻塞本进程指令队列并触发主进程 _call 超时。
                    COGNITION.stop()
                    logger.info(f"认知循环已请求优雅停止（当前轮完成后退出）")
                    resp_queue.put_nowait({"req_id": req_id, "ok": True, "running": False})
                except Exception as e:
                    logger.error(f"停止认知循环失败: {e}\n{traceback.format_exc()}")
                    resp_queue.put_nowait({"req_id": req_id, "ok": False, "error": str(e)})

            elif action == "get_cognition_status":
                resp_queue.put_nowait({
                    "req_id": req_id,
                    "ok": True,
                    "running": COGNITION.is_running(),
                })

    except Exception as e:
        logger.error(f"子进程发生未捕获致命异常退出: {e}\n{traceback.format_exc()}")
    finally:
        try:
            from core.cognition_runner import COGNITION
            if COGNITION.is_running():
                COGNITION.stop()
        except Exception:
            pass
        logger.info(f"用户沙箱子进程已彻底退出 (PID={os.getpid()})")
        # 强制立即退出：避免 multiprocessing.Queue 的 feeder 线程在进程收尾时挂死
        os._exit(0)


class UserWorkerClient:
    """运行在主进程中、用于与专属子进程进行 IPC 通信的客户端句柄。"""

    def __init__(self, username: str, log_queue: multiprocessing.Queue, project_dir: str):
        self.username = username
        self.log_queue = log_queue
        self.project_dir = project_dir
        self.req_queue = multiprocessing.Queue()
        self.resp_queue = multiprocessing.Queue()
        # 可重入锁：_call 持锁期间会调用 ensure_alive（内部同样加锁），
        # 普通 Lock 会导致同一线程二次加锁死锁，必须用 RLock
        self._lock = threading.RLock()
        self.process: Optional[multiprocessing.Process] = None
        self._spawn_process()

    def _spawn_process(self):
        """拉起子进程。"""
        self.process = multiprocessing.Process(
            target=_worker_main,
            args=(
                self.username,
                self.req_queue,
                self.resp_queue,
                self.log_queue,
                self.project_dir,
            ),
            name=f"UserWorker-{self.username}",
            daemon=True,
        )
        self.process.start()
        # 绑定到共享 Job Object：主进程消亡（含强杀）时自动终止子进程
        _assign_process(self.process)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.is_alive()

    def ensure_alive(self):
        """确保子进程处于存活状态，若崩溃则自动重启。"""
        with self._lock:
            if not self.is_alive():
                logger = logging.getLogger(f"UserWorker[{self.username}]")
                logger.warning(f"检测到用户子进程未存活，正在自动拉起新实例...")
                self._spawn_process()

    def _call(self, payload: dict, timeout: float = 120.0) -> dict:
        """同步发送请求并等待专属子进程返回结果。

        注意：等待响应期间绝不持有 self._lock（否则 shutdown 会被卡死），
        仅在入队瞬间使用短锁保证同一时刻只有一个请求在途。
        """
        req_id = payload.get("req_id") or f"req_{time.time()}_{id(payload)}"
        payload["req_id"] = req_id

        # 短锁入队（保证同一子进程同一时刻只有一个请求在途）
        with self._lock:
            self.ensure_alive()
            try:
                self.req_queue.put_nowait(payload)
            except Exception as e:
                return {"ok": False, "error": f"发送指令失败: {e}", "reply": f"【系统】无法连接用户沙箱: {e}"}

        # 无锁等待响应（按 req_id 精确匹配）
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self.resp_queue.get(timeout=0.5)
                if isinstance(resp, dict) and resp.get("req_id") == req_id:
                    return resp
            except queue.Empty:
                # 队列为空时，再检查子进程是否已终止
                if not self.is_alive():
                    return {
                        "ok": False,
                        "error": "Worker 进程异常退出",
                        "reply": "【系统】用户沙箱进程异常退出",
                        "think_seconds": 0.0,
                    }
                continue
            except Exception as e:
                return {"ok": False, "error": str(e), "reply": f"【系统】IPC 错误: {e}"}

        return {
            "ok": False,
            "error": "请求超时",
            "reply": "【系统】用户沙箱响应超时，请稍后重试",
            "think_seconds": timeout,
        }

    def chat(self, sender: str, text: str, mentioned: bool = True, timeout: float = 180.0) -> dict:
        """向子进程发送对话请求。"""
        return self._call({
            "action": "chat",
            "sender": sender,
            "text": text,
            "mentioned": mentioned,
        }, timeout=timeout)

    def start_cognition(self) -> dict:
        """让子进程启动其专属认知循环。"""
        return self._call({"action": "start_cognition"}, timeout=10.0)

    def stop_cognition(self) -> dict:
        """让子进程停止其专属认知循环。"""
        return self._call({"action": "stop_cognition"}, timeout=10.0)

    def get_cognition_status(self) -> dict:
        """获取子进程认知循环状态。"""
        return self._call({"action": "get_cognition_status"}, timeout=5.0)

    def shutdown(self):
        """安全关闭子进程。

        短锁发送 None 哨兵后立即释放锁；随后在无锁状态下 join，若仍存活则强制 kill，
        避免与正在进行的 _call 等待死锁。
        """
        with self._lock:
            if not self.is_alive():
                return
            try:
                self.req_queue.put_nowait(None)
            except Exception:
                pass

        # 无锁等待子进程退出。子进程收到 None 后会优雅停止认知循环（最多约15秒）
        # 并保存沙箱数据，因此给出充足等待时间；仍存活才强制 kill。
        self.process.join(timeout=20.0)
        if self.process.is_alive():
            try:
                self.process.kill()
            except Exception:
                pass
            self.process.join(timeout=1.0)


class UserWorkerPool:
    """主进程中管理所有用户专属子进程的进程池。"""

    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.log_queue = multiprocessing.Queue()
        self._workers: Dict[str, UserWorkerClient] = {}
        self.cognition_enabled = False
        self._lock = threading.Lock()

    def get_worker(self, username: str) -> UserWorkerClient:
        """获取或创建指定用户的专属 Worker 客户端。"""
        with self._lock:
            worker = self._workers.get(username)
            if worker is None:
                worker = UserWorkerClient(username, self.log_queue, self.project_dir)
                self._workers[username] = worker
                if self.cognition_enabled:
                    try:
                        worker.start_cognition()
                    except Exception:
                        pass
            else:
                worker.ensure_alive()
                if self.cognition_enabled:
                    try:
                        worker.start_cognition()
                    except Exception:
                        pass
            return worker

    def get_all_workers(self) -> Dict[str, UserWorkerClient]:
        with self._lock:
            return dict(self._workers)

    def start_cognition_for_all(self):
        """为当前所有已激活的用户子进程启动认知循环。"""
        with self._lock:
            self.cognition_enabled = True
            for username, worker in self._workers.items():
                try:
                    worker.start_cognition()
                except Exception:
                    pass

    def stop_cognition_for_all(self):
        """为当前所有已激活的用户子进程停止认知循环。"""
        with self._lock:
            self.cognition_enabled = False
            for username, worker in self._workers.items():
                try:
                    worker.stop_cognition()
                except Exception:
                    pass

    def shutdown_all(self):
        """关闭所有子进程。"""
        with self._lock:
            for worker in self._workers.values():
                try:
                    worker.shutdown()
                except Exception:
                    pass
            self._workers.clear()
