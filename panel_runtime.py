# panel_runtime.py
# ========================================================================
# 面板共享运行环境（无 GUI 依赖）。
#
# 桌面控制面板（control_panel.py，PyQt5）与 Web 控制面板（web_panel.py）
# 共用这里的 Runtime：负责 Ollama 启停、QQ 服务线程、伪造发送/思考、
# 记忆注入、落盘与关停。此前这些逻辑内嵌在 control_panel.py 里，
# 导致 Web 面板被迫 import PyQt5；抽出后两个面板互不依赖，
# 核心代码（认知循环、记忆引擎、生物钟）仍一行不动。
#
# 约束：本模块不得 import 任何 GUI 库（PyQt5 等）。
# ========================================================================

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
try:
    import fcntl
except ImportError:
    import msvcrt
    fcntl = None
from datetime import datetime
from pathlib import Path

from config.api_config import DEFAULT_CONFIG
from config.constants import BOT_NAME

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
SESSION_LOG = LOG_DIR / f"panel_{datetime.now():%Y%m%d_%H%M%S}.log"
PANEL_LOCK_FILE = RUN_DIR / "control_panel.lock"

LOG_CAT_RUNTIME = "runtime"
LOG_CAT_THINKING = "thinking"
LOG_CAT_QQBOT = "qqbot"
LOG_CAT_OLLAMA = "ollama"

# 面板显示上限：只保留最近 N 个文本块，避免长时间运行后控件占用持续膨胀。
# 注意这是"显示"上限，磁盘日志仍由 RotatingFileHandler 独立轮转。
LOG_VIEW_MAX_BLOCKS = 500
CHAT_VIEW_MAX_BLOCKS = 300        # 对话测试页气泡上限
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3


def _check_clock_offset(clock):
    """启动自检：虚拟时钟换算后应与墙钟一致。

    正常启动路径（enable_qq_mode 的 else 分支）会重锚基准，使
    to_real_time(now()) == time.time()。若偏差过大，通常是存档被外部脚本
    以不完整流程写入（未经 enable_qq_mode 重锚），此时相对时间短语会整体失真。
    这里只告警、不修改时钟，避免掩盖问题。
    """
    try:
        drift = abs(clock.to_real_time(clock.now()) - time.time())
    except Exception:
        return
    if drift > 3600:
        logging.warning(
            "虚拟时钟偏移异常：to_real_time(now) 与墙钟相差 %.1f 小时，"
            "相对时间短语可能失真（若为外部脚本写入的存档，重启一次即可重锚）",
            drift / 3600,
        )


class SignalLogHandler(logging.Handler):
    """把日志转发到事件总线，供面板（桌面或 Web）实时显示。"""

    def emit(self, record):
        try:
            from utils.event_bus import BUS
            cat = self._categorize(record)
            BUS.log.emit(cat, self.format(record))
        except Exception:
            pass

    @staticmethod
    def _categorize(record):
        name = record.name
        # 自训练与世界生成已删除，对应的日志来源不再存在
        if name == "Nascence.Processing":
            return LOG_CAT_THINKING
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)
        if "[Ollama]" in msg or msg.startswith("[Ollama]"):
            return LOG_CAT_OLLAMA
        if "[主动发言]" in msg:
            return LOG_CAT_THINKING
        qq_keywords = ("QQ 服务", "QQ Bot", "QQBot", "NapCat", "WebSocket")
        if any(k in msg for k in qq_keywords):
            return LOG_CAT_QQBOT
        return LOG_CAT_RUNTIME


class StreamToLogger:
    def __init__(self, logger, level):
        self.logger = logger
        self.level = level
        self.buffer = ""

    def write(self, text):
        self.buffer += str(text)
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                self.logger.log(self.level, line)
        return len(str(text))

    def flush(self):
        if self.buffer:
            self.logger.log(self.level, self.buffer)
            self.buffer = ""

    def isatty(self):
        return False


