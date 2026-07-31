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
import traceback
try:
    import fcntl
except ImportError:
    import msvcrt
    fcntl = None
from datetime import datetime
from pathlib import Path
from config.api_config import DEFAULT_CONFIG

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
SESSION_LOG = LOG_DIR / f"panel_{datetime.now():%Y%m%d_%H%M%S}.log"
PANEL_LOCK_FILE = RUN_DIR / "control_panel.lock"

# 允许直接运行 control_panel.py 时也使用项目内 Qt/X11 依赖（仅 Linux）。
if sys.platform == "linux":
    LOCAL_LIB = RUN_DIR / "lib" / "usr" / "lib" / "x86_64-linux-gnu"
    if LOCAL_LIB.is_dir():
        import ctypes, ctypes.util
        os.environ["LD_LIBRARY_PATH"] = f"{LOCAL_LIB}:{os.environ.get('LD_LIBRARY_PATH', '')}".rstrip(":")
        try:
            ctypes.CDLL(str(LOCAL_LIB / "libxcb-cursor.so.0.0.0"), mode=ctypes.RTLD_GLOBAL)
        except OSError:
            pass

# PyQt5 与系统 Fcitx4 Qt5 前端同属 Qt 5.15 ABI，可安全加载候选框插件。
LOCAL_QT_PLUGINS = RUN_DIR / "qt5-plugins"
if (LOCAL_QT_PLUGINS / "platforminputcontexts").is_dir():
    os.environ["QT_PLUGIN_PATH"] = str(LOCAL_QT_PLUGINS)
if sys.platform == "linux":
    os.environ["QT_IM_MODULE"] = "fcitx"
    os.environ.setdefault("GTK_IM_MODULE", "fcitx")
    os.environ.setdefault("XMODIFIERS", "@im=fcitx")

from PyQt5.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from utils.event_bus import BUS


LOG_CAT_RUNTIME = "runtime"
LOG_CAT_THINKING = "thinking"
LOG_CAT_QQBOT = "qqbot"
LOG_CAT_OLLAMA = "ollama"
LOG_CAT_WORLD = "world_gen"


