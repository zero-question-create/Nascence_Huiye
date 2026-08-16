# webui.py
# ========================================================================
# Nascence 辉夜 Web 控制面板（替代原 PyQt5 控制面板）
#
# 本文件提供完整的 Web 服务：
#   - 静态前端（webui/ 目录下的 index.html / style.css / app.js）
#   - REST API（/api/*）：启动/停止 QQ、对话测试、伪造发送/思考、记忆注入、
#                          模型开关切换、配置读写、自训练控制等
#   - WebSocket（/ws）：实时推送日志、状态、对话消息到浏览器
#   - 三个模型后端（text/embed/multimodal）由 core.model_backend 托管，
#     启动时按开关加载 llama.cpp 本地模型，运行中可在「模型」页切换
#
# 运行方式：
#   python webui.py                 # 启动 Web 面板（默认 127.0.0.1:8787）
#   python webui.py --port 9000     # 自定义端口
#
# 浏览器访问 http://127.0.0.1:8787
# ========================================================================

import asyncio
import hashlib
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from aiohttp import web

from config.api_config import config, DEFAULT_CONFIG, reload_config, save_config
from config.constants import BOT_NAME
from utils.event_bus import BUS
from core import runtime_paths
from core.user_worker import UserWorkerPool

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
WEBUI_DIR = PROJECT_DIR / "webui"
GATEWAY_DIR = PROJECT_DIR / "gateway"
CLIENT_DIR = PROJECT_DIR / "client"  # 客户端独立存放（可脱离服务端单独打开）
USERS_FILE = GATEWAY_DIR / "users.json"
SESSIONS_FILE = GATEWAY_DIR / "sessions.json"

USER_WORKERS = UserWorkerPool(str(PROJECT_DIR))
GLOBAL_COGNITION_ENABLED = False

# 默认端口（可通过 --port 覆盖；或读配置中保存的 server_port）
DEFAULT_WEB_PORT = 8787
# 网页版客户端独立端口（随服务器运行；读配置中保存的 client_port）
DEFAULT_CLIENT_PORT = 8987

# 运行中实时切换监听端口所需的状态（复用同一 AppRunner，停旧 site 起新 site）
_RUNNER = None          # 当前 AppRunner 引用（_serve 内赋值）
_SITES = []             # 当前活跃的 TCPSite 列表
_CURRENT_PORT = None    # 当前监听端口

# 网页版客户端服务状态（独立端口，随服务器启动）
_CLIENT_RUNNER = None
_CLIENT_SITES = []
_CLIENT_PORT = None

# 日志分类（与前端 tab 对应）
LOG_CAT_RUNTIME = "runtime"
LOG_CAT_THINKING = "thinking"
LOG_CAT_MODEL = "model"
LOG_CAT_CONNECTION = "connection"  # 连接类：Web 轮询(ping) 等

# 历史日志环形缓冲（供 WebSocket 连接建立时补发）
# 各分类日志行数上限（统一 2000）
_MAX_LOG_LINES = 2000
_log_buffer = {cat: [] for cat in (LOG_CAT_RUNTIME, LOG_CAT_THINKING, LOG_CAT_MODEL, LOG_CAT_CONNECTION)}


# ------------------------------------------------------------------------
# 日志处理
# ------------------------------------------------------------------------
class SignalLogHandler(logging.Handler):
    """把日志写入环形缓冲，并广播给所有 WebSocket 客户端。"""

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self._clients = set()      # 在线 WebSocket
        self._clients_lock = threading.Lock()

    def add_client(self, ws):
        with self._clients_lock:
            self._clients.add(ws)

    def remove_client(self, ws):
        with self._clients_lock:
            self._clients.discard(ws)

    def emit(self, record):
        try:
            cat = self._categorize(record)
            line = self.format(record)
            # 写入环形缓冲（线程安全由 GIL 保证），统一限制行数
            buf = _log_buffer.setdefault(cat, [])
            buf.append(line)
            if len(buf) > _MAX_LOG_LINES:
                del buf[:len(buf) - _MAX_LOG_LINES]
            # 入队广播（不直接碰 asyncio，避免跨线程事件循环问题）
            if _ws_broadcast_queue is not None:
                _ws_broadcast_queue.put_nowait((cat, line))
        except Exception:
            pass

    @staticmethod
    def _categorize(record):
        name = record.name
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)

        # 1. 连接类日志（含心跳、Web 轮询、WebSocket 连接等） → 「连接」页
        if (
            name == "aiohttp.access"
            or name.startswith("UserWorker.Connection")
            or "[心跳]" in msg
            or "用户子进程正在运行" in msg
            or any(k in msg for k in ("WebSocket", "连接", "发送失败", "连接已断开", "[心跳]"))
        ):
            return LOG_CAT_CONNECTION

        # 2. 思考类日志（含认知循环、思考、意图拆解、用户对话处理等） → 「思考」页
        if (
            name in ("Nascence.Processing", "CognitionRunner", "CognitiveLoop", "UserWorker.Thinking")
            or any(k in msg for k in (
                "[用户对话]", "[认知循环]", "[主动发言]", "[思考]", "[认知]",
                "[内心独白]", "[意图拆解]", "[联想扩散]", "[思维回想]", "思考", "thinking"
            ))
        ):
            return LOG_CAT_THINKING

        # 3. 模型加载/推理相关日志 → 「模型」页
        if name == "ModelBackend":
            return LOG_CAT_MODEL
        model_keywords = (
            "[ModelBackend]", "[embed]", "[text]", "[multimodal]", "[模型]",
            "正在启动语义模型", "语义模型启动完成", "llama-server", "llama.cpp",
            "模型", "GGUF", "mmproj", "embedding"
        )
        if any(k in msg for k in model_keywords):
            return LOG_CAT_MODEL

        # 4. 其他常规系统日志 → 「运行」页
        return LOG_CAT_RUNTIME