def configure_logging():
    """统一日志配置：轮转写盘 + 终端输出 + 事件总线转发。

    桌面面板与 Web 面板都调用它，保证日志页/日志推送的内容一致。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    # 轮转写盘：单文件超过上限自动切分并保留有限份数，避免长时间运行撑满磁盘
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        SESSION_LOG, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    signal_handler = SignalLogHandler()
    signal_handler.setFormatter(formatter)
    root.addHandler(signal_handler)

    original_stdout = sys.__stdout__
    terminal_handler = logging.StreamHandler(original_stdout)
    terminal_handler.setFormatter(formatter)
    root.addHandler(terminal_handler)
    sys.stdout = StreamToLogger(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = StreamToLogger(logging.getLogger("stderr"), logging.ERROR)


def acquire_panel_lock():
    """单实例锁：同一时间只允许一个面板（桌面或 Web）管理运行环境。

    返回已持有的锁文件对象；已被其他面板占用时返回 None。
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lock_file = PANEL_LOCK_FILE.open("w", encoding="utf-8")
    except (OSError, PermissionError):
        return None
    if fcntl:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return None
    else:
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock_file.close()
            return None
    try:
        lock_file.write(str(os.getpid()))
        lock_file.flush()
    except OSError:
        pass
    return lock_file


def release_panel_lock(lock_file):
    if lock_file is None:
        return
    try:
        if fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    except (OSError, PermissionError):
        pass
    try:
        lock_file.close()
    except OSError:
        pass


