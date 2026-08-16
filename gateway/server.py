# gateway/server.py
# ========================================================================
# Nascence 辉夜 对话网关服务端
#
# 职责：
#   - 接收 /gateway/* 前缀的请求（register / login / chat / history）
#   - 用户注册/登录与信息核对（密码加盐哈希存储）
#   - 每个用户使用独立的 data/<username>/ 目录存放记忆/faiss/对话状态
#   - 复用本项目核心（core.memory_engine / core.cognition / utils.persistence）
#     完成对话处理
#   - 托管独立对话客户端（gateway/client/）静态资源
#
# 运行：
#   python gateway/server.py --port 8899
# 访问：
#   http://127.0.0.1:8899/          （客户端登录/注册界面）
# ========================================================================

import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
from pathlib import Path

from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PROJECT_DIR))

from core import runtime_paths
from config.api_config import config, save_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("Gateway")

GATEWAY_DIR = Path(__file__).resolve().parent
CLIENT_DIR = PROJECT_DIR / "client"  # 客户端独立存放（可脱离服务端单独打开）
USERS_FILE = GATEWAY_DIR / "users.json"
SESSIONS_FILE = GATEWAY_DIR / "sessions.json"

# 对话处理全局串行锁：memory_engine 为进程级单例，同一时刻只加载一个用户的数据
_chat_lock = threading.Lock()
_engine_ready = False


# ------------------------------------------------------------------------
# 用户存储（加盐哈希）
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
    """仅允许字母数字下划线，避免路径穿越等安全问题。"""
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
    # 创建该用户独立的数据目录
    os.makedirs(f"data/{username}", exist_ok=True)
    logger.info("[注册] 新用户 %s 已创建，数据目录 data/%s/", username, username)
    return {"ok": True, "username": username}


def verify_user(username: str, password: str) -> bool:
    username = _sanitize_username(username)
    users = _load_users()
    rec = users.get(username)
    if not rec:
        return False
    return _hash_password(password, rec["salt"]) == rec["hash"]


# ------------------------------------------------------------------------
# 会话（token → username）
# ------------------------------------------------------------------------
def _load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_sessions(sessions: dict):
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)


def create_session(username: str) -> str:
    token = secrets.token_hex(24)
    sessions = _load_sessions()
    sessions[token] = username
    _save_sessions(sessions)
    return token


def resolve_session(token: str):
    """返回 token 对应的用户名；无效返回 None。"""
    if not token:
        return None
    sessions = _load_sessions()
    return sessions.get(token)


# ------------------------------------------------------------------------
# 核心引擎初始化（复用项目自身模型后端）
# ------------------------------------------------------------------------
def ensure_engine():
    """首次对话前初始化模型后端（幂等）。"""
    global _engine_ready
    if _engine_ready:
        return
    from core.model_backend import start_default_backends, check_models_ready
    from core.memory_engine import get_model
    from core.virtual_clock import clock
    start_default_backends()
    check_models_ready()
    clock.enable_qq_mode()
    get_model()
    _engine_ready = True


# ------------------------------------------------------------------------
# 按用户切换数据目录并加载其记忆
# ------------------------------------------------------------------------
def load_user_context(username: str):
    """切换数据目录到 data/<username>/，重置并加载该用户数据。"""
    user_dir = f"data/{username}"
    os.makedirs(user_dir, exist_ok=True)
    runtime_paths.set_data_dir(user_dir)

    from core.memory_engine import reset_engine_state, memories, _init_metrics_counters
    from utils.persistence import load_all_data, load_state
    from utils.dialogue_state import reset_state
    from utils import message_history
    from core.llm_interface import load_dialogue_history

    reset_engine_state()
    # 重置对话状态与消息历史缓冲，再加载该用户各自的持久化数据
    reset_state()
    message_history._message_history.clear()
    message_history._flushed_count = 0
    load_all_data()
    load_state()
    load_dialogue_history()
    _init_metrics_counters()
    return memories


# ------------------------------------------------------------------------
# HTTP 处理器
# ------------------------------------------------------------------------
async def index_handler(request):
    return web.FileResponse(CLIENT_DIR / "index.html")


