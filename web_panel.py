# web_panel.py
# ========================================================================
# Nascence_Huiye Web 控制面板
#
# 背景：原控制面板是 PyQt5 桌面程序（control_panel.py）。本模块提供等价的
# Web 形态：同一套后端 Runtime（panel_runtime.py），通过 HTTP + WebSocket
# 暴露给浏览器。
#
# 设计约束（见实施文档第 5 章）：
#   - 只替换"面板"这一层，核心逻辑一行不改（认知循环、记忆引擎、
#     生物钟、概念层、动作层、QQ 接入均原样复用）。
#   - 关闭浏览器**不应**停止 QQ 服务与认知循环：服务在独立线程的事件循环里跑。
#   - 不引入新的第三方依赖：只用 aiohttp（项目已有），且不依赖 PyQt5。
#
# 启动：python web_panel.py [--host 127.0.0.1] [--port 32123] [--open]
# 端口可在控制面板页面中配置（默认 32123，保存后下次启动生效）。
# ========================================================================

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

from aiohttp import web, WSMsgType, WSCloseCode

import panel_runtime as panel
from config.constants import BOT_NAME
from utils.event_bus import BUS
from web_page import PAGE_HTML

logger = logging.getLogger("WebPanel")

# 事件积压上限：订阅方（浏览器）断线时不能让内存无限增长
_MAX_EVENT_QUEUE = 500


class EventHub:
    """把 EventBus 的事件转成可推送给浏览器的队列。

    事件可能来自任意线程（QQ 服务线程、认知循环、面板线程），
    这里统一入队；推送协程从队列取出发给所有已连接的 WebSocket。
    """

    def __init__(self):
        self._subscribers = set()
        self._lock = threading.Lock()
        self._loop = None
        self._pending = []          # 尚无事件循环时先缓存
        # 订阅 EventBus：核心侧无需知道 Web 面板存在
        BUS.log.connect(lambda cat, line: self._publish({"type": "log", "cat": cat, "line": line}))
        BUS.status.connect(lambda text: self._publish({"type": "status", "text": text}))
        BUS.stage.connect(lambda text, done: self._publish(
            {"type": "stage", "text": text, "done": bool(done)}))
        BUS.message.connect(lambda s, c, src: self._publish(
            {"type": "message", "sender": s, "content": c, "source": src}))
        BUS.task_error.connect(lambda text: self._publish({"type": "error", "text": text}))

    def bind_loop(self, loop):
        """绑定推送用的事件循环（由 Web 服务启动时调用）。"""
        with self._lock:
            self._loop = loop

    def _publish(self, payload: dict):
        with self._lock:
            loop = self._loop
            subs = list(self._subscribers)
            if loop is None:
                # 循环尚未就绪：先缓存，等绑定后补发（保持有界）
                self._pending.append(payload)
                if len(self._pending) > _MAX_EVENT_QUEUE:
                    del self._pending[:-_MAX_EVENT_QUEUE]
                return
            pending, self._pending = self._pending, []
        if pending:
            for p in pending:
                self._send(loop, subs, p)
        self._send(loop, subs, payload)

    @staticmethod
    def _send(loop, subs, payload):
        for ws in subs:
            try:
                # 从发布方线程调度到事件循环线程发送
                asyncio.run_coroutine_threadsafe(_safe_send(ws, payload), loop)
            except Exception:
                pass

    def subscribe(self, ws):
        with self._lock:
            self._subscribers.add(ws)
            loop = self._loop
            pending, self._pending = self._pending, []
        # 新连接补发缓存事件，避免浏览器错过订阅前的内容
        if loop is not None:
            for p in pending:
                try:
                    asyncio.run_coroutine_threadsafe(_safe_send(ws, p), loop)
                except Exception:
                    pass

    def unsubscribe(self, ws):
        with self._lock:
            self._subscribers.discard(ws)

    def notify(self, payload: dict):
        """向全部浏览器直推一条事件（关停通知等，不经 EventBus）。"""
        with self._lock:
            subs = list(self._subscribers)
            loop = self._loop
        if loop is None:
            return
        for ws in subs:
            try:
                asyncio.run_coroutine_threadsafe(_safe_send(ws, payload), loop)
            except Exception:
                pass

    def close_all(self):
        """关停时主动断开所有浏览器连接。

        不做这一步，aiohttp 的 runner.cleanup() 会等 WebSocket 长连接
        自然超时（两个 60s 超时叠加 = 实测 120 秒），而浏览器每 3 秒还在
        自动重连，于是"关闭面板"看起来像死机。这里由面板主动发关闭帧。
        """
        with self._lock:
            subs, self._subscribers = list(self._subscribers), set()
            loop = self._loop
        if loop is None:
            return
        for ws in subs:
            try:
                asyncio.run_coroutine_threadsafe(_close_ws(ws), loop)
            except Exception:
                pass