# 全局日志处理器（web 服务器运行时由 configure_logging 注册）
# 使用 asyncio.Queue 桥接：日志可能来自任意线程，统一入队，
# 由主事件循环中的 task 消费并推送给 WebSocket 客户端，避免跨线程直接调用 asyncio API。
signal_handler = SignalLogHandler()
_ws_broadcast_queue = None      # asyncio.Queue；由 start() 初始化
_ws_broadcast_task = None       # 消费队列的后台 task
_main_loop = None               # 主事件循环引用


# ------------------------------------------------------------------------
# 运行环境（Runtime）
# ------------------------------------------------------------------------
class Runtime:
    """核心业务逻辑（与原控制面板一致，去掉 PyQt 依赖）。"""

    def __init__(self):
        self.initialized = False
        self._shutdown_done = False
        self._lock = threading.RLock()  # 可重入：initialize 与认知控制会嵌套加锁
        self._chat_lock = threading.Lock()  # 串行化对话处理：允许用户随时发言，但保证回复按序返回、记忆状态一致

    # ---------------- 初始化 ----------------
    def initialize(self):
        with self._lock:
            if self.initialized:
                return
            logging.info("开始初始化 Nascence 运行环境")
            # 1. 启动本地模型后端（按开关加载三个默认模型）
            from core.model_backend import start_default_backends, check_models_ready
            start_default_backends()
            # 2. 门槛检查：至少每种能力有本地模型或外部 API，否则明确报错阻止启动
            check_models_ready()
            # 3. 预热 embedding + 加载记忆
            from core.memory_engine import get_model, memories, _init_metrics_counters
            from core.virtual_clock import clock
            from main import cold_start_batch_injection
            from utils.persistence import load_all_data, load_state, save_all_data, save_state
            from core.llm_interface import load_dialogue_history

            clock.enable_qq_mode()
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

    # ---------------- 认知循环控制 ----------------
    def start_cognition(self):
        """启动永续认知循环（独立常驻）。"""
        self.initialize()
        from core.cognition_runner import COGNITION
        COGNITION.start()
        BUS.emit_status("认知循环运行中")
        logging.info("认知循环已启动")

    def stop_cognition(self):
        """优雅停止认知循环（当前轮完成后退出，不影响主程序/面板）。"""
        from core.cognition_runner import COGNITION
        COGNITION.stop()
        BUS.emit_status("认知循环已停止")
        logging.info("认知循环停止信号已发送")

    def cognition_status(self) -> dict:
        """返回认知循环当前状态（供 WebUI 展示）。"""
        from core.cognition_runner import COGNITION
        return {"running": COGNITION.is_running()}

    # ---------------- 对话测试 ----------------
    def chat(self, sender, text, mentioned):
        self.initialize()
        from core.cognition import process_dialogue
        from core.llm_interface import add_to_history
        from utils.persistence import append_dialogue, save_all_data, save_state

        augmented = (
            f'{sender}对{BOT_NAME}说：“{text}”'
            if mentioned
            else f'{sender}说：“{text}”'
        )
        logging.info("[对话测试] 发送者=%s @%s=%s 输入=%s", sender, BOT_NAME, mentioned, augmented)
        # 串行化对话处理：并发请求排队执行，保证回复顺序与记忆写入一致。
        # 用户可随时继续发言（前端不阻塞输入），这里只是让多条消息逐条处理。
        with self._chat_lock:
            reply, _ = asyncio.run(process_dialogue(augmented_input=augmented))
            reply = str(reply or "静默")
            if mentioned and reply.strip() in ("", "静默", "静默。", "[SILENT]"):
                reply = "嗯......"
            add_to_history(sender, text, reply, "测试")
            append_dialogue(augmented, reply)
            save_all_data()
            save_state()
            logging.info("[对话测试] %s回复=%s", BOT_NAME, reply)
        return sender, reply

    # ---------------- 伪造思考 ----------------
    def fake_think(self, text):
        self.initialize()
        from core.cognition import generate_response
        from utils.message_history import add_message
        from utils.persistence import save_all_data, save_state

        augmented = f"我（{BOT_NAME}）自己想着：{text}"
        reply, _ = generate_response(augmented)
        reply = str(reply or "（静默）").strip()
        memory_text = f"我想：{reply}"
        add_message(BOT_NAME, memory_text, "伪造思考")
        save_all_data()
        save_state()
        logging.info("伪造思考完成：输入=%s 想法=%s", text, reply)
        return reply

    # ---------------- 记忆注入 ----------------
    def inject_memory(self, content):
        self.initialize()
        from core.memory_engine import create_memory
        from utils.persistence import save_all_data

        memory_id = create_memory(content)
        save_all_data()
        logging.info("管理员注入记忆成功，ID=%s，内容=%s", memory_id, content)
        return memory_id

    # ---------------- 保存 / 关闭 ----------------
    def save(self):
        if not self.initialized:
            return
        from utils.persistence import save_all_data, save_state
        save_all_data()
        save_state()
        logging.info("记忆和对话状态已完整保存")

    def shutdown(self):
        """完整关停：停止认知循环 → 最终保存 → 停止模型后端。

        幂等：可被按钮退出、信号、atexit、_serve finally 多路径重复调用，
        内部通过 _shutdown_done 保证只执行一次完整关停。
        """
        with self._lock:
            if getattr(self, "_shutdown_done", False):
                return
            self._shutdown_done = True
        logging.info("控制面板正在停止全部服务")
        try:
            from core.cognition_runner import COGNITION
            COGNITION.shutdown()
        except Exception:
            logging.exception("停止认知循环失败")
        try:
            self.save()
        except Exception:
            logging.exception("退出保存失败")
        try:
            from core.model_backend import stop_all_backends
            stop_all_backends()
        except Exception:
            logging.exception("停止模型后端失败")
        logging.info("全部服务已停止")


RUNTIME = Runtime()