class SignalLogHandler(logging.Handler):
    def emit(self, record):
        try:
            cat = self._categorize(record)
            BUS.log.emit(cat, self.format(record))
        except Exception:
            pass

    @staticmethod
    def _categorize(record):
        name = record.name
        if name in ("Nascence.Processing", "SelfTraining"):
            return LOG_CAT_THINKING
        if name == "WorldGen":
            return LOG_CAT_WORLD
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)
        if "[Ollama]" in msg or msg.startswith("[Ollama]"):
            return LOG_CAT_OLLAMA
        if "[主动发言]" in msg:
            return LOG_CAT_THINKING
        qq_keywords = ("[对话测试]", "QQ 服务", "QQ Bot", "QQBot", "NapCat", "WebSocket")
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


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.result.emit(self.fn(*self.args, **self.kwargs))
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class Runtime:
    def __init__(self):
        self.initialized = False
        self.ollama_process = None
        self.qq_thread = None
        self.qq_loop = None
        self.qq_task = None
        self._lock = threading.Lock()

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
            get_model()
            load_all_data()
            load_state()
            load_dialogue_history()
            _init_metrics_counters()
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
            self.qq_thread.join(timeout=90)

    def chat(self, sender, text, group_id, mentioned):
        self.initialize()
        from core.cognition import process_dialogue
        from core.llm_interface import add_to_history
        from utils.persistence import append_dialogue, save_all_data, save_state

        augmented = (
            f'{sender}对辉夜说：“{text}”'
            if mentioned
            else f'{sender}说：“{text}”'
        )
        logging.info("[对话测试] 群=%s 发送者=%s @辉夜=%s 输入=%s", group_id, sender, mentioned, augmented)
        reply, _ = asyncio.run(process_dialogue(augmented_input=augmented))
        reply = str(reply or "静默")
        if mentioned and reply.strip() in ("", "静默", "静默。", "[SILENT]"):
            reply = "嗯......"
        add_to_history(sender, text, reply, "测试")
        append_dialogue(augmented, reply)
        save_all_data()
        save_state()
        logging.info("[对话测试] 辉夜回复=%s", reply)
        return sender, reply

    def set_speed(self, speed):
        self.initialize()
        from core.virtual_clock import clock

        if clock.qq_mode:
            raise RuntimeError("QQ 模式下虚拟时间与真实时间同步，无法修改倍速")
        value = clock.set_speed(speed)
        clock.save_state()
        logging.info("虚拟时间倍速已调整为 %s", value)
        return value

    def inject_memory(self, content):
        self.initialize()
        from core.memory_engine import create_memory
        from utils.persistence import save_all_data

        memory_id = create_memory(content)
        save_all_data()
        logging.info("管理员注入记忆成功，ID=%s，内容=%s", memory_id, content)
        return memory_id

    def undo_dialogue(self):
        self.initialize()
        from core.cognition import UNDO_FILE
        from core.memory_engine import _data_lock, _rebuild_faiss_index, links, memories, hot_ids
        from utils.dialogue_state import set_state
        from utils.persistence import save_all_data, save_state
        from utils.message_history import remove_last

        if not os.path.exists(UNDO_FILE):
            raise FileNotFoundError("没有可撤销的对话快照")
        with open(UNDO_FILE, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        with _data_lock:
            memories.clear()
            memories.update(snapshot["memories"])
            hot_ids.clear()
            hot_ids.update(snapshot["memories"].keys())
            links.clear()
            for key, value in snapshot["links"].items():
                source, target = key.split("||")
                links[(source, target)] = value
            set_state(snapshot["state"])
            _rebuild_faiss_index()
        save_all_data()
        save_state()
        remove_last(2)
        logging.info("上一轮对话已撤销并完整保存")

    def save(self):
        if not self.initialized:
            return
        from utils.persistence import save_all_data, save_state

        save_all_data()
        save_state()
        logging.info("记忆和对话状态已完整保存")

    def shutdown(self):
        logging.info("控制面板正在停止全部服务")
        self.stop_qq()
        try:
            from self_training import TRAINER
            TRAINER.stop()
        except Exception:
            pass
        try:
            self.save()
        except Exception:
            logging.exception("退出保存失败")
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


class StatCard(QFrame):
    def __init__(self, title, value="--"):
        super().__init__()
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        title_label = QLabel(title)
        title_label.setObjectName("muted")
        self.value = QLabel(value)
        self.value.setObjectName("statValue")
        layout.addWidget(title_label)
        layout.addWidget(self.value)


class ControlPanel(QMainWindow):
    def __init__(self):
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self.active_workers = set()
        self.busy_chat = False
        self.setWindowTitle("Nascence 辉夜 · 控制面板")
        self.resize(1180, 760)
        self.setMinimumSize(900, 620)
        self.log_views = {}
        self._shutdown_done = False
        self._running_service = None  # "qq" | "training" | "chat" | None
        self._warnings = []
        self._errors = []
        self._ignore_errors = False
        self._ignore_warnings = False
        self._build_ui()
        self._connect_bus()
        if SESSION_LOG.exists():
            text = SESSION_LOG.read_text(encoding="utf-8")
            self.log_views[LOG_CAT_RUNTIME].setPlainText(text)
        self._load_config()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_stats)
        self.timer.start(1000)
        self.run_worker(RUNTIME.initialize, on_result=lambda _: self.set_status("核心已就绪"))

    def _build_ui(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 14, 18, 16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("NASCENCE · 辉夜")
        title.setObjectName("title")
        subtitle = QLabel("记忆认知系统 · 本地运行控制台")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.status_label = QLabel("正在初始化")
        self.status_label.setObjectName("statusBadge")
        header.addWidget(self.status_label)
        root_layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._overview_page(), "总览")
        self.tabs.addTab(self._qq_page(), "QQ 服务")
        self.tabs.addTab(self._chat_page(), "对话测试")
        self.tabs.addTab(self._logs_page(), "日志")
        self.tabs.addTab(self._config_page(), "配置")
        self.tabs.addTab(self._maintenance_page(), "维护")
        root_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)

    def _overview_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        cards = QHBoxLayout()
        self.mem_card = StatCard("热记忆节点")
        self.link_card = StatCard("记忆链接")
        self.word_card = StatCard("词网规模")
        self.runtime_card = StatCard("运行时间")
        self.clock_card = StatCard("虚拟时间")
        for card in (self.mem_card, self.link_card, self.word_card, self.runtime_card, self.clock_card):
            cards.addWidget(card)
        layout.addLayout(cards)

        actions = QFrame()
        actions.setObjectName("panel")
        action_layout = QVBoxLayout(actions)
        action_layout.setContentsMargins(6, 4, 6, 4)
        action_layout.addWidget(QLabel("运行控制", objectName="sectionTitle"))
        buttons = QHBoxLayout()
        start_btn = QPushButton("启动 QQ 服务")
        start_btn.clicked.connect(self.start_qq)
        stop_btn = QPushButton("停止 QQ 服务")
        stop_btn.setObjectName("secondaryButton")
        stop_btn.clicked.connect(self.stop_qq)
        save_btn = QPushButton("立即保存全部数据")
        save_btn.setObjectName("secondaryButton")
        save_btn.clicked.connect(lambda: self.run_worker(RUNTIME.save))
        buttons.addWidget(start_btn)
        buttons.addWidget(stop_btn)
        buttons.addWidget(save_btn)
        buttons.addStretch()
        action_layout.addLayout(buttons)

        tools = QHBoxLayout()
        tools.addWidget(QLabel("时间倍速"))
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(1, 10000)
        self.speed_spin.setValue(1)
        self.set_speed_btn = QPushButton("应用倍速", objectName="secondaryButton")
        self.set_speed_btn.clicked.connect(self._apply_speed)
        undo_btn = QPushButton("撤销上一轮对话", objectName="secondaryButton")
        undo_btn.clicked.connect(lambda: self.run_worker(
            RUNTIME.undo_dialogue,
            on_finished=self._rebuild_chat_view
        ))
        tools.addWidget(self.speed_spin)
        tools.addWidget(self.set_speed_btn)
        tools.addWidget(undo_btn)
        tools.addStretch()
        action_layout.addLayout(tools)
        layout.addWidget(actions)

        info = QFrame()
        info.setObjectName("panel")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(14, 10, 14, 10)
        info_layout.setSpacing(10)
        info_header = QHBoxLayout()
        info_header.addWidget(QLabel("运行情况", objectName="sectionTitle"))
        info_header.addStretch()
        self.ignore_err_btn = QPushButton("忽略报错")
        self.ignore_err_btn.setObjectName("secondaryButton")
        self.ignore_err_btn.setCheckable(True)
        self.ignore_err_btn.toggled.connect(self._toggle_ignore_errors)
        info_header.addWidget(self.ignore_err_btn)
        self.ignore_warn_btn = QPushButton("忽略警告")
        self.ignore_warn_btn.setObjectName("secondaryButton")
        self.ignore_warn_btn.setCheckable(True)
        self.ignore_warn_btn.toggled.connect(self._toggle_ignore_warnings)
        info_header.addWidget(self.ignore_warn_btn)
        info_layout.addLayout(info_header)
        self.status_display = QLabel(
            "控制面板与终端属于同一进程。关闭启动终端或关闭本窗口，QQ 服务、对话测试任务、自训练任务和本次启动的 Ollama 都会停止。"
        )
        self.status_display.setWordWrap(True)
        self.status_display.setObjectName("statusDisplay")
        info_layout.addWidget(self.status_display)
        layout.addWidget(info, 1)
        return page

    def _qq_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        panel = QFrame(objectName="panel")
        form = QFormLayout(panel)
        self.qq_status = QLabel("QQ 服务已停止")
        self.qq_status.setObjectName("statusBadge")
        form.addRow("服务状态", self.qq_status)
        form.addRow("WebSocket", QLabel("ws://127.0.0.1:6700/ws"))
        form.addRow("消息发送", QLabel("NapCat WebSocket Action"))
        form.addRow("NapCat HTTP", QLabel("http://127.0.0.1:5700（Token 已配置）"))
        form.addRow("主动发言群", QLabel("1057279304"))
        layout.addWidget(panel)
        buttons = QHBoxLayout()
        start = QPushButton("启动并等待 NapCat")
        start.clicked.connect(self.start_qq)
        stop = QPushButton("停止 QQ 服务", objectName="secondaryButton")
        stop.clicked.connect(self.stop_qq)
        buttons.addWidget(start)
        buttons.addWidget(stop)
        buttons.addStretch()
        layout.addLayout(buttons)
        note = QTextBrowser()
        note.setHtml(
            "<h3>NapCat 接入</h3>"
            "<p>NapCat 应配置为主动 WebSocket 客户端，连接 <code>ws://127.0.0.1:6700/ws</code>，Token 为 <code>Fxr13142</code>。群消息发送使用同一 WebSocket 的 OneBot Action。</p>"
            "<p>引用消息原文通过同一 WebSocket 的 <code>get_msg</code> Action 获取。HTTP 5700 已通过 Token 鉴权，用于无直链语音文件的兼容处理。</p>"
            "<p>图片、语音和视频由 Lucis GPT 模型处理；文本理解和回复由 DeepSeek Flash 处理。</p>"
        )
        layout.addWidget(note, 1)
        return page

    def _chat_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        options = QHBoxLayout()
        self.sender_input = QLineEdit("测试群友")
        self.sender_input.setMaximumWidth(180)
        self.group_input = QLineEdit("1057279304")
        self.group_input.setMaximumWidth(180)
        self.mention_check = QCheckBox("@辉夜")
        self.mention_check.setChecked(True)
        options.addWidget(QLabel("发送者"))
        options.addWidget(self.sender_input)
        options.addWidget(QLabel("群号"))
        options.addWidget(self.group_input)
        options.addWidget(self.mention_check)
        options.addStretch()
        layout.addLayout(options)

        self.chat_view = QTextBrowser()
        self.chat_view.setObjectName("chatView")
        self.chat_view.setHtml("<div class='system'>对话测试已准备。此页面使用与 QQ 接近的输入格式和相同认知流程。</div>")
        layout.addWidget(self.chat_view, 1)
        composer = QHBoxLayout()
        self.chat_input = QPlainTextEdit()
        self.chat_input.setPlaceholderText("输入群聊消息，Ctrl+Enter 发送")
        self.chat_input.setMaximumHeight(92)
        self.send_btn = QPushButton("发送")
        self.send_btn.setMinimumHeight(60)
        self.send_btn.clicked.connect(self.send_chat)
        composer.addWidget(self.chat_input, 1)
        composer.addWidget(self.send_btn)
        layout.addLayout(composer)
        return page

    def _logs_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel(f"本次日志文件：{SESSION_LOG}"))
        toolbar.addStretch()
        clear_all_btn = QPushButton("清空所有日志")
        clear_all_btn.setObjectName("secondaryButton")
        clear_all_btn.setMaximumHeight(36)
        clear_all_btn.clicked.connect(self._clear_all_logs)
        toolbar.addWidget(clear_all_btn)
        layout.addLayout(toolbar)
        sub_tabs = QTabWidget()
        sub_tabs.setDocumentMode(True)
        labels = [("运行日志", LOG_CAT_RUNTIME), ("思考过程", LOG_CAT_THINKING),
                  ("QQbot", LOG_CAT_QQBOT), ("Ollama", LOG_CAT_OLLAMA),
                  ("世界生成", LOG_CAT_WORLD)]
        for label, cat in labels:
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setMaximumBlockCount(0)
            view.setFont(QFont("Monospace", 10))
            self.log_views[cat] = view
            sub_tabs.addTab(view, label)
        layout.addWidget(sub_tabs, 1)
        clear_bar = QHBoxLayout()
        clear_bar.addStretch()
        for label, cat in labels:
            btn = QPushButton(f"清空{label}", objectName="secondaryButton")
            btn.setMaximumHeight(36)
            btn.clicked.connect(lambda _, c=cat: self._clear_log_cat(c))
            clear_bar.addWidget(btn)
        layout.addLayout(clear_bar)
        return page

    def _config_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        panel = QFrame(objectName="panel")
        form = QFormLayout(panel)
        self.ds_url = QLineEdit()
        self.ds_model = QLineEdit()
        self.ds_key = QLineEdit()
        self.ds_key.setEchoMode(QLineEdit.Password)
        self.lucis_url = QLineEdit()
        self.lucis_model = QLineEdit()
        self.lucis_key = QLineEdit()
        self.lucis_key.setEchoMode(QLineEdit.Password)
        form.addRow("DeepSeek URL", self.ds_url)
        form.addRow("DeepSeek 模型", self.ds_model)
        form.addRow("DeepSeek API Key", self.ds_key)
        form.addRow("Lucis URL", self.lucis_url)
        form.addRow("Lucis GPT 模型", self.lucis_model)
        form.addRow("Lucis API Key", self.lucis_key)
        layout.addWidget(panel)
        save = QPushButton("保存 API 配置")
        save.clicked.connect(self.save_config)
        layout.addWidget(save, alignment=Qt.AlignLeft)
        layout.addStretch()
        return page

    def _maintenance_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        panel = QFrame(objectName="panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.addWidget(QLabel("管理员记忆注入", objectName="sectionTitle"))
        panel_layout.addWidget(QLabel("直接写入记忆图并立即持久化。", objectName="muted"))
        self.memory_input = QPlainTextEdit()
        self.memory_input.setPlaceholderText("输入要植入的记忆")
        self.memory_input.setMaximumHeight(120)
        panel_layout.addWidget(self.memory_input)
        inject_btn = QPushButton("注入并保存记忆")
        inject_btn.clicked.connect(self.inject_memory)
        panel_layout.addWidget(inject_btn, alignment=Qt.AlignLeft)
        layout.addWidget(panel)

        train_panel = QFrame(objectName="panel")
        train_layout = QVBoxLayout(train_panel)
        train_layout.addWidget(QLabel("自训练", objectName="sectionTitle"))
        train_layout.addWidget(QLabel("自主循环：世界→感知→决策→语言+动作→位置更新，每轮间隔可调。", objectName="muted"))
        self.train_status = QLabel("自训练：已停止")
        self.train_status.setObjectName("statusBadge")
        train_layout.addWidget(self.train_status)
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("循环间隔（秒）："))
        self.train_interval_spin = QSpinBox()
        self.train_interval_spin.setRange(5, 600)
        self.train_interval_spin.setValue(30)
        self.train_interval_spin.valueChanged.connect(self._update_train_interval)
        interval_row.addWidget(self.train_interval_spin)
        interval_row.addStretch()
        train_layout.addLayout(interval_row)
        btn_row = QHBoxLayout()
        self.train_start_btn = QPushButton("启动自训练")
        self.train_start_btn.clicked.connect(self.start_training)
        self.train_stop_btn = QPushButton("停止自训练", objectName="secondaryButton")
        self.train_stop_btn.clicked.connect(self.stop_training)
        self.train_stop_btn.setEnabled(False)
        btn_row.addWidget(self.train_start_btn)
        btn_row.addWidget(self.train_stop_btn)
        btn_row.addStretch()
        train_layout.addLayout(btn_row)
        layout.addWidget(train_panel)

        layout.addStretch()
        return page

    def _connect_bus(self):
        BUS.log.connect(self.append_log)
        BUS.status.connect(self.update_qq_status)
        BUS.task_error.connect(self.show_error)
        BUS.message.connect(self._on_message)

    def run_worker(self, fn, *args, on_result=None, on_finished=None, **kwargs):
        worker = Worker(fn, *args, **kwargs)
        self.active_workers.add(worker)
        if on_result:
            worker.signals.result.connect(on_result)
        worker.signals.error.connect(self.show_error)
        def finished():
            self.active_workers.discard(worker)
            if on_finished:
                on_finished()

        worker.signals.finished.connect(finished)
        self.pool.start(worker)

    def append_log(self, cat, line):
        view = self.log_views.get(cat)
        if view is not None:
            scrollbar = view.verticalScrollBar()
            position = scrollbar.value()
            view.appendPlainText(line)
            scrollbar.setValue(position)

    def _apply_speed(self):
        from core.virtual_clock import clock
        if clock.qq_mode:
            self._add_warning("QQ 模式下虚拟时间与真实时间同步，无法修改倍速")
            return
        self.run_worker(RUNTIME.set_speed, self.speed_spin.value())

    def set_status(self, text):
        self.status_label.setText(text)

    def update_qq_status(self, text):
        self.qq_status.setText(text)
        self.status_label.setText(text)

    def start_qq(self):
        if not self._check_service_conflict("qq"):
            return
        self._running_service = "qq"
        self._refresh_running_status()
        self.set_status("正在启动 QQ 服务")
        self.run_worker(RUNTIME.start_qq)

    def stop_qq(self):
        self.set_status("正在停止 QQ 服务")
        self.run_worker(RUNTIME.stop_qq, on_finished=self._on_qq_stopped)

    def _on_qq_stopped(self):
        self._running_service = None
        self._refresh_running_status()

    def _on_message(self, sender, content, source):
        self._append_bubble(sender, content, source)

    def _rebuild_chat_view(self):
        from utils.message_history import get_all
        self.chat_view.clear()
        for msg in get_all():
            self._append_bubble(msg["sender"], msg["content"], msg["source"])

    def send_chat(self):
        text = self.chat_input.toPlainText().strip()
        if not text or self.busy_chat:
            return
        if not self._check_service_conflict("chat"):
            return
        self._running_service = "chat"
        self._refresh_running_status()
        sender = self.sender_input.text().strip() or "测试群友"
        group_id = self.group_input.text().strip() or "1057279304"
        mentioned = self.mention_check.isChecked()
        self._append_bubble(sender, text, source="测试")
        self.chat_input.clear()
        self.busy_chat = True
        self.send_btn.setEnabled(False)
        self.send_btn.setText("思考中")
        worker = Worker(RUNTIME.chat, sender, text, group_id, mentioned)
        self.active_workers.add(worker)
        worker.signals.result.connect(lambda result: self._append_bubble("辉夜", result[1], source="测试"))
        worker.signals.error.connect(self.show_error)
        worker.signals.finished.connect(self._chat_finished)
        worker.signals.finished.connect(lambda: self.active_workers.discard(worker))
        self.pool.start(worker)

    def _chat_finished(self):
        self.busy_chat = False
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self._running_service = None
        self._refresh_running_status()

    def _append_bubble(self, name, text, source=""):
        if name == "辉夜":
            bubble_color = "#2d6cdf"
        elif name == "彩叶":
            bubble_color = "#2a5a3a"
        else:
            bubble_color = "#263044"
        safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        source_tag = f"<span style='color:#6a7d96;font-size:10px'>[{source}]</span> " if source else ""
        self.chat_view.append(
            f"<div style='text-align:left;margin:10px 4px'>"
            f"<span style='color:#8fa2bd;font-size:11px'>{source_tag}{name}</span><br>"
            f"<span style='display:inline-block;background:{bubble_color};color:#f4f7fb;padding:8px 12px;border-radius:10px;max-width:85%'>{safe}</span>"
            "</div>"
        )
        self.chat_view.moveCursor(QTextCursor.End)

    def refresh_stats(self):
        try:
            from core.memory_engine import provide_for_monitor
            from core.virtual_clock import clock
            import datetime as dtmod

            mem, links, words = provide_for_monitor()
            self.mem_card.value.setText(str(mem))
            self.link_card.value.setText(str(links))
            self.word_card.value.setText(str(words))

            elapsed = clock.get_real_runtime()
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            self.runtime_card.value.setText(f"{h:02d}:{m:02d}:{s:02d}")

            if clock.qq_mode:
                real_ts = clock.to_real_time(clock.now())
                dt = dtmod.datetime.fromtimestamp(real_ts)
                self.clock_card.value.setText(dt.strftime("%H:%M:%S") + f"  ×{clock.speed:g}")
            else:
                virt_now = clock.now()
                days = int(virt_now // 86400)
                hours = int((virt_now % 86400) // 3600)
                mins = int((virt_now % 3600) // 60)
                self.clock_card.value.setText(f"{days}d {hours:02d}:{mins:02d}  ×{clock.speed:g}")
        except Exception:
            pass

    def _load_config(self):
        path = PROJECT_DIR / "config" / "api_config.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        with path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.ds_url.setText(cfg.get("primary_base_url", ""))
        self.ds_model.setText(cfg.get("primary_model", ""))
        self.ds_key.setText(cfg.get("primary_api_key", ""))
        self.lucis_url.setText(cfg.get("secondary_base_url", ""))
        self.lucis_model.setText(cfg.get("secondary_model", ""))
        self.lucis_key.setText(cfg.get("secondary_api_key", ""))

    def save_config(self):
        path = PROJECT_DIR / "config" / "api_config.json"
        if not path.exists():
            cfg = dict(DEFAULT_CONFIG)
        else:
            with path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
        cfg.update({
            "primary_base_url": self.ds_url.text().strip(),
            "primary_model": self.ds_model.text().strip(),
            "primary_api_key": self.ds_key.text().strip(),
            "secondary_base_url": self.lucis_url.text().strip(),
            "secondary_model": self.lucis_model.text().strip(),
            "secondary_api_key": self.lucis_key.text().strip(),
        })
        with path.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "配置已保存", "配置已写入文件。已初始化的 API 客户端需重启控制面板后生效。")
        logging.info("API 配置已保存，重启后生效")

    def start_training(self):
        if not self._check_service_conflict("training"):
            return
        from self_training import TRAINER
        TRAINER.set_interval(self.train_interval_spin.value())
        try:
            TRAINER.start()
        except Exception as e:
            self._add_error(str(e))
            return
        self._running_service = "training"
        self._refresh_running_status()
        self._update_train_ui(True)

    def stop_training(self):
        from self_training import TRAINER
        try:
            TRAINER.stop()
        except Exception as e:
            self._add_error(str(e))
            return
        self._running_service = None
        self._refresh_running_status()
        self._update_train_ui(False)

    def _update_train_ui(self, running):
        self.train_status.setText("自训练：运行中" if running else "自训练：已停止")
        self.train_start_btn.setEnabled(not running)
        self.train_stop_btn.setEnabled(running)
        self.train_interval_spin.setEnabled(not running)

    def _update_train_interval(self, seconds):
        from self_training import TRAINER
        TRAINER.set_interval(seconds)

    def _add_warning(self, msg):
        self._warnings.append(msg)
        if len(self._warnings) > 10:
            self._warnings = self._warnings[-10:]
        self._refresh_running_status()
        logging.warning(msg)

    def _add_error(self, msg):
        self._errors.append(msg)
        if len(self._errors) > 10:
            self._errors = self._errors[-10:]
        self._refresh_running_status()

    def _refresh_running_status(self):
        lines = []
        if self._running_service:
            names = {"qq": "正在运行QQ服务", "training": "正在运行自训练", "chat": "正在运行对话测试"}
            lines.append(names.get(self._running_service, "正在运行服务"))
        else:
            lines.append("控制面板与终端属于同一进程。关闭启动终端或关闭本窗口，QQ 服务、对话测试任务、自训练任务和本次启动的 Ollama 都会停止。")
        if self._ignore_errors:
            lines.append("[当前已隐藏报错信息]")
        if self._ignore_warnings:
            lines.append("[当前已隐藏警告信息]")
        all_entries = []
        if not self._ignore_errors:
            for e in self._errors:
                all_entries.append(f"[报错] {e}")
        if not self._ignore_warnings:
            for w in self._warnings:
                all_entries.append(f"[警告] {w}")
        max_extra = 4
        if len(all_entries) > max_extra:
            all_entries = all_entries[-max_extra:]
        lines.extend(all_entries)
        self.status_display.setText("\n".join(lines))

    def _toggle_ignore_errors(self, checked):
        self._ignore_errors = checked
        self.ignore_err_btn.setText("显示报错" if checked else "忽略报错")
        self._refresh_running_status()

    def _toggle_ignore_warnings(self, checked):
        self._ignore_warnings = checked
        self.ignore_warn_btn.setText("显示警告" if checked else "忽略警告")
        self._refresh_running_status()

    def _clear_all_logs(self):
        for view in self.log_views.values():
            view.clear()
        self._errors.clear()
        self._warnings.clear()
        self._refresh_running_status()
        logging.info("已清空所有日志")

    def _clear_log_cat(self, cat):
        view = self.log_views.get(cat)
        if view:
            view.clear()
            logging.info("已清空 %s 日志", cat)

    def _check_service_conflict(self, service_name):
        if self._running_service and self._running_service != service_name:
            conflict_map = {
                ("qq", "training"): "正在使用QQ服务，无法启动自训练，请手动停止正在使用的服务",
                ("training", "qq"): "正在使用自训练服务，无法启动QQ服务，请手动停止正在使用的服务",
                ("qq", "chat"): "正在使用QQ服务，无法启动对话测试，请手动停止正在使用的服务",
                ("training", "chat"): "正在使用自训练服务，无法启动对话测试，请手动停止正在使用的服务",
                ("chat", "qq"): "正在使用对话测试，无法启动QQ服务，请手动停止正在使用的服务",
                ("chat", "training"): "正在使用对话测试，无法启动自训练，请手动停止正在使用的服务",
            }
            key = (self._running_service, service_name)
            if key in conflict_map:
                self._add_warning(conflict_map[key])
            return False
        return True

    def inject_memory(self):
        content = self.memory_input.toPlainText().strip()
        if not content:
            return
        self.memory_input.clear()
        self.run_worker(
            RUNTIME.inject_memory,
            content,
            on_result=lambda memory_id: QMessageBox.information(self, "注入成功", f"记忆 ID：{memory_id}"),
        )

    def show_error(self, details):
        logging.error("任务执行失败\n%s", details)
        self._add_error(details.split("\n")[-1] if "\n" in details else details)
        QMessageBox.critical(self, "任务执行失败", "任务发生错误，完整堆栈已写入日志页面。")

    def closeEvent(self, event):
        self.timer.stop()
        event.accept()
        self._shutdown_done = True
        from utils.message_history import flush_to_file
        RUNTIME.shutdown()
        flush_to_file()


STYLE = """
QWidget { background:#0d1118; color:#e7edf6; font-family:'Noto Sans CJK SC','Sans Serif'; font-size:14px; }
QMainWindow, QTabWidget::pane { background:#0d1118; }
QTabWidget::pane { border:1px solid #263247; border-radius:10px; top:-1px; }
QTabBar::tab { background:#121a26; color:#8fa2bd; padding:11px 22px; border:1px solid #263247; }
QTabBar::tab:selected { color:#ffffff; background:#1b2b45; border-bottom:2px solid #4a8cff; }
QLabel#title { font-size:25px; font-weight:800; letter-spacing:2px; color:#f7fbff; }
QLabel#muted { color:#8493aa; }
QLabel#statusBadge { background:#183a31; color:#77e2bd; border:1px solid #286653; padding:6px 13px; border-radius:12px; }
QLabel#sectionTitle { font-size:16px; font-weight:700; }
QLabel#statValue { font-size:26px; font-weight:800; color:#7eb0ff; }
QFrame#statCard, QFrame#panel { background:#141d2a; border:1px solid #263247; border-radius:10px; padding:10px; }
QPushButton { background:#2d6cdf; color:white; border:0; border-radius:7px; padding:10px 18px; font-weight:700; }
QPushButton:hover { background:#3d7bea; }
QPushButton:disabled { background:#344156; color:#8b98aa; }
QPushButton#secondaryButton { background:#222d3d; border:1px solid #35445a; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox { background:#0f1621; border:1px solid #2a3850; border-radius:7px; padding:8px; selection-background-color:#2d6cdf; }
QLabel#statusDisplay { background:#0f1621; border:1px solid #2a3850; border-radius:7px; padding:10px; color:#b8c8e0; font-size:13px; }
QPlainTextEdit#chatView { background:#101722; }
QScrollBar:vertical { background:#101722; width:12px; }
QScrollBar::handle:vertical { background:#34445d; border-radius:5px; min-height:30px; }
QCheckBox { spacing:8px; }
"""


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    file_handler = logging.FileHandler(SESSION_LOG, encoding="utf-8")
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


def main():
    os.chdir(PROJECT_DIR)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = PANEL_LOCK_FILE.open("w", encoding="utf-8")
    if fcntl:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("控制面板已经在运行，请关闭旧的控制面板窗口后再启动。", file=sys.__stderr__)
            return 2
    else:
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print("控制面板已经在运行，请关闭旧的控制面板窗口后再启动。", file=sys.__stderr__)
            return 2
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    configure_logging()
    logging.info("控制面板启动，项目目录=%s", PROJECT_DIR)
    app = QApplication(sys.argv)
    app.setApplicationName("Nascence Huiye Control Panel")
    window = ControlPanel()
    window.show()

    def stop_from_terminal(*_):
        logging.info("收到终端关闭信号")
        window.close()

    signal.signal(signal.SIGINT, stop_from_terminal)
    signal.signal(signal.SIGTERM, stop_from_terminal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop_from_terminal)
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(250)
    code = app.exec_()
    if not getattr(window, '_shutdown_done', False):
        RUNTIME.shutdown()
    logging.shutdown()
    try:
        if fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    except (OSError, PermissionError):
        pass
    lock_file.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