async def _close_ws(ws):
    try:
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"panel shutting down")
    except Exception:
        pass


async def _safe_send(ws, payload: dict):
    try:
        await ws.send_json(payload)
    except Exception:
        # 连接已断，交由 WebSocket 处理器清理
        pass


HUB = EventHub()


def _install_graceful_exception_handler(loop):
    """静默 Windows Proactor 的断连竞态噪声。

    浏览器刷新/关闭时连接被强行重置，ProactorEventLoop 会在
    _call_connection_lost 里弹 ConnectionResetError（WinError 10054）。
    这是关连瞬态，不是错误；与 qq_bot.py 的关机过滤同一思路。
    """
    orig_handler = loop.get_exception_handler()

    def _handler(current_loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return
        if isinstance(exc, AssertionError) and "_attach" in str(context.get("handle", "")):
            return
        if orig_handler:
            orig_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


# ---------- HTTP 处理器 ----------

async def handle_index(request):
    """面板首页（内联 HTML，避免额外静态文件依赖）。"""
    return web.Response(text=PAGE_HTML.replace("{{BOT_NAME}}", BOT_NAME),
                        content_type="text/html", charset="utf-8")


def _collect_stats() -> dict:
    """在工作线程里收集统计信息。

    这里的导入（core.memory_engine / core.biorhythm / qq_bot）绝不能放在
    事件循环线程上执行：首次导入是秒级操作，且会与核心初始化线程争抢
    模块导入锁——这正是"启动卡在加载核心"的成因。统一丢进线程池后，
    事件循环始终可以及时响应 HTTP/WebSocket。
    """
    data = {}
    try:
        from core.memory_engine import provide_for_monitor
        from core.virtual_clock import clock
        import datetime as dtmod

        mem, links, words = provide_for_monitor()
        data["memory"] = mem
        data["links"] = links
        data["words"] = words
        data["clock"] = round(clock.now(), 1)

        elapsed = clock.get_real_runtime()
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        data["runtime"] = f"{h:02d}:{m:02d}:{s:02d}"

        if clock.qq_mode:
            real_ts = clock.to_real_time(clock.now())
            dt = dtmod.datetime.fromtimestamp(real_ts)
            data["clock_text"] = dt.strftime("%H:%M:%S") + f"  ×{clock.speed:g}"
        else:
            virt_now = clock.now()
            days = int(virt_now // 86400)
            hours = int((virt_now % 86400) // 3600)
            mins = int((virt_now % 3600) // 60)
            data["clock_text"] = f"{days}d {hours:02d}:{mins:02d}  ×{clock.speed:g}"
    except Exception as e:
        data["stats_error"] = str(e)
    try:
        from core.biorhythm import BIORHYTHM
        snap = BIORHYTHM.snapshot()
        data["energy"] = snap.get("energy")
        data["state"] = snap.get("state")
        data["circadian"] = snap.get("circadian")
        data["rhythm_nights"] = snap.get("rhythm_nights")
        data["sleep_hours"] = BIORHYTHM.rhythm_hours()
    except Exception as e:
        data["biorhythm_error"] = str(e)
    data["qq_running"] = bool(panel.RUNTIME.qq_thread and panel.RUNTIME.qq_thread.is_alive())
    data["core_ready"] = panel.RUNTIME.initialized
    data["init_stage"] = panel.RUNTIME.init_stage
    data["init_elapsed"] = round(panel.RUNTIME.init_elapsed(), 1)
    data["shutting_down"] = panel.RUNTIME.shutdown_requested
    try:
        import qq_bot
        data["napcat_connected"] = qq_bot._napcat_websocket is not None
    except Exception:
        data["napcat_connected"] = False
    return data


async def handle_stats(request):
    """总览数据（重型导入都在线程池执行，不阻塞事件循环）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _collect_stats)
    return web.json_response(data)


async def handle_config_get(request):
    cfg = panel.load_panel_config()
    safe = {k: v for k, v in cfg.items() if "key" not in k}
    safe["_has_primary_key"] = bool(str(cfg.get("primary_api_key") or "").strip())
    safe["_has_secondary_key"] = bool(str(cfg.get("secondary_api_key") or "").strip())
    return web.json_response(safe)


# API Key / 文本字段 / 数值字段分别处理：空值不覆盖，数字容错转换。
_CONFIG_STR_KEYS = (
    "primary_base_url", "primary_model", "secondary_base_url", "secondary_model",
    "bot_qq", "active_group_id", "napcat_token", "napcat_http_url",
)
_CONFIG_KEY_KEYS = ("primary_api_key", "secondary_api_key")
_CONFIG_FLOAT_KEYS = (
    "biorhythm_wake_seconds", "biorhythm_sleep_seconds",
    "biorhythm_onset_threshold", "biorhythm_wake_threshold",
    "biorhythm_rhythm_weight", "biorhythm_idle_to_sleep",
)
_CONFIG_INT_KEYS = ("biorhythm_rhythm_min_nights", "biorhythm_rhythm_full_nights", "panel_port")


async def handle_config_post(request):
    """保存配置。API Key 为空表示"不修改"，避免掩码回写把密钥清掉。"""
    body = await request.json()
    cfg = panel.load_panel_config()

    for key in _CONFIG_STR_KEYS:
        if key in body and body[key] not in (None, ""):
            cfg[key] = str(body[key])
    for key in _CONFIG_KEY_KEYS:
        if body.get(key):
            cfg[key] = str(body[key])
    for key in _CONFIG_FLOAT_KEYS:
        if key in body and body[key] not in (None, ""):
            try:
                cfg[key] = float(body[key])
            except (TypeError, ValueError):
                pass
    for key in _CONFIG_INT_KEYS:
        if key in body and body[key] not in (None, ""):
            try:
                cfg[key] = int(float(body[key]))
            except (TypeError, ValueError):
                pass

    panel.save_panel_config(cfg)
    # 热刷新里包含 qq_bot / memory_engine 等重型导入，必须放线程池：
    # 事件循环线程执行秒级导入会把整个面板卡住（与 /api/stats 同一教训）。
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, panel.apply_config_hot_reload, cfg)
    return web.json_response({"ok": True})


async def handle_qq_start(request):
    try:
        loop = asyncio.get_running_loop()
        # 初始化（启动 Ollama、加载记忆）可能耗时，放线程池避免卡住 HTTP
        await loop.run_in_executor(None, panel.RUNTIME.start_qq)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_qq_stop(request):
    try:
        await asyncio.get_running_loop().run_in_executor(None, panel.RUNTIME.stop_qq)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_save(request):
    """立即保存全部数据（绕过节流）。"""
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: panel.RUNTIME.save(force=True))
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_maintenance(request):
    """维护动作：伪造发送 / 伪造思考 / 记忆注入。"""
    body = await request.json()
    kind = body.get("kind")
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "内容为空"}, status=400)
    loop = asyncio.get_running_loop()
    try:
        if kind == "fake_send":
            mid = await loop.run_in_executor(None, panel.RUNTIME.fake_send, text)
            return web.json_response({"ok": True, "detail": f"记忆ID {mid}"})
        if kind == "fake_think":
            reply = await loop.run_in_executor(None, panel.RUNTIME.fake_think, text)
            return web.json_response({"ok": True, "detail": reply})
        if kind == "inject":
            mid = await loop.run_in_executor(None, panel.RUNTIME.inject_memory, text)
            return web.json_response({"ok": True, "detail": f"记忆ID {mid}"})
        return web.json_response({"ok": False, "error": "未知操作"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_shutdown(request):
    """面板"关闭"按钮：优雅停止全部服务并退出。

    只负责置位停止信号；真正的关停顺序（先断 WebSocket → 停服务 →
    落盘 → 清理 HTTP）在主循环里统一执行，避免重入。
    """
    if request.app["stop_event"].is_set():
        return web.json_response({"ok": True, "detail": "已在关闭中"})
    panel.RUNTIME.request_shutdown()
    request.app["stop_event"].set()
    logging.info("收到面板关闭请求，正在停止全部服务…")
    return web.json_response({"ok": True})


async def _replay_log_tail(ws, max_lines: int = 200):
    """连接建立时补发本次会话日志的尾部，避免刷新页面后日志页空白。

    桌面面板直接读取日志文件回填，这里用同样的思路：只发尾部若干行，
    分类沿用"运行日志"（与桌面面板的初始回填一致，不重新猜测分类）。
    """
    try:
        if not panel.SESSION_LOG.exists():
            return
        with panel.SESSION_LOG.open("r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
        for line in lines:
            line = line.rstrip("\n")
            if line:
                await ws.send_json({"type": "log", "cat": "runtime", "line": line})
    except Exception:
        logging.exception("日志回填失败")


async def handle_ws(request):
    """事件推送：日志、状态、消息、错误。"""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    HUB.subscribe(ws)
    await _replay_log_tail(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        HUB.unsubscribe(ws)
    return ws


# ---------- 启动 ----------

def _build_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/api/config", handle_config_get)
    app.router.add_post("/api/config", handle_config_post)
    app.router.add_post("/api/qq/start", handle_qq_start)
    app.router.add_post("/api/qq/stop", handle_qq_stop)
    app.router.add_post("/api/save", handle_save)
    app.router.add_post("/api/shutdown", handle_shutdown)
    app.router.add_post("/api/maintenance", handle_maintenance)
    app.router.add_get("/ws", handle_ws)
    return app


def run(host=None, port=None, open_browser=False):
    """启动 Web 面板（阻塞）。QQ 服务在独立线程里运行，不受面板开关影响。"""
    panel.configure_logging()
    # 每 5 秒一次的轮询统计不该刷满日志页：访问日志只保留告警以上
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    cfg = panel.load_panel_config()
    if host is None:
        host = str(cfg.get("panel_host") or "127.0.0.1")
    if port is None:
        try:
            port = int(cfg.get("panel_port") or 32123)
        except (TypeError, ValueError):
            port = 32123

    lock = panel.acquire_panel_lock()
    if lock is None:
        print("控制面板已经在运行，请先关闭旧的面板（桌面或 Web）后再启动。",
              file=sys.__stderr__)
        return 2

    app = _build_app()
    stop_event = threading.Event()
    # 让 /api/shutdown 处理器能触达同一个停止信号
    app["stop_event"] = stop_event

    def _on_signal(*_):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        # Windows 控制台的 Ctrl+Break / 关闭终端前的 CTRL_BREAK 事件
        try:
            signal.signal(signal.SIGBREAK, _on_signal)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, _on_signal)
        except (ValueError, OSError):
            pass

    def _init_core():
        try:
            panel.RUNTIME.initialize()
            BUS.status.emit("核心已就绪")
        except Exception as e:
            logging.exception("运行环境初始化失败（面板仍会启动）")
            BUS.task_error.emit(f"核心初始化失败：{panel.format_error(str(e))}")

    async def _main():
        _install_graceful_exception_handler(asyncio.get_running_loop())
        # shutdown_timeout 默认 60s：aiohttp 会为尚未关闭的长连接等满超时，
        # 多条叠加后正是实测的 120 秒"关不掉"。真正断开由下面主动 close_all 完成，
        # 这里把兜底超时压到 3 秒，确保任何残余连接都不会再拖住退出。
        runner = web.AppRunner(app, shutdown_timeout=3)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        HUB.bind_loop(asyncio.get_running_loop())
        url = f"http://{host}:{port}"
        logger.info(f"Web 控制面板已启动：{url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        # 核心初始化放后台线程：面板先可用，不阻塞首屏
        threading.Thread(target=_init_core, name="PanelInit", daemon=True).start()

        while not stop_event.is_set():
            await asyncio.sleep(0.3)

        logging.info("收到退出信号，正在停止服务并保存数据…")
        # 关停顺序很重要：
        #   1) 通知浏览器停止重连（否则它会每 3 秒重连、把清理拖住）
        #   2) 停止监听新连接
        #   3) 主动断开既有 WebSocket
        #   4) 停服务并落盘（可能较慢，放线程池）
        #   5) HTTP runner 清理（此时已无长连接，秒级完成）
        HUB.notify({"type": "shutdown"})
        try:
            await site.stop()
        except Exception:
            pass
        HUB.close_all()
        await asyncio.sleep(0.2)   # 让关闭帧发出
        await asyncio.get_running_loop().run_in_executor(None, panel.RUNTIME.shutdown)
        await runner.cleanup()
        logging.info("Web 控制面板已退出")

    try:
        asyncio.run(_main())
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98, 10048, 10049) or "in use" in str(e).lower():
            print(f"端口 {port} 已被占用，请换一个端口（--port）或关闭占用程序。",
                  file=sys.__stderr__)
        else:
            print(f"Web 面板启动失败：{e}", file=sys.__stderr__)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        panel.release_panel_lock(lock)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nascence Huiye Web 控制面板")
    parser.add_argument("--host", default=None, help="监听地址（默认从配置读取或 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认从配置读取或 32123）")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="启动后自动打开浏览器")
    args = parser.parse_args()
    raise SystemExit(run(args.host, args.port, open_browser=args.open_browser))