class Runtime:
    def __init__(self):
        self.initialized = False
        self.ollama_process = None
        self.qq_thread = None
        self.qq_loop = None
        self.qq_task = None
        self._lock = threading.Lock()
        self._fake_lock = threading.Lock()
        self._shutdown_done = False

    def start_ollama(self):
        import requests
        session = requests.Session()
        session.trust_env = False

        api_url = "http://127.0.0.1:11434/api/tags"

        def api_ready():
            try:
                response = session.get(api_url, timeout=1)
                response.raise_for_status()
                response.json()
                return True
            except Exception:
                return False

        def port_in_use():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                return sock.connect_ex(("127.0.0.1", 11434)) == 0

        if api_ready():
            logging.info("Ollama 服务已在运行，直接复用")
            return

        # 端口已被其他 Ollama 实例占用时，只等待它完成启动，不再启动第二个实例。
        if port_in_use():
            logging.info("检测到 11434 端口已有服务，等待该 Ollama 完成启动")
            for _ in range(60):
                if api_ready():
                    logging.info("已连接到现有 Ollama 服务")
                    return
                time.sleep(0.5)
            raise RuntimeError("11434 端口已被其他服务占用，且 Ollama API 未就绪")

        binary_name = "ollama.exe" if sys.platform == "win32" else "ollama"
        binary = PROJECT_DIR / "ollama" / "bin" / binary_name
        if not binary.exists():
            raise FileNotFoundError(f"未找到项目内 Ollama: {binary}")
        env = os.environ.copy()
        env["OLLAMA_HOME"] = str(PROJECT_DIR / "ollama" / "home")
        env["OLLAMA_MODELS"] = str(PROJECT_DIR / "ollama" / "home" / "models")
        self.ollama_process = subprocess.Popen(
            [str(binary), "serve"],
            cwd=PROJECT_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(
            target=self._pipe_process_output,
            args=(self.ollama_process, "Ollama"),
            daemon=True,
        ).start()
        for _ in range(60):
            if self.ollama_process.poll() is not None:
                # 竞态下另一实例可能刚刚接管端口，优先尝试复用它。
                if api_ready():
                    self.ollama_process = None
                    logging.info("检测到其他 Ollama 已接管服务，直接复用")
                    return
                raise RuntimeError("Ollama 启动失败，请查看完整日志")
            if api_ready():
                logging.info("Ollama 服务启动完成")
                return
            time.sleep(0.5)
        raise TimeoutError("等待 Ollama 启动超时")

    @staticmethod
    def _pipe_process_output(process, name):
        if process.stdout:
            for line in process.stdout:
                logging.info("[%s] %s", name, line.rstrip("\n"))

    def initialize(self):
        with self._lock:
            if self.initialized:
                return
            logging.info("开始初始化 Nascence 运行环境")
            self.start_ollama()
            from core.memory_engine import get_model, memories, _init_metrics_counters
            from core.virtual_clock import clock
            from main import cold_start_batch_injection
            from utils.persistence import load_all_data, load_state, save_all_data, save_state
            from core.llm_interface import load_dialogue_history

            clock.enable_qq_mode()
            clock.set_speed(1)
            _check_clock_offset(clock)
            get_model()
            load_all_data()
            load_state()
            load_dialogue_history()
            _init_metrics_counters()
            try:
                from core.concept_store import reload_from_db, stats as concept_stats
                reload_from_db()
                logging.info("概念层已加载：%s", concept_stats())
            except Exception:
                logging.exception("概念层初始化失败，本次运行将不启用概念检索")
            if not memories:
                cold_start_batch_injection()
                save_all_data()
                save_state()
            self.initialized = True
            logging.info("运行环境初始化完成，记忆数=%d", len(memories))

    def start_qq(self):
        self.initialize()
        if self.qq_thread and self.qq_thread.is_alive():
            logging.info("QQ 服务已经在运行")
            return
        self.qq_thread = threading.Thread(target=self._qq_main, name="QQBotService", daemon=True)
        self.qq_thread.start()

    def _qq_main(self):
        from utils.event_bus import BUS
        try:
            import qq_bot

            self.qq_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.qq_loop)
            qq_bot.init_sleep_state()
            self.qq_task = self.qq_loop.create_task(qq_bot.start_server())
            BUS.status.emit("QQ 服务运行中")
            logging.info("QQ Bot 后台服务已启动")
            self.qq_loop.run_until_complete(self.qq_task)
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("QQ Bot 服务异常退出")
        finally:
            if self.qq_loop:
                pending = asyncio.all_tasks(self.qq_loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.qq_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self.qq_loop.close()
            self.qq_loop = None
            self.qq_task = None
            BUS.status.emit("QQ 服务已停止")
            logging.info("QQ Bot 后台服务已停止")

    def stop_qq(self):
        if not self.qq_loop or not self.qq_task:
            return
        # 优雅停止：置位停止信号 → start_server 先等认知循环完成当前轮，再断开 NapCat
        try:
            import qq_bot

            def _trigger():
                if qq_bot._shutdown_event is None:
                    # 服务尚未初始化停止信号，退化为直接取消
                    if not self.qq_task.done():
                        self.qq_task.cancel()
                else:
                    qq_bot.request_shutdown()

            self.qq_loop.call_soon_threadsafe(_trigger)
        except Exception:
            try:
                self.qq_loop.call_soon_threadsafe(self.qq_task.cancel)
            except Exception:
                pass
        if self.qq_thread:
            # 关停已走优雅路径（等认知循环收尾 + 落盘），给足时间但不再无限等待
            self.qq_thread.join(timeout=30)

    def set_speed(self, speed):
        self.initialize()
        from core.virtual_clock import clock

        if clock.qq_mode:
            raise RuntimeError("QQ 模式下虚拟时间与真实时间同步，无法修改倍速")
        value = clock.set_speed(speed)
        clock.save_state()
        logging.info("虚拟时间倍速已调整为 %s", value)
        return value

    def fake_send(self, text):
        self.initialize()
        with self._fake_lock:
            if not self.qq_loop or not self.qq_task or self.qq_task.done():
                raise RuntimeError("QQ 服务未运行，无法通过 NapCat 发送消息")
            import qq_bot
            if qq_bot._napcat_websocket is None:
                raise RuntimeError("NapCat 尚未连接，无法发送消息")
            from core.memory_engine import create_memory
            from utils.message_history import add_message
            from utils.persistence import save_all_data

            group_id = qq_bot.get_active_group_id()
            fut = asyncio.run_coroutine_threadsafe(
                qq_bot.send_group_msg(group_id, text), self.qq_loop
            )
            fut.result(timeout=10)
            add_message(BOT_NAME, text, "伪造")
            memory_id = create_memory(f"我说：{text}")
            save_all_data()
            logging.info("伪造发送成功：群=%s 内容=%s 记忆ID=%s", group_id, text, memory_id)
            return memory_id

    def fake_think(self, text):
        self.initialize()
        with self._fake_lock:
            from core.cognition import generate_response
            from utils.message_history import add_message
            from utils.persistence import save_all_data, save_state

            augmented = f"我（{BOT_NAME}）自己想着：{text}"
            # 调用核心思考流程（LLM 拆解→检索→扩散→拼接）
            # 返回 (reply, user_input, new_mem_ids, should_speak)
            # "伪造思考"本身就是模拟内部想法，不区分 say（generate_response
            # 内部已按 say 决定记为"我说"还是"我想"）
            reply, _, _new_mem_ids, _should_speak = generate_response(augmented)
            reply = str(reply or "（静默）").strip()
            memory_text = f"我想：{reply}"
            add_message(BOT_NAME, memory_text, "伪造思考")
            save_all_data()
            save_state()
            logging.info("伪造思考完成：输入=%s 想法=%s", text, reply)
            return reply

    def inject_memory(self, content):
        self.initialize()
        from core.memory_engine import create_memory
        from utils.persistence import save_all_data

        memory_id = create_memory(content)
        save_all_data()
        logging.info("管理员注入记忆成功，ID=%s，内容=%s", memory_id, content)
        return memory_id

    def save(self, force: bool = False):
        if not self.initialized:
            return
        from utils.persistence import save_all_data, save_state

        save_all_data(force=force)
        save_state()
        logging.info("记忆和对话状态已完整保存")

    def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logging.info("控制面板正在停止全部服务")
        self.stop_qq()
        try:
            # 关停是最后一次落盘机会，绕过节流确保写入
            self.save(force=True)
        except Exception:
            logging.exception("退出保存失败")
        # 对话历史缓冲一并落盘（与桌面面板的关停流程保持一致）
        try:
            from utils.message_history import flush_to_file
            flush_to_file()
        except Exception:
            logging.exception("对话历史落盘失败")
        if self.ollama_process and self.ollama_process.poll() is None:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.ollama_process.pid)],
                                   capture_output=True, timeout=5)
                else:
                    os.killpg(os.getpgid(self.ollama_process.pid), signal.SIGTERM)
                self.ollama_process.wait(timeout=8)
            except Exception:
                try:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.ollama_process.pid)],
                                       capture_output=True, timeout=5)
                    else:
                        os.killpg(os.getpgid(self.ollama_process.pid), signal.SIGKILL)
                except Exception:
                    pass
        logging.info("全部服务已停止")


RUNTIME = Runtime()


# ---------- 配置文件读写（供两个面板共用） ----------

def load_panel_config():
    path = PROJECT_DIR / "config" / "api_config.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    with path.open("r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    # 与 config.load_config 一致地补齐缺省键：
    # 用户的存档往往只有 API/QQ 字段，生物钟等新参数要显示实际生效值而非空白。
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_panel_config(cfg):
    path = PROJECT_DIR / "config" / "api_config.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def reload_biorhythm_params():
    """把最新配置里的生物钟参数热加载进动力学模块。"""
    try:
        from core import biorhythm as _bio
        _bio._load_params()
    except Exception:
        logging.exception("生物钟参数热加载失败")


def format_error(details):
    """从 traceback 文本里提取最后一行作为简短错误信息。"""
    if not details:
        return "未知错误"
    last = details.strip().split("\n")[-1]
    return last if last else "未知错误"
