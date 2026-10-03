# control_panel.py
# ========================================================================
# Nascence_Huiye 桌面控制面板（PyQt5）。
#
# 运行环境（Ollama 启停、QQ 服务线程、伪造发送/思考、落盘、关停）已抽到
# panel_runtime.py，与 Web 面板（web_panel.py）共用；本文件只保留桌面 UI。
# 两个面板互不依赖：Web 面板不需要 PyQt5，桌面面板也不需要 aiohttp 服务。
# ========================================================================

import logging
import os
import signal
import sys
import traceback
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"

# 允许直接运行 control_panel.py 时也使用项目内 Qt/X11 依赖（仅 Linux）。
if sys.platform == "linux":
    LOCAL_LIB = RUN_DIR / "lib" / "usr" / "lib" / "x86_64-linux-gnu"
    if LOCAL_LIB.is_dir():
        import ctypes
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

from config.api_config import config
from config.constants import BOT_NAME
from panel_runtime import (
    LOG_CAT_OLLAMA,
    LOG_CAT_QQBOT,
    LOG_CAT_RUNTIME,
    LOG_CAT_THINKING,
    LOG_VIEW_MAX_BLOCKS,
    RUNTIME,
    SESSION_LOG,
    acquire_panel_lock,
    configure_logging,
    load_panel_config,
    release_panel_lock,
    save_panel_config,
)
from utils.event_bus import BUS

CHAT_VIEW_MAX_BLOCKS = 300        # 对话气泡上限


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


class StatCard(QFrame):
    def __init__(self, title, value="--", caption=""):
        super().__init__()
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("muted")
        self.value = QLabel(value)
        self.value.setObjectName("statValue")
        # 副标题：数值以外的补充信息（字号小，避免撑破卡片）
        self.caption = QLabel(caption)
        self.caption.setObjectName("statCaption")
        self.caption.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)