# ------------------------------------------------------------------------
# API 辅助：模型开关切换
# ------------------------------------------------------------------------
def _switch_backend(name: str, use_default: bool, api_base_url: str, api_key: str, api_model: str) -> dict:
    """切换指定后端的模式（默认模型 / 外部 API），返回 (ok, needs_restart, message)。

    说明：
      - 切到 API：本地模型保持运行不关闭，仅路由变更（可实时生效）
      - 切到默认：若本地模型本次已加载 → 立即生效；否则提示重启
    """
    from core.model_backend import BACKENDS
    backend = BACKENDS.get(name)
    if backend is None:
        return {"ok": False, "needs_restart": False, "message": f"未知后端: {name}"}

    # 先更新配置（写回磁盘 + 热刷新全局 config）
    cfg = config.get("backends", {})
    cfg.setdefault(name, {})
    cfg[name]["use_default"] = bool(use_default)
    cfg[name]["api_base_url"] = (api_base_url or "").strip()
    # 前端不回显 API Key；当提交的 key 为空时保留原有 key，避免误清
    submitted_key = (api_key or "").strip()
    if submitted_key:
        cfg[name]["api_key"] = submitted_key
    cfg[name]["api_model"] = (api_model or "").strip()
    save_config(config)
    reload_config()

    # 执行切换语义判断
    ok, needs_restart, message = backend.switch_use_default(bool(use_default))
    if not ok:
        # 配置不合法：回滚 use_default（保留用户填的 API）
        cfg[name]["use_default"] = not bool(use_default)
        save_config(config)
        reload_config()
    logging.info("[模型] %s 切换 use_default=%s -> %s", name, use_default, message)
    return {"ok": ok, "needs_restart": needs_restart, "message": message}


# ------------------------------------------------------------------------
# WebSocket 消息推送（消息 / 状态）
# ------------------------------------------------------------------------
_ws_clients = set()
_ws_clients_lock = threading.Lock()


def _broadcast(payload: dict):
    """把消息入队，由主事件循环统一推送给所有 WebSocket 客户端。"""
    if _ws_broadcast_queue is not None:
        try:
            _ws_broadcast_queue.put_nowait(payload)
        except Exception:
            pass


def _bus_forwarder(topic, *args):
    """把事件总线消息转发为 WebSocket 消息。"""
    if topic == "message":
        sender, content, source = args
        _broadcast({"type": "message", "sender": sender, "content": content, "source": source})
    elif topic == "status":
        _broadcast({"type": "status", "text": args[0]})


async def _ws_broadcast_worker():
    """消费广播队列并把消息推给所有在线 WebSocket 客户端（在主事件循环内运行）。"""
    global _ws_broadcast_queue, _ws_broadcast_task
    while True:
        try:
            item = await _ws_broadcast_queue.get()
            # 两种消息：tuple(cat, line) 表示日志；dict 表示结构消息
            if isinstance(item, tuple):
                cat, line = item
                payload = {"type": "log", "cat": cat, "line": line}
            else:
                payload = item
            text = json.dumps(payload, ensure_ascii=False)
            with _ws_clients_lock:
                clients = list(_ws_clients)
            for ws in clients:
                if not ws.closed:
                    try:
                        await ws.send_str(text)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            break
        except Exception:
            continue


# ------------------------------------------------------------------------
# 用户鉴权与会话管理（PBKDF2 加盐哈希）
# ------------------------------------------------------------------------
def _load_users() -> dict:
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_users(users: dict):
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()


def _sanitize_username(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum() or ch in "_-")


def register_user(username: str, password: str) -> dict:
    username = _sanitize_username(username)
    if not (2 <= len(username) <= 32):
        return {"ok": False, "message": "用户名需为 2-32 位字母/数字/下划线。"}
    if len(password) < 4:
        return {"ok": False, "message": "密码至少 4 位。"}
    users = _load_users()
    if username in users:
        return {"ok": False, "message": "用户名已存在。"}
    salt = secrets.token_hex(16)
    users[username] = {"salt": salt, "hash": _hash_password(password, salt)}
    _save_users(users)
    os.makedirs(f"data/{username}", exist_ok=True)
    logging.info("[注册] 新用户 %s 已创建，数据目录 data/%s/", username, username)
    return {"ok": True, "username": username}


def verify_user(username: str, password: str) -> bool:
    username = _sanitize_username(username)
    users = _load_users()
    rec = users.get(username)
    if not rec:
        return False
    return _hash_password(password, rec["salt"]) == rec["hash"]


SESSION_TTL_SECONDS = 86400  # 登录状态在服务端保留 1 天


def _load_sessions(cleanup: bool = False) -> dict:
    sessions = {}
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    sessions = raw
        except (json.JSONDecodeError, OSError):
            sessions = {}

    if cleanup:
        now = time.time()
        valid = {}
        changed = False
        for token, val in sessions.items():
            if isinstance(val, dict):
                exp = val.get("expires_at", 0)
                if exp > now:
                    valid[token] = val
                else:
                    changed = True
            elif isinstance(val, str):
                # 兼容旧格式，赋予 1 天有效期
                valid[token] = {
                    "username": val,
                    "created_at": now,
                    "expires_at": now + SESSION_TTL_SECONDS
                }
                changed = True
        if changed:
            _save_sessions(valid)
        return valid
    return sessions


def _save_sessions(sessions: dict):
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)


def create_session(username: str) -> str:
    token = secrets.token_hex(24)
    now = time.time()
    sessions = _load_sessions(cleanup=True)
    sessions[token] = {
        "username": username,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS
    }
    _save_sessions(sessions)
    return token


def resolve_session(token: str):
    if not token:
        return None
    sessions = _load_sessions(cleanup=True)
    val = sessions.get(token)
    if not val:
        return None
    if isinstance(val, dict):
        if time.time() > val.get("expires_at", 0):
            sessions.pop(token, None)
            _save_sessions(sessions)
            return None
        return val.get("username")
    if isinstance(val, str):
        return val
    return None