async def static_handler(request):
    """托管客户端静态资源（/static/* 与根目录资源）。"""
    path = request.match_info.get("path", "")
    target = (CLIENT_DIR / path).resolve()
    if target.is_file() and CLIENT_DIR in target.parents:
        return web.FileResponse(target)
    return web.FileResponse(CLIENT_DIR / "index.html")


async def client_config_handler(request):
    return web.FileResponse(CLIENT_DIR / "client_config.json")


async def api_register(request):
    data = await request.json()
    result = register_user(data.get("username", ""), data.get("password", ""))
    if result["ok"]:
        token = create_session(result["username"])
        result["token"] = token
    return web.json_response(result)


async def api_login(request):
    data = await request.json()
    username = _sanitize_username(data.get("username", ""))
    password = data.get("password", "")
    if verify_user(username, password):
        token = create_session(username)
        logger.info("[登录] 用户 %s 登录成功", username)
        return web.json_response({"ok": True, "username": username, "token": token})
    return web.json_response({"ok": False, "message": "用户名或密码错误。"}, status=401)


async def api_chat(request):
    data = await request.json()
    username = resolve_session(data.get("token", ""))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效，请重新登录。"}, status=401)
    sender = data.get("sender", "测试群友")
    text = data.get("text", "")
    mentioned = bool(data.get("mentioned", True))
    if not text.strip():
        return web.json_response({"ok": False, "message": "消息为空。"})

    def _process():
        with _chat_lock:
            ensure_engine()
            from config.constants import BOT_NAME
            from core.cognition import process_dialogue
            from core.llm_interface import add_to_history
            from utils.persistence import append_dialogue, save_all_data, save_state

            load_user_context(username)
            augmented = (
                f'{sender}对{BOT_NAME}说：“{text}”'
                if mentioned
                else f'{sender}说：“{text}”'
            )
            logger.info("[%s] 输入=%s", username, augmented)
            reply, _ = asyncio.run(process_dialogue(augmented_input=augmented))
            reply = str(reply or "静默")
            if mentioned and reply.strip() in ("", "静默", "静默。", "[SILENT]"):
                reply = "嗯......"
            add_to_history(sender, text, reply, "网关")
            append_dialogue(augmented, reply)
            save_all_data()
            save_state()
            return reply

    try:
        reply = await asyncio.to_thread(_process)
        return web.json_response({"ok": True, "reply": reply, "username": username})
    except Exception as e:
        logger.exception("[%s] 对话处理失败", username)
        return web.json_response({"ok": False, "message": f"对话处理失败：{e}"}, status=500)


async def api_history(request):
    username = resolve_session((request.query.get("token") or ""))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效，请重新登录。"}, status=401)
    with _chat_lock:
        load_user_context(username)
        from utils.message_history import get_all
        return web.json_response({"ok": True, "messages": get_all()})


async def api_bot_info(request):
    from config.constants import BOT_NAME
    return web.json_response({"ok": True, "bot_name": BOT_NAME})


# ------------------------------------------------------------------------
# ------------------------------------------------------------------------
# CORS：客户端独立存放后可跨源访问（file:// 或其它端口的静态服务器）
# ------------------------------------------------------------------------
@web.middleware
async def _cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
        origin = request.headers.get("Origin", "")
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp
    resp = await handler(request)
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


# 构建应用
# ------------------------------------------------------------------------
def build_app() -> web.Application:
    app = web.Application(middlewares=[_cors_middleware])
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/static/{path:.*}", static_handler)
    app.router.add_get("/client_config.json", client_config_handler)
    # 网关前缀接口
    app.router.add_post("/gateway/register", api_register)
    app.router.add_post("/gateway/login", api_login)
    app.router.add_post("/gateway/chat", api_chat)
    app.router.add_get("/gateway/history", api_history)
    app.router.add_get("/gateway/bot", api_bot_info)
    return app


def main():
    port = 8899
    args = sys.argv[1:]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])

    app = build_app()
    runner = web.AppRunner(app)
    asyncio.run(_serve(runner, port))


async def _serve(runner, port):
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("对话网关已就绪：http://127.0.0.1:%d （客户端请访问本地址）", port)
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    main()