class ControlPanel(QMainWindow):
    def __init__(self):
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self.active_workers = set()
        self.setWindowTitle(f"Nascence {BOT_NAME} · 控制面板")
        self.resize(1280, 860)
        self.setMinimumSize(1000, 700)
        self.log_views = {}
        self._shutdown_done = False
        self._running_service = None  # 目前仅 "qq" 一种可启动服务
        self._warnings = []
        self._errors = []
        self._ignore_errors = False
        self._ignore_warnings = False
        self._build_ui()
        self._connect_bus()
        if SESSION_LOG.exists():
            text = SESSION_LOG.read_text(encoding="utf-8-sig")
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
        title = QLabel(f"NASCENCE · {BOT_NAME}")
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
        self.energy_card = StatCard("精力")
        for card in (self.mem_card, self.link_card, self.word_card, self.runtime_card, self.clock_card, self.energy_card):
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
        save_btn.clicked.connect(lambda: self.run_worker(RUNTIME.save, force=True))
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
        tools.addWidget(self.speed_spin)
        tools.addWidget(self.set_speed_btn)
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
            "控制面板与终端属于同一进程。关闭启动终端或关闭本窗口，QQ 服务与本次启动的 Ollama 都会停止。"
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
        self.qq_bot_label = QLabel(str(config.get("bot_qq") or "123456"))
        self.qq_group_label = QLabel(str(config.get("active_group_id") or "123456"))
        form.addRow("机器人QQ号", self.qq_bot_label)
        form.addRow("主动发言群", self.qq_group_label)
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
        self.qq_note = QTextBrowser()
        self._refresh_napcat_note()
        layout.addWidget(self.qq_note, 1)
        return page

    def _refresh_napcat_note(self):
        """按当前 config 刷新 NapCat 接入说明与 QQ 页标签。"""
        if hasattr(self, "qq_bot_label"):
            self.qq_bot_label.setText(str(config.get("bot_qq") or "123456"))
        if hasattr(self, "qq_group_label"):
            self.qq_group_label.setText(str(config.get("active_group_id") or "123456"))
        token = str(config.get("napcat_token") or "Nascence")
        primary_model = str(config.get("primary_model") or "deepseek-v4-flash")
        secondary_model = str(config.get("secondary_model") or "gpt-5.6-sol")
        self.qq_note.setHtml(
            "<h3>NapCat 接入</h3>"
            f"<p>NapCat 应配置为主动 WebSocket 客户端，连接 <code>ws://127.0.0.1:6700/ws</code>，Token 为 <code>{token}</code>。群消息发送使用同一 WebSocket 的 OneBot Action。</p>"
            "<p>引用消息原文通过同一 WebSocket 的 <code>get_msg</code> Action 获取。HTTP 5700 已通过 Token 鉴权，用于无直链语音文件的兼容处理。</p>"
            f"<p>图片、语音和视频由 <code>{secondary_model}</code> 处理；文本理解和回复由 <code>{primary_model}</code> 处理。</p>"
        )

    # _chat_page（对话测试页）已移除：
    # 该页面依赖 core.cognition.process_dialogue，而对话测试在 QQ 接入形态下
    # 已无实际用途（虚拟时钟加速与离线对话功能一并放弃）。CLI（main.py）保留
    # 作为架构参考与本机调试入口，其静默语义由 generate_response 负责。

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
                  ("QQbot", LOG_CAT_QQBOT), ("Ollama", LOG_CAT_OLLAMA)]
        for label, cat in labels:
            view = QPlainTextEdit()
            view.setReadOnly(True)
            # 显示上限：超出后自动丢弃最旧的块，防止长时间运行内存与重绘开销累积
            view.setMaximumBlockCount(LOG_VIEW_MAX_BLOCKS)
            view.setUndoRedoEnabled(False)
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

        napcat_panel = QFrame(objectName="panel")
        napcat_form = QFormLayout(napcat_panel)
        self.bot_qq_input = QLineEdit()
        self.active_group_input = QLineEdit()
        self.napcat_token_input = QLineEdit()
        napcat_form.addRow("机器人QQ号", self.bot_qq_input)
        napcat_form.addRow("主动发言目标群号", self.active_group_input)
        napcat_form.addRow("NapCat 鉴权 Token", self.napcat_token_input)
        layout.addWidget(napcat_panel)
        save_napcat = QPushButton("保存 NapCat 设置")
        save_napcat.clicked.connect(self.save_napcat_config)
        layout.addWidget(save_napcat, alignment=Qt.AlignLeft)
        layout.addStretch()
        return page

    def _maintenance_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        fake_panel = QFrame(objectName="panel")
        fake_layout = QVBoxLayout(fake_panel)
        fake_layout.addWidget(QLabel("伪造发送", objectName="sectionTitle"))
        fake_layout.addWidget(QLabel("以 bot 身份向当前群发送消息，并写入历史对话与记忆库。", objectName="muted"))
        self.fake_input = QPlainTextEdit()
        self.fake_input.setPlaceholderText("输入要以 bot 身份发送的内容")
        self.fake_input.setMaximumHeight(100)
        fake_layout.addWidget(self.fake_input)
        fake_btn = self.fake_btn = QPushButton("伪造发送")
        fake_btn.clicked.connect(self.fake_send)
        fake_layout.addWidget(fake_btn, alignment=Qt.AlignLeft)
        layout.addWidget(fake_panel)

        think_panel = QFrame(objectName="panel")
        think_layout = QVBoxLayout(think_panel)
        think_layout.addWidget(QLabel("伪造思考", objectName="sectionTitle"))
        think_layout.addWidget(QLabel("模拟 bot 的内心思考流程（LLM 拆解→检索→扩散→拼接），不向 QQ 发送消息，仅写入历史与记忆库。", objectName="muted"))
        self.fake_think_input = QPlainTextEdit()
        self.fake_think_input.setPlaceholderText("输入要让 bot 思考的内容")
        self.fake_think_input.setMaximumHeight(100)
        think_layout.addWidget(self.fake_think_input)
        think_btn = self.think_btn = QPushButton("伪造思考")
        think_btn.clicked.connect(self.fake_think)
        think_layout.addWidget(think_btn, alignment=Qt.AlignLeft)
        layout.addWidget(think_panel)

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

        # 自训练面板已移除（F01/F14/F26）：自训练链路整体删除，
        # 项目定位为 QQ 接入的长期对话基座，不再提供离线自主训练循环。

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

    def _trim_view(self, view, max_blocks):
        """兜底裁剪：超限时从头部移除多余内容（QTextBrowser 无块数上限，需手动截）。"""
        try:
            if view.document().blockCount() <= max_blocks:
                return
            cursor = view.textCursor()
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(
                QTextCursor.NextBlock,
                QTextCursor.KeepAnchor,
                view.document().blockCount() - max_blocks,
            )
            cursor.removeSelectedText()
        except Exception:
            pass

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

    def _append_bubble(self, name, text, source=""):
        if not hasattr(self, "chat_view"):
            return
        if name == BOT_NAME:
            bubble_color = "#2d6cdf"
        else:
            bubble_color = "#263044"
        # 单条过长时截断显示，避免超长文本把整个对话页撑爆
        shown = str(text)
        if len(shown) > 500:
            shown = shown[:500] + "……（已截断显示）"
        safe = shown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        source_tag = f"<span style='color:#6a7d96;font-size:10px'>[{source}]</span> " if source else ""
        self.chat_view.append(
            f"<div style='text-align:left;margin:10px 4px'>"
            f"<span style='color:#8fa2bd;font-size:11px'>{source_tag}{name}</span><br>"
            f"<span style='display:inline-block;background:{bubble_color};color:#f4f7fb;padding:8px 12px;border-radius:10px;max-width:85%'>{safe}</span>"
            "</div>"
        )
        self._trim_view(self.chat_view, CHAT_VIEW_MAX_BLOCKS)
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

            from core.biorhythm import BIORHYTHM
            snap = BIORHYTHM.snapshot()
            label = "睡眠中" if snap["state"] == "asleep" else "清醒"
            # 数值行只放短信息，作息等细节放副标题（大字行放不下长文本）
            self.energy_card.value.setText(f"{int(snap['energy']*100)}% {label}")
            nights = snap.get("rhythm_nights", 0)
            if nights > 0:
                hours = BIORHYTHM.rhythm_hours()
                # 文案保持简短：卡片宽度有限（6 张并排），长了会换行挤压
                span = f" 常睡{min(hours)}-{max(hours)}点" if hours else ""
                self.energy_card.caption.setText(
                    f"作息{snap.get('circadian', 0):.2f} {nights}晚{span}"
                )
            else:
                self.energy_card.caption.setText("作息积累中")
        except Exception:
            pass

    def _load_config(self):
        cfg = load_panel_config()
        self.ds_url.setText(cfg.get("primary_base_url", ""))
        self.ds_model.setText(cfg.get("primary_model", ""))
        self.ds_key.setText(cfg.get("primary_api_key", ""))
        self.lucis_url.setText(cfg.get("secondary_base_url", ""))
        self.lucis_model.setText(cfg.get("secondary_model", ""))
        self.lucis_key.setText(cfg.get("secondary_api_key", ""))
        self.bot_qq_input.setText(str(cfg.get("bot_qq") or "123456"))
        self.active_group_input.setText(str(cfg.get("active_group_id") or "123456"))
        self.napcat_token_input.setText(str(cfg.get("napcat_token") or "Nascence"))

    def save_config(self):
        cfg = load_panel_config()
        cfg.update({
            "primary_base_url": self.ds_url.text().strip(),
            "primary_model": self.ds_model.text().strip(),
            "primary_api_key": self.ds_key.text().strip(),
            "secondary_base_url": self.lucis_url.text().strip(),
            "secondary_model": self.lucis_model.text().strip(),
            "secondary_api_key": self.lucis_key.text().strip(),
        })
        save_panel_config(cfg)
        QMessageBox.information(self, "配置已保存", "配置已写入文件。已初始化的 API 客户端需重启控制面板后生效。")
        logging.info("API 配置已保存，重启后生效")

    def save_napcat_config(self):
        cfg = load_panel_config()
        cfg.update({
            "bot_qq": self.bot_qq_input.text().strip() or "123456",
            "active_group_id": self.active_group_input.text().strip() or "123456",
            "napcat_token": self.napcat_token_input.text().strip() or "Nascence",
        })
        save_panel_config(cfg)
        # 热刷新：重读磁盘配置到全局 config，运行中的 QQ 服务立即感知新值（无需重启面板）
        from config.api_config import reload_config
        reload_config()
        try:
            import qq_bot
            qq_bot.BOT_QQ = qq_bot.get_bot_qq()
            qq_bot.ACTIVE_GROUP_ID = qq_bot.get_active_group_id()
            qq_bot.HTTP_ACCESS_TOKEN = qq_bot.get_napcat_token()
            qq_bot.WS_ACCESS_TOKEN = qq_bot.get_napcat_token()
        except Exception:
            pass
        # 刷新控制面板显示（QQ 页标签、接入说明、主动发言目标群号）
        self._refresh_napcat_note()
        if hasattr(self, "bot_qq_input"):
            self.bot_qq_input.setText(str(config.get("bot_qq") or "123456"))
        if hasattr(self, "active_group_input"):
            self.active_group_input.setText(str(config.get("active_group_id") or "123456"))
        if hasattr(self, "napcat_token_input"):
            self.napcat_token_input.setText(str(config.get("napcat_token") or "Nascence"))
        QMessageBox.information(self, "已保存", "NapCat 设置已保存，运行中的 QQ 服务将自动使用新值。")
        logging.info("NapCat 配置已保存并热刷新")

    # start_training / stop_training / _update_train_ui / _update_train_interval
    # 已随自训练链路一并移除（F01/F14/F26）。

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
            names = {"qq": "正在运行QQ服务"}
            lines.append(names.get(self._running_service, "正在运行服务"))
        else:
            lines.append("控制面板与终端属于同一进程。关闭启动终端或关闭本窗口，QQ 服务与本次启动的 Ollama 都会停止。")
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
        """服务互斥检查。

        自训练与对话测试已移除，现在只剩 QQ 服务一种可启动的服务，
        因此这里只需判断"已有服务在跑且不是同一种"即可。
        """
        if self._running_service and self._running_service != service_name:
            self._add_warning(
                f"正在运行{self._running_service}服务，无法启动{service_name}，"
                f"请先停止正在使用的服务"
            )
            return False
        return True

    def fake_send(self):
        text = self.fake_input.toPlainText().strip()
        if not text:
            return
        self.fake_input.clear()
        self.fake_btn.setEnabled(False)
        self.run_worker(
            RUNTIME.fake_send,
            text,
            on_result=lambda memory_id: self._append_bubble(BOT_NAME, text, source="伪造"),
            on_finished=lambda: self.fake_btn.setEnabled(True),
        )

    def fake_think(self):
        text = self.fake_think_input.toPlainText().strip()
        if not text:
            return
        self.fake_think_input.clear()
        self.think_btn.setEnabled(False)
        self.run_worker(
            RUNTIME.fake_think,
            text,
            on_result=lambda reply: self._append_bubble(BOT_NAME, f"（内心）{reply}", source="伪造思考"),
            on_finished=lambda: self.think_btn.setEnabled(True),
        )

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
        # 关停包含停服务、落盘、关 Ollama 等同步阻塞步骤（最坏可达十余秒）。
        # 若直接在 GUI 线程里跑，窗口会失去响应、被系统标记为"未响应"，
        # 因此改为：先在后台线程完成关停，结束后再真正关闭窗口。
        if not getattr(self, "_shutdown_started", False):
            self._shutdown_started = True
            event.ignore()
            self.timer.stop()
            self.set_status("正在停止服务并保存数据…")
            self.status_display.setText("正在停止服务并保存数据，请稍候…\n窗口将在完成后自动关闭。")
            self.setEnabled(False)
            self.run_worker(self._shutdown_background, on_finished=self._finish_close)
            return
        event.accept()

    def _shutdown_background(self):
        """在后台线程执行的关停流程（不触碰任何 Qt 控件）。"""
        try:
            RUNTIME.shutdown()
        except Exception:
            logging.exception("关停过程中出现异常")
        # 必须先于窗口真正关闭完成，避免 main() 重复执行关停
        self._shutdown_done = True

    def _finish_close(self):
        """关停完成后回到 GUI 线程关闭窗口。"""
        self.timer.stop()
        self.close()


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
QLabel#statCaption { font-size:11px; color:#8493aa; }
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
"""


def main():
    os.chdir(PROJECT_DIR)
    os.makedirs(RUN_DIR, exist_ok=True)
    lock_file = acquire_panel_lock()
    if lock_file is None:
        print("控制面板已经在运行，请关闭旧的面板（桌面或 Web）后再启动。", file=sys.__stderr__)
        return 2
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
    release_panel_lock(lock_file)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