def get_token_from_request(request, data=None):
    token = None
    if data and isinstance(data, dict):
        token = data.get("token")
    if not token:
        token = request.query.get("token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    return token or ""


# ------------------------------------------------------------------------
# 按用户切换数据目录并加载其记忆沙箱
# ------------------------------------------------------------------------
_current_loaded_user = None


def load_user_context(username: str, force: bool = False):
    """切换数据目录到 data/<username>/，重置并加载该用户独立数据。"""
    global _current_loaded_user
    if not force and _current_loaded_user == username:
        from core.memory_engine import memories
        return memories

    user_dir = f"data/{username}"
    os.makedirs(user_dir, exist_ok=True)
    runtime_paths.set_data_dir(user_dir)

    from core.memory_engine import reset_engine_state, memories, _init_metrics_counters
    from utils.persistence import load_all_data, load_state
    from utils.dialogue_state import reset_state
    from utils import message_history
    from core.llm_interface import load_dialogue_history

    reset_engine_state()
    reset_state()
    message_history._message_history.clear()
    message_history._flushed_count = 0
    load_all_data()
    load_state()
    load_dialogue_history()
    _init_metrics_counters()
    _current_loaded_user = username
    return memories


# ------------------------------------------------------------------------
# 用户网关 HTTP 处理器（/gateway/* 与客户端静态资源）
# ------------------------------------------------------------------------
async def client_index_handler(request):
    """用户客户端主入口。"""
    return web.FileResponse(CLIENT_DIR / "index.html")


async def client_config_handler(request):
    """客户端配置文件。"""
    return web.FileResponse(CLIENT_DIR / "client_config.json")


async def client_asset_handler(request):
    """客户端静态资源（index.html 使用相对路径，服务端下发模式需按名映射到 client/）。"""
    name = request.match_info["name"]
    # 按请求路径前缀确定子目录（/nav/* → client/nav/，/images/* → client/images/）
    prefix = ""
    if request.path.startswith("/nav/"):
        prefix = "nav"
    elif request.path.startswith("/images/"):
        prefix = "images"
    target = (CLIENT_DIR / prefix / name).resolve() if prefix else (CLIENT_DIR / name).resolve()
    # 防目录穿越：仅允许 client/ 内的文件
    try:
        target.relative_to(CLIENT_DIR.resolve())
    except ValueError:
        return web.Response(status=404, text="Not Found")
    if target.is_file():
        return web.FileResponse(target)
    return web.Response(status=404, text="Not Found")


async def admin_index_handler(request):
    """管理员控制面板主入口。"""
    return web.FileResponse(WEBUI_DIR / "index.html")


async def gateway_api_register(request):
    data = await request.json()
    result = register_user(data.get("username", ""), data.get("password", ""))
    if result["ok"]:
        token = create_session(result["username"])
        result["token"] = token
    return web.json_response(result)


async def gateway_api_login(request):
    data = await request.json()
    username = _sanitize_username(data.get("username", ""))
    password = data.get("password", "")
    if verify_user(username, password):
        token = create_session(username)
        logging.info("[登录] 用户 %s 登录成功", username)
        return web.json_response({"ok": True, "username": username, "token": token})
    return web.json_response({"ok": False, "message": "用户名或密码错误。"}, status=401)


async def gateway_api_bot_info(request):
    return web.json_response({"ok": True, "bot_name": BOT_NAME})


async def gateway_api_history(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)

    def _get():
        # 读取该用户沙箱目录下的 message_state.json 真实历史
        user_msg_file = PROJECT_DIR / "data" / username / "message_state.json"
        if user_msg_file.exists():
            try:
                with open(user_msg_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        raw_msgs = data
                    elif isinstance(data, dict):
                        raw_msgs = data.get("messages", [])
                    else:
                        raw_msgs = []
                    res = []
                    for m in raw_msgs:
                        text = m.get("content") if "content" in m else m.get("text", "")
                        if text:
                            res.append({
                                "sender": m.get("sender", "未知"),
                                "content": text,
                                "source": m.get("source", "客户端"),
                                "time": m.get("time"),  # 消息时间戳，供前端按时间差计算思考时间
                            })
                    return res
            except Exception:
                pass
        return []

    msgs = await asyncio.to_thread(_get)
    return web.json_response({"ok": True, "messages": msgs})


async def gateway_api_chat(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效，请重新登录。"}, status=401)

    # 认知循环服务端控制：未启动认知循环时，拦截发送并提示
    if not GLOBAL_COGNITION_ENABLED:
        return web.json_response({
            "ok": False,
            "error": "请联系管理员启动认知循环",
            "message": "请联系管理员启动认知循环",
            "reply": "【提示】请联系管理员启动认知循环",
            "think_seconds": 0.0,
        }, status=400)

    sender = data.get("sender", "测试群友")
    text = data.get("text", "")
    mentioned = bool(data.get("mentioned", True))
    if not text.strip():
        return web.json_response({"ok": False, "message": "消息为空。"})

    # 确保主进程集中式模型服务已启动（所有子进程共用此单例模型服务）
    RUNTIME.initialize()

    def _process():
        worker = USER_WORKERS.get_worker(username)
        if GLOBAL_COGNITION_ENABLED:
            worker.start_cognition()
        return worker.chat(sender, text, mentioned)

    try:
        res = await asyncio.to_thread(_process)
        return web.json_response(res)
    except Exception as e:
        logging.exception("用户对话处理异常: %s", e)
        return web.json_response({"ok": False, "message": f"处理失败: {e}", "reply": f"【错误】处理失败: {e}"}, status=500)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    with _ws_clients_lock:
        _ws_clients.add(ws)
    signal_handler.add_client(ws)
    # 连接建立后补发最近日志（避免白屏）
    for cat, lines in _log_buffer.items():
        for line in lines[-200:]:
            await ws.send_str(json.dumps({"type": "log", "cat": cat, "line": line}, ensure_ascii=False))
    try:
        async for msg in ws:
            pass  # 服务端只做推送，客户端无需发消息
    finally:
        signal_handler.remove_client(ws)
        with _ws_clients_lock:
            _ws_clients.discard(ws)
    return ws


async def api_status(request):
    """总览状态：记忆/链接/词网/运行时间/虚拟时间/QQ状态/模型状态。"""
    try:
        from core.memory_engine import provide_for_monitor
        from core.virtual_clock import clock
        import datetime as dtmod
        from core.model_backend import BACKENDS

        mem, links, words = provide_for_monitor()
        elapsed = clock.get_real_runtime()
        h, m, s = int(elapsed // 3600), int((elapsed % 3600) // 60), int(elapsed % 60)
        # 始终按真实时间显示（默认 qq 模式，速度为 1）
        real_ts = clock.to_real_time(clock.now())
        clock_str = dtmod.datetime.fromtimestamp(real_ts).strftime("%Y-%m-%d %H:%M:%S")

        model_status = {}
        for name, b in BACKENDS.items():
            model_status[name] = {
                "use_default": b.use_default(),
                "local_ready": b.is_local_ready(),
                "api_model": b.api_model(),
                "port": b.local_port,
            }

        return web.json_response({
            "mem": mem, "links": links, "words": words,
            "runtime": f"{h:02d}:{m:02d}:{s:02d}",
            "clock": clock_str,
            "cognition": {"running": GLOBAL_COGNITION_ENABLED},
            "models": model_status,
            "initialized": RUNTIME.initialized,
            "bot_name": BOT_NAME,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_config_get(request):
    """返回配置（后端开关 + API + bot 名）。

    出于安全考虑，API Key 不返回给前端（置空）。
    前端保存时若未改动 key，保持原值；只有用户重新填写才覆盖。
    """
    safe = json.loads(json.dumps(config))
    for name in ("text", "embed", "multimodal"):
        safe["backends"].get(name, {})["api_key"] = ""
    return web.json_response(safe)


def _write_client_gateway(port: int):
    """把客户端 gateway 指向新端口（客户端刷新后即连接新端口）。"""
    path = CLIENT_DIR / "client_config.json"
    try:
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data["gateway"] = f"http://127.0.0.1:{port}"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[配置] 客户端 gateway 已同步为 http://127.0.0.1:%d", port)
    except Exception:
        logging.exception("[配置] 写入 client_config.json 失败")


async def _apply_server_port(port: int):
    """实时切换 HTTP 监听端口：停掉旧 site，在新端口启动新 site（复用同一 runner）。"""
    global _SITES, _CURRENT_PORT
    if port == _CURRENT_PORT:
        return
    for site in _SITES:
        await site.stop()
    _SITES = []
    site = web.TCPSite(_RUNNER, "127.0.0.1", port)
    await site.start()
    _SITES = [site]
    _CURRENT_PORT = port
    logging.info("[配置] Web 控制面板监听端口已实时切换为 http://127.0.0.1:%d", port)


async def api_config_save(request):
    """保存配置（「配置」页：Bot 名字 / 服务端监听端口 / 网页版客户端端口；「模型」页走 /api/model/switch）。

    - bot_name：仅修改配置，重启进程生效（原行为不变）
    - server_port：写入配置 + 同步客户端 gateway + 实时切换监听端口（无需重启）
    - client_port：写入配置 + 实时切换网页版客户端监听端口（无需重启）
    """
    data = await request.json() or {}
    bot_name = (data.get("bot_name") or "").strip()
    if bot_name:
        config["bot_name"] = bot_name

    port_msg = ""
    new_port = data.get("server_port")
    if new_port is not None:
        try:
            new_port = int(new_port)
        except (TypeError, ValueError):
            new_port = -1
        if not (1 <= new_port <= 65535):
            return web.json_response({"ok": False, "message": "端口需为 1-65535 的整数。"})
        config["server_port"] = new_port
        _write_client_gateway(new_port)
        try:
            await _apply_server_port(new_port)
            port_msg = f"，服务端端口已实时切换为 {new_port}"
        except Exception as e:
            logging.exception("[配置] 端口切换失败")
            return web.json_response({"ok": False, "message": f"端口切换失败: {e}"})

    client_port_msg = ""
    new_client_port = data.get("client_port")
    if new_client_port is not None:
        try:
            new_client_port = int(new_client_port)
        except (TypeError, ValueError):
            new_client_port = -1
        if not (1 <= new_client_port <= 65535):
            return web.json_response({"ok": False, "message": "网页版客户端端口需为 1-65535 的整数。"})
        config["client_port"] = new_client_port
        try:
            await _apply_client_port(new_client_port)
            client_port_msg = f"，网页版客户端端口已实时切换为 {new_client_port}"
        except Exception as e:
            logging.exception("[配置] 网页版客户端端口切换失败")
            return web.json_response({"ok": False, "message": f"网页版客户端端口切换失败: {e}"})

    save_config(config)
    reload_config()
    logging.info("配置已保存（bot_name=%s%s%s）", config.get("bot_name"), port_msg, client_port_msg)
    return web.json_response({"ok": True, "needs_restart": bool(bot_name),
                              "message": f"已保存{port_msg}{client_port_msg}"})


async def api_model_switch(request):
    """切换某个后端的模式：/api/model/switch  body: {name, use_default, api_base_url, api_key, api_model}"""
    data = await request.json()
    name = data.get("name")
    result = _switch_backend(
        name,
        data.get("use_default", True),
        data.get("api_base_url", ""),
        data.get("api_key", ""),
        data.get("api_model", ""),
    )
    return web.json_response(result)


# ------------------------------------------------------------------------
# 维护页：语义向量维度重建
# ------------------------------------------------------------------------
async def api_embed_dim_status(request):
    """返回维度重建工具的状态：当前存储维度 / 模型输出维度 / 记忆数 / 任务进度。"""
    from core import embed_rebuild
    status = embed_rebuild.rebuild_status()
    # 任务未运行时，若 embed 后端已就绪，可即时探测模型输出维度（不触发模型启动）
    model_dim = status.get("model_dim")
    if model_dim is None and not status.get("running"):
        try:
            from core.model_backend import BACKENDS
            if BACKENDS["embed"].is_local_ready():
                model_dim = embed_rebuild.get_model_dim()
        except Exception:
            model_dim = None
    return web.json_response({
        "stored_dim": status.get("stored_dim"),
        "model_dim": model_dim,
        "memory_count": status.get("memory_count"),
        "running": status.get("running"),
        "done": status.get("done"),
        "total": status.get("total"),
        "target_dim": status.get("target_dim"),
        "error": status.get("error"),
        "log": status.get("log"),
    })


async def api_embed_dim_rebuild(request):
    """启动全量维度重建：/api/maintenance/embed_rebuild  body: {target_dim?}"""
    from core import embed_rebuild
    data = await request.json() or {}
    target_dim = data.get("target_dim")
    if target_dim is not None:
        try:
            target_dim = int(target_dim)
        except (TypeError, ValueError):
            target_dim = None
    result = embed_rebuild.start_rebuild(target_dim=target_dim)
    return web.json_response(result)


async def api_cognition_start(request):
    global GLOBAL_COGNITION_ENABLED
    try:
        GLOBAL_COGNITION_ENABLED = True
        # 初始化主进程集中式模型服务
        await asyncio.to_thread(RUNTIME.initialize)
        # 通知所有已注册的活跃子进程启动其各自独立的认知循环
        USER_WORKERS.start_cognition_for_all()
        _broadcast({"type": "status", "text": "认知循环已全局启动（各用户专属子进程已激活）"})
        return web.json_response({"ok": True})
    except Exception as e:
        logging.exception("启动认知循环失败")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_cognition_stop(request):
    global GLOBAL_COGNITION_ENABLED
    try:
        GLOBAL_COGNITION_ENABLED = False
        USER_WORKERS.stop_cognition_for_all()
        _broadcast({"type": "status", "text": "认知循环已全局停止"})
        return web.json_response({"ok": True})
    except Exception as e:
        logging.exception("停止认知循环失败")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_chat(request):
    """对话测试：/api/chat  body: {sender, text, mentioned}"""
    data = await request.json()
    try:
        sender = data.get("sender", "测试群友")
        text = data.get("text", "")
        mentioned = bool(data.get("mentioned", True))
        
        # 确保主进程集中式模型服务已初始化
        RUNTIME.initialize()
        
        def _process():
            worker = USER_WORKERS.get_worker("_admin")
            res = worker.chat(sender, text, mentioned)
            return sender, res.get("reply", "")

        sender, reply = await asyncio.to_thread(_process)
        return web.json_response({"ok": True, "sender": sender, "reply": reply})
    except Exception as e:
        logging.exception("对话测试失败")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_fake_think(request):
    data = await request.json()
    try:
        reply = await asyncio.to_thread(RUNTIME.fake_think, data.get("text", ""))
        return web.json_response({"ok": True, "reply": reply})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_inject_memory(request):
    data = await request.json()
    try:
        memory_id = await asyncio.to_thread(RUNTIME.inject_memory, data.get("content", ""))
        return web.json_response({"ok": True, "memory_id": memory_id})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_save(request):
    try:
        await asyncio.to_thread(RUNTIME.save)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_history(request):
    """返回最近对话历史（供对话测试页初始化）。"""
    from utils.message_history import get_all
    return web.json_response({"messages": get_all()})


async def api_logs(request):
    """返回最近日志（各分类）。"""
    return web.json_response({"logs": {cat: lines[-500:] for cat, lines in _log_buffer.items()}})


async def api_logs_clear(request):
    """清空日志缓冲（各分类）。"""
    for cat in _log_buffer:
        _log_buffer[cat].clear()
    logging.info("日志缓冲已清空")
    return web.json_response({"ok": True})


async def api_shutdown(request):
    """安全退出 Web 服务与全部后台。"""
    logging.info("收到安全退出请求，准备关闭所有子进程与服务...")
    _broadcast({"type": "status", "text": "服务正在安全退出..."})

    def _shutdown_process():
        time.sleep(0.4)
        _safe_finalize()
        logging.info("服务已安全退出，进程终止。")
        os._exit(0)

    def _force_exit_watchdog():
        # 兜底看门狗：若正常退出流程超时（各用户子进程优雅停止认知循环约需
        # 15~20 秒），才强制终止进程，保证服务一定能退出。
        time.sleep(30)
        try:
            logging.info("安全退出超时，强制终止服务进程。")
            USER_WORKERS.shutdown_all()
        except Exception:
            pass
        os._exit(1)

    threading.Thread(target=_shutdown_process, daemon=True).start()
    threading.Thread(target=_force_exit_watchdog, daemon=True).start()
    return web.json_response({"ok": True, "message": "服务正在退出..."})


# ------------------------------------------------------------------------
# 退出保护（保证任何路径关闭时都执行最终保存）
# ------------------------------------------------------------------------
_shutdown_guard = threading.Lock()


def _safe_finalize():
    """幂等地执行最终关停与保存。

    供按钮退出 / 信号处理 / atexit / _serve finally 多路径调用，
    通过 _shutdown_guard 保证只执行一次，避免重复保存或并发竞争。
    """
    with _shutdown_guard:
        try:
            USER_WORKERS.shutdown_all()
        except Exception:
            pass
        try:
            RUNTIME.shutdown()
        except Exception:
            logging.exception("最终关停异常")
        from utils.message_history import flush_to_file
        try:
            flush_to_file()
        except Exception:
            pass


def _signal_exit(*_):
    """信号处理器：终端 Ctrl+C / 关闭信号 → 先最终保存，再退出进程。"""
    logging.info("收到退出信号，正在执行最终保存并退出…")
    try:
        _safe_finalize()
    except Exception:
        pass
    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(0)


def _register_exit_protection():
    """注册多层退出保护：

    1. atexit 钩子：进程任何方式退出前兜底保存（含 os._exit 之外的正常退出）
    2. SIGINT / SIGTERM 信号：终端 Ctrl+C / taskkill 优雅信号 → 保存后退出
    """
    import atexit
    atexit.register(_safe_finalize)
    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _signal_exit)
            except (ValueError, OSError):
                pass  # 非主线程或平台不支持时忽略


def _graceful_exit():
    """按钮退出路径：等前端收到响应后执行最终关停。"""
    time.sleep(0.3)
    _safe_finalize()
    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(0)


# ------------------------------------------------------------------------
# 日志配置
# ------------------------------------------------------------------------
class _IgnoreProactorResetFilter(logging.Filter):
    """过滤 Windows asyncio Proactor 的无害回调异常。

    浏览器关闭 WebSocket 连接时，Windows Proactor 事件循环在 socket 清理
    回调中可能抛出 ConnectionResetError（WinError 10054）。这是连接已关闭
    后的无害噪音，不属于真实错误。此过滤器精准匹配该场景，避免日志刷屏，
    同时保留其他 asyncio 真实错误。
    """

    def filter(self, record):
        msg = record.getMessage()
        if "Exception in callback" in msg and "_call_connection_lost" in msg:
            return False
        if "ConnectionResetError" in msg and "10054" in msg:
            return False
        if "远程主机强迫关闭" in msg:
            return False
        return True


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    session_log = LOG_DIR / f"webui_{time.strftime('%Y%m%d_%H%M%S')}.log"
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    file_handler = logging.FileHandler(session_log, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    global signal_handler
    signal_handler = SignalLogHandler()
    signal_handler.setFormatter(formatter)
    root.addHandler(signal_handler)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    # 静音 aiohttp 的访问日志（Web 轮询/ping 刷屏），保留其 WARNING/ERROR 级日志
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    # 过滤 Windows Proactor 的无害 ConnectionResetError 回调噪音（浏览器断连时）
    logging.getLogger("asyncio").addFilter(_IgnoreProactorResetFilter())
    # 清空上一会话的日志缓冲（环形缓冲仅保留本次会话）
    for cat in _log_buffer:
        _log_buffer[cat].clear()


# ------------------------------------------------------------------------
# CORS：客户端独立存放后可跨源访问（file:// 或其它端口的静态服务器）
# ------------------------------------------------------------------------
@web.middleware
async def _cors_middleware(request, handler):
    origin = request.headers.get("Origin", "")
    if origin:
        # 放行任意来源（客户端为纯本地工具；auth 依赖 token，与 CORS 无关）
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
        return resp
    return await handler(request)


@web.middleware
async def _cors_options_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
        origin = request.headers.get("Origin", "")
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp
    return await handler(request)


# ------------------------------------------------------------------------
# 启动
# ------------------------------------------------------------------------
def build_client_app() -> web.Application:
    """构建网页版客户端应用（独立端口）：仅提供客户端静态页面。

    与本地 client/ 目录完全一致（index.html 用相对路径引用资源），
    浏览器访问独立端口即获得与本地客户端相同的网页服务。
    """
    app = web.Application(middlewares=[_cors_options_middleware, _cors_middleware])
    app.router.add_get("/", client_index_handler)
    app.router.add_get("/index.html", client_index_handler)
    app.router.add_get("/client_config.json", client_config_handler)
    app.router.add_get("/{name:style.css|app.js|nav_style.css|nav_script.js|logo.PNG}", client_asset_handler)
    app.router.add_get("/nav/{name:nav_style.css|nav_script.js}", client_asset_handler, name="nav_asset")
    app.router.add_get("/images/{name:logo.PNG}", client_asset_handler, name="img_asset")
    return app


def build_app() -> web.Application:
    """构建 aiohttp 应用（包含用户客户端界面、管理控制台及完整 API）。"""
    app = web.Application(middlewares=[_cors_options_middleware, _cors_middleware])

    # 1. 客户端静态主入口与路由 (/)
    app.router.add_get("/", client_index_handler)
    app.router.add_get("/index.html", client_index_handler)
    app.router.add_get("/client", client_index_handler)
    app.router.add_get("/client_config.json", client_config_handler)
    # 客户端 index 用相对路径引用资源：服务端下发模式下按文件名映射到 client/
    app.router.add_get("/{name:style.css|app.js|nav_style.css|nav_script.js|logo.PNG}", client_asset_handler)
    app.router.add_get("/nav/{name:nav_style.css|nav_script.js}", client_asset_handler, name="nav_asset")
    app.router.add_get("/images/{name:logo.PNG}", client_asset_handler, name="img_asset")

    # 2. 管理员控制面板主入口 (/admin)
    app.router.add_get("/admin", admin_index_handler)
    app.router.add_get("/admin/index.html", admin_index_handler)
    app.router.add_get("/admin/ws", ws_handler)
    app.router.add_get("/ws", ws_handler)

    # 3. 静态资源路由（同时支持客户端与管理面板）
    app.router.add_static("/admin_static", WEBUI_DIR, name="admin_static")
    app.router.add_static("/static", CLIENT_DIR, name="client_static")
    app.router.add_static("/webui_static", WEBUI_DIR, name="webui_static")

    # 4. 用户网关 REST API (/gateway/*)
    app.router.add_post("/gateway/register", gateway_api_register)
    app.router.add_post("/gateway/login", gateway_api_login)
    app.router.add_get("/gateway/bot", gateway_api_bot_info)
    app.router.add_get("/gateway/history", gateway_api_history)
    app.router.add_post("/gateway/chat", gateway_api_chat)

    # 5. 管理员控制台 REST API (/api/*)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_save)
    app.router.add_post("/api/model/switch", api_model_switch)
    app.router.add_get("/api/maintenance/embed_dim", api_embed_dim_status)
    app.router.add_post("/api/maintenance/embed_rebuild", api_embed_dim_rebuild)
    app.router.add_post("/api/cognition/start", api_cognition_start)
    app.router.add_post("/api/cognition/stop", api_cognition_stop)
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/fake_think", api_fake_think)
    app.router.add_post("/api/inject_memory", api_inject_memory)
    app.router.add_post("/api/save", api_save)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_post("/api/logs/clear", api_logs_clear)
    app.router.add_post("/api/shutdown", api_shutdown)
    return app


def main():
    # 解析端口：命令行 --port 优先，否则读配置中保存的 server_port
    port = DEFAULT_WEB_PORT
    args = sys.argv[1:]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])
    else:
        try:
            cfg_port = config.get("server_port")
            if cfg_port:
                port = int(cfg_port)
        except Exception:
            pass

    # 网页版客户端独立端口（读配置；随服务器运行）
    client_port = DEFAULT_CLIENT_PORT
    try:
        cfg_cp = config.get("client_port")
        if cfg_cp:
            client_port = int(cfg_cp)
    except Exception:
        pass

    os.chdir(PROJECT_DIR)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    configure_logging()
    logging.info("Web 控制面板启动，项目目录=%s", PROJECT_DIR)

    # 主进程生命周期统一托管：把 multiprocessing 的辅助进程（resource_tracker）
    # 也绑定到共享 Job Object，保证主进程退出（含强杀）时连同这些辅助进程一并回收。
    # 注意：resource_tracker 在 import 阶段创建 USER_WORKERS 的 Queue 时已被启动。
    try:
        import multiprocessing.resource_tracker as _rt
        from core.win_job import assign_pid
        _tracker_pid = getattr(getattr(_rt, "_resource_tracker", None), "_pid", None)
        if _tracker_pid:
            assign_pid(_tracker_pid)
    except Exception:
        pass

    # 系统启动：全局只允许一个 llama-server，先杀掉所有残留/其它实例的 llama-server
    # （孤儿进程、崩溃遗留、其它实例启动的进程都会被清理，避免端口冲突与内存翻倍）
    try:
        from core.model_backend import kill_all_llama_servers
        kill_all_llama_servers()
    except Exception:
        logging.exception("启动时清理 llama-server 进程失败")

    # 事件总线 → WebSocket 转发
    BUS.subscribe("message", _bus_forwarder)
    BUS.subscribe("status", _bus_forwarder)

    # 注册退出保护：atexit + 信号 → 保证任何方式关闭时最终保存
    _register_exit_protection()

    app = build_app()
    runner = web.AppRunner(app)
    asyncio.run(_serve(runner, port, client_port))


async def _start_client_site(cp: int):
    """在独立端口启动网页版客户端服务（随服务器运行）。"""
    global _CLIENT_RUNNER, _CLIENT_SITES, _CLIENT_PORT
    runner = web.AppRunner(build_client_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cp)
    await site.start()
    _CLIENT_RUNNER = runner
    _CLIENT_SITES = [site]
    _CLIENT_PORT = cp
    logging.info("网页版客户端已就绪：http://127.0.0.1:%d （与本地客户端相同）", cp)


async def _apply_client_port(cp: int):
    """实时切换网页版客户端监听端口（配置页保存 client_port 时调用）。"""
    global _CLIENT_RUNNER, _CLIENT_SITES, _CLIENT_PORT
    if cp == _CLIENT_PORT:
        return
    for site in _CLIENT_SITES:
        await site.stop()
    _CLIENT_SITES = []
    if _CLIENT_RUNNER:
        await _CLIENT_RUNNER.cleanup()
        _CLIENT_RUNNER = None
    if cp == _CURRENT_PORT:
        # 与主服务端口相同：不再独立监听，主服务根路径已提供客户端
        _CLIENT_PORT = cp
        logging.info("网页版客户端端口与服务端相同（%d），共用主服务入口", cp)
        return
    await _start_client_site(cp)


async def _serve(runner, port, client_port=None):
    global _ws_broadcast_queue, _ws_broadcast_task, _main_loop, _RUNNER, _SITES, _CURRENT_PORT
    # 记录主事件循环 + 初始化广播队列，供任意线程的日志/事件入队
    _main_loop = asyncio.get_event_loop()
    _ws_broadcast_queue = asyncio.Queue(maxsize=2000)
    _ws_broadcast_task = asyncio.create_task(_ws_broadcast_worker())
    # 记录 runner 与初始 site（供「配置」页实时切换端口）
    _RUNNER = runner
    _CURRENT_PORT = port

    # 启动 IPC 日志与事件监听线程：将所有子进程 Worker 的日志与事件聚合到主进程
    def _ipc_log_worker():
        while True:
            try:
                item = USER_WORKERS.log_queue.get()
                if item is None:
                    break
                if isinstance(item, dict) and item.get("_ipc_type") == "bus_event":
                    topic = item.get("topic")
                    args = item.get("args", ())
                    _bus_forwarder(topic, *args)
                elif hasattr(item, "name"):
                    logger = logging.getLogger(item.name)
                    logger.handle(item)
            except Exception:
                pass

    ipc_log_thread = threading.Thread(target=_ipc_log_worker, daemon=True, name="IPC-Log-Drainer")
    ipc_log_thread.start()

    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    _SITES = [site]
    logging.info("Web 控制面板已就绪：http://127.0.0.1:%d （按 Ctrl+C 退出）", port)

    # 网页版客户端独立端口：随服务器启动（与本地客户端相同的网页服务）
    if client_port and client_port != port:
        try:
            await _start_client_site(client_port)
        except Exception:
            logging.exception("启动网页版客户端服务失败（端口=%s）", client_port)

    try:
        # 保持事件循环运行（等待 Ctrl+C / 退出请求）
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if _ws_broadcast_task:
            _ws_broadcast_task.cancel()
        try:
            if _CLIENT_RUNNER:
                await _CLIENT_RUNNER.cleanup()
        except Exception:
            pass
        try:
            await runner.cleanup()
        except Exception:
            pass
        # 兜底最终保存（Ctrl+C 等路径在关闭 runner 后仍保存）
        _safe_finalize()


if __name__ == "__main__":
    main()
