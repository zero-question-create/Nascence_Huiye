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
#   - 关闭浏览器**不应**停止插件服务与认知循环：服务在独立线程的事件循环里跑。
#   - 不引入新的第三方依赖：只用 aiohttp（项目已有），且不依赖 PyQt5。
#
# 启动：python web_panel.py [--host 127.0.0.1] [--port 32123] [--open]
# 端口可在控制面板页面中配置（默认 32123，保存后下次启动生效）。
# ========================================================================

import argparse
import asyncio
import hmac
import ipaddress
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
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

    事件可能来自任意线程（插件服务线程、认知循环、面板线程），
    这里统一入队；推送协程从队列取出发给所有已连接的 WebSocket。
    """

    def __init__(self):
        self._subscribers = set()
        self._lock = threading.Lock()
        self._loop = None
        self._pending = []          # 尚无事件循环时先缓存
        # 订阅 EventBus：核心侧无需知道 Web 面板存在
        BUS.log_event.connect(
            lambda cat, line, level: self._publish(
                {"type": "log", "cat": cat, "line": line, "level": level}
            )
        )
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
    # 观测探针：LLM 调用/耗时/token、锁等待、事件循环阻塞、缓存命中
    try:
        from utils.metrics_probe import PROBE
        data["probe"] = PROBE.snapshot()
    except Exception:
        pass
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
    data["cognitive_running"] = bool(
        panel.RUNTIME.cognitive_thread and panel.RUNTIME.cognitive_thread.is_alive()
    )
    data["core_ready"] = panel.RUNTIME.initialized
    data["init_stage"] = panel.RUNTIME.init_stage
    data["init_elapsed"] = round(panel.RUNTIME.init_elapsed(), 1)
    data["shutting_down"] = panel.RUNTIME.shutdown_requested
    try:
        import qq_bot
        data["napcat_connected"] = qq_bot._napcat_websocket is not None
        data["qq_identity"] = qq_bot.get_qq_identity()
        data["qq_app"] = qq_bot.app_status()
    except Exception:
        data["napcat_connected"] = False
        data["qq_identity"] = {}
        data["qq_app"] = {}
    try:
        from plugins.registry import active_plugin, get_active_id
        data["active_plugin"] = get_active_id()
        active = active_plugin()
        data["active_plugin_label"] = active.label if active else ""
    except Exception as e:
        data["plugin_error"] = str(e)
    return data


async def handle_stats(request):
    """总览数据（重型导入都在线程池执行，不阻塞事件循环）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _collect_stats)
    return web.json_response(data)


async def handle_config_get(request):
    cfg = panel.load_panel_config()
    safe = {k: v for k, v in cfg.items() if "key" not in k}
    safe["_has_primary_key"] = str(cfg.get("primary_api_key") or "").strip() not in ("", "YOUR_API_KEY_HERE")
    safe["_has_secondary_key"] = str(cfg.get("secondary_api_key") or "").strip() not in ("", "YOUR_API_KEY_HERE")
    safe["_has_embedding_key"] = bool(str(cfg.get("embedding_api_key") or "").strip())
    safe["_has_app_router_key"] = bool(str(cfg.get("app_router_api_key") or "").strip())
    return web.json_response(safe)


# API Key / 文本字段 / 数值字段分别处理：空值不覆盖，数字容错转换。
_CONFIG_STR_KEYS = (
    "primary_base_url", "primary_model",
    "behavior_base_url", "behavior_model",
    "secondary_base_url", "secondary_model",
    "ollama_base_url", "ollama_embed_model",
    "embedding_api_mode",
    "bot_qq", "active_group_id", "napcat_token", "napcat_http_url",
    "app_router_mode", "app_router_base_url", "app_router_model",
    "browser_mode", "browser_dynamic_url", "browser_summary_base_url", "browser_summary_model",
)
_CONFIG_KEY_KEYS = (
    "primary_api_key", "behavior_api_key", "secondary_api_key", "embedding_api_key",
    "app_router_api_key",
    "browser_dynamic_api_key", "browser_summary_api_key",
)
_CONFIG_BOOL_KEYS = (
    "app_router_follow_global_behavior",
    "biorhythm_never_sleep",
)
_CONFIG_FLOAT_KEYS = (
    "biorhythm_wake_seconds", "biorhythm_sleep_seconds",
    "biorhythm_onset_threshold", "biorhythm_wake_threshold",
    "biorhythm_rhythm_weight", "biorhythm_idle_to_sleep",
)
_CONFIG_INT_KEYS = (
    "biorhythm_rhythm_min_nights", "biorhythm_rhythm_full_nights", "panel_port",
    "embedding_dim",
    "app_open_after_unread_rounds", "app_close_conversation_idle_rounds",
    "app_close_idle_rounds", "app_router_timeout", "app_router_max_tokens",
)


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
    for key in _CONFIG_BOOL_KEYS:
        if key in body:
            cfg[key] = bool(body[key])
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


async def handle_plugins_get(request):
    """插件运行状态与行为模型配置（API Key 不明文返回）。

    首次访问会触发插件模块导入（QQ 运行时含 faiss 等重型依赖），
    必须丢线程池，避免卡住面板事件循环。
    """
    def _collect():
        from plugins import registry
        plugins = registry.list_plugins()
        return {
            "ok": True,
            "active": registry.get_active_id(),
            "plugins": plugins,
            "path": registry.plugin_config_path(),
        }

    try:
        data = await asyncio.get_running_loop().run_in_executor(None, _collect)
        return web.json_response(data)
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_plugins_post(request):
    """保存插件启用状态与行为模型配置。"""
    try:
        body = await request.json()
        from plugins import registry
        result = await asyncio.get_running_loop().run_in_executor(
            None, registry.update_from_panel, body
        )
        return web.json_response({"ok": True, **result})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_plugin_start(request):
    """通用插件启动入口（QQ 会复用现有 QQ 服务线程）。"""
    try:
        body = await request.json()
        plugin_id = str(body.get("id") or "").strip()
        from plugins.registry import get_plugin, update_from_panel
        plugin = get_plugin(plugin_id)
        if plugin is None:
            return web.json_response({"ok": False, "error": "插件不存在"}, status=404)
        # 从插件页直接点“启动”时顺手启用，避免启动了一个未启用、不会进入软件门控的插件。
        if not plugin.enabled:
            await asyncio.get_running_loop().run_in_executor(
                None, update_from_panel, {"plugin_id": plugin_id, "enabled": True}
            )
            plugin = get_plugin(plugin_id)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: asyncio.run(plugin.start()))
        # 启动成功才记为“自动启动”：下次面板启动时按这个标记恢复运行。
        await loop.run_in_executor(
            None, update_from_panel,
            {"plugin_id": plugin_id, "enabled": True, "autostart": True},
        )
        return web.json_response({"ok": True})
    except NotImplementedError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=501)
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_plugin_stop(request):
    try:
        body = await request.json()
        plugin_id = str(body.get("id") or "").strip()
        from plugins.registry import get_plugin, update_from_panel
        plugin = get_plugin(plugin_id)
        if plugin is None:
            return web.json_response({"ok": False, "error": "插件不存在"}, status=404)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: asyncio.run(plugin.stop()))
        # 插件页只有“启动/停止”一个开关：停止即退出运行与软件门控，
        # 同时清掉自动启动标记，下次面板启动不会再拉起。
        await loop.run_in_executor(
            None, update_from_panel,
            {"plugin_id": plugin_id, "enabled": False, "autostart": False},
        )
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_plugin_panel_save(request):
    """保存插件自述面板的字段；写哪个配置文件由插件自己决定。"""
    try:
        body = await request.json()
        plugin_id = str(body.get("id") or "").strip()
        values = body.get("values")
        from plugins import registry
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, registry.save_panel, plugin_id, values if isinstance(values, dict) else {}
        )
        if not result.get("ok", True):
            return web.json_response(
                {"ok": False, "error": result.get("error") or "保存失败"}, status=400
            )
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_plugin_readme(request):
    """读取已安装插件的 README（只允许插件目录内文件）。"""
    try:
        plugin_id = str(request.query.get("id") or "").strip()
        if not plugin_id or "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id:
            return web.json_response({"ok": False, "error": "插件 id 非法"}, status=400)
        from plugins import registry
        from core.plugin_manifest import read_manifest, normalize_manifest

        plugin_dir = PROJECT_DIR / "plugins" / plugin_id
        manifest = normalize_manifest(read_manifest(plugin_dir), fallback_id=plugin_id)
        readme_name = str(manifest.get("readme") or "README.md")
        readme_path = (plugin_dir / readme_name).resolve()
        root = plugin_dir.resolve()
        if root != readme_path and root not in readme_path.parents:
            return web.json_response({"ok": False, "error": "README 路径越界"}, status=400)
        text = ""
        if readme_path.is_file():
            text = readme_path.read_text(encoding="utf-8", errors="replace")[:200000]
        return web.json_response({"ok": True, "id": plugin_id, "readme": text})
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_plugin_panel_action(request):
    """执行插件面板上声明的动作（自测、收起工作桌面等）。"""
    try:
        body = await request.json()
        plugin_id = str(body.get("id") or "").strip()
        key = str(body.get("key") or "").strip()
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        from plugins import registry
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, registry.panel_action, plugin_id, key, payload
        )
        if not result.get("ok", True):
            return web.json_response(
                {"ok": False, "error": result.get("error") or "执行失败"}, status=400
            )
        return web.json_response({"ok": True, "detail": result.get("detail") or ""})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


def _is_loopback(request) -> bool:
    try:
        peer = request.transport.get_extra_info("peername")
        host = str((peer or ("", 0))[0])
        return ipaddress.ip_address(host).is_loopback
    except Exception:
        return False


def _community_guard(request):
    """安装/更新/导入/重启的写操作保护。

    本机访问默认放行；远程（Docker/反代）必须配置 panel_token，
    否则任何人都能通过面板安装并执行插件代码。
    """
    from config.api_config import config

    try:
        enabled = bool(config.get("community_install_enabled", True))
    except Exception:
        enabled = True
    if not enabled:
        return web.json_response({"ok": False, "error": "插件安装已在配置中关闭"}, status=403)
    expected = str(config.get("panel_token") or "").strip()
    provided = (
        request.headers.get("X-NHY-Token")
        or request.query.get("token")
        or ""
    ).strip()
    if expected:
        if not provided or not hmac.compare_digest(provided, expected):
            return web.json_response({"ok": False, "error": "管理 token 不正确"}, status=403)
    elif not _is_loopback(request):
        return web.json_response(
            {"ok": False, "error": "远程安装插件需要先配置 panel_token"},
            status=403,
        )
    return None


async def handle_community_plugins(request):
    """返回 GitHub/cached catalog + 本机安装摘要。"""
    try:
        force = str(request.query.get("refresh") or "") in ("1", "true", "yes")
        from core import plugin_market

        loop = asyncio.get_running_loop()
        catalog, installed = await asyncio.wait_for(
            asyncio.gather(
                loop.run_in_executor(None, plugin_market.get_catalog, force),
                loop.run_in_executor(None, plugin_market.installed_summary),
            ),
            timeout=30,
        )
        return web.json_response({
            "ok": True,
            "plugins": catalog.get("plugins") or [],
            "installed": installed,
            "stale": bool(catalog.get("stale")),
            "error": catalog.get("error") or "",
            "fetched_at": catalog.get("fetched_at"),
        })
    except asyncio.TimeoutError:
        from core import plugin_market

        cached = plugin_market.get_catalog(force=False)
        return web.json_response({
            "ok": True,
            "plugins": cached.get("plugins") or [],
            "installed": plugin_market.installed_summary(),
            "stale": True,
            "error": "GitHub 目录刷新超时，当前显示本地缓存",
            "fetched_at": cached.get("fetched_at"),
        })
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_community_config(request):
    """社区设置：GitHub token/账号/topic/安装开关。"""
    from config.api_config import load_config

    if request.method == "GET":
        cfg = load_config()
        return web.json_response({
            "ok": True,
            "github_token_configured": bool(str(cfg.get("community_github_token") or "").strip()),
            "github_user": str(cfg.get("community_github_user") or ""),
            "topic": str(cfg.get("community_topic") or "nhy-plugin"),
            "install_enabled": bool(cfg.get("community_install_enabled", True)),
            "allow_git": bool(cfg.get("community_allow_git", True)),
        })
    denied = _community_guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
        cfg = load_config()
        token = str(body.get("github_token") or "").strip()
        if token:
            cfg["community_github_token"] = token
        if body.get("clear_token"):
            cfg["community_github_token"] = ""
        if "github_user" in body:
            cfg["community_github_user"] = str(body.get("github_user") or "").strip()
        if "topic" in body:
            cfg["community_topic"] = str(body.get("topic") or "nhy-plugin").strip()
        if "install_enabled" in body:
            cfg["community_install_enabled"] = bool(body.get("install_enabled"))
        if "allow_git" in body:
            cfg["community_allow_git"] = bool(body.get("allow_git"))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, panel.save_panel_config, cfg)
        await loop.run_in_executor(None, panel.apply_config_hot_reload, cfg)
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_community_readme(request):
    try:
        plugin_id = str(request.query.get("id") or "").strip()
        if not plugin_id:
            return web.json_response({"ok": False, "error": "缺少插件 id"}, status=400)
        from core import plugin_market

        text = await asyncio.get_running_loop().run_in_executor(
            None, plugin_market.read_readme, plugin_id
        )
        return web.json_response({"ok": True, "id": plugin_id, "readme": text})
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_community_install(request):
    denied = _community_guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
        plugin_id = str(body.get("id") or "").strip()
        if not plugin_id:
            return web.json_response({"ok": False, "error": "缺少插件 id"}, status=400)
        from core import plugin_market

        result = await asyncio.get_running_loop().run_in_executor(
            None, plugin_market.install_plugin, plugin_id, bool(body.get("force"))
        )
        if result.get("ok"):
            from plugins import registry
            await asyncio.get_running_loop().run_in_executor(None, registry.reload)
        return web.json_response(result, status=200 if result.get("ok") else 400)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_community_update(request):
    denied = _community_guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
        plugin_id = str(body.get("id") or "").strip()
        if not plugin_id:
            return web.json_response({"ok": False, "error": "缺少插件 id"}, status=400)
        from core import plugin_market

        result = await asyncio.get_running_loop().run_in_executor(
            None, plugin_market.update_plugin, plugin_id
        )
        if result.get("ok"):
            from plugins import registry
            await asyncio.get_running_loop().run_in_executor(None, registry.reload)
        return web.json_response(result, status=200 if result.get("ok") else 400)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_community_uninstall(request):
    denied = _community_guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
        plugin_id = str(body.get("id") or "").strip()
        if not plugin_id:
            return web.json_response({"ok": False, "error": "缺少插件 id"}, status=400)
        from core import plugin_market

        result = await asyncio.get_running_loop().run_in_executor(
            None, plugin_market.uninstall, plugin_id, bool(body.get("purge"))
        )
        if result.get("ok"):
            from plugins import registry
            await asyncio.get_running_loop().run_in_executor(None, registry.reload)
        return web.json_response(result, status=200 if result.get("ok") else 400)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_community_import(request):
    """导入本地插件：支持 zip 上传或本机路径。"""
    denied = _community_guard(request)
    if denied is not None:
        return denied
    try:
        from core.plugin_install import install_from_directory, install_from_zip

        content_type = str(request.headers.get("Content-Type") or "")
        path = ""
        upload = None
        if content_type.startswith("multipart/"):
            reader = await request.multipart()
            field = await reader.next()
            while field is not None:
                if field.name == "path":
                    path = (await field.text()).strip()
                elif field.name == "file" and field.filename:
                    suffix = Path(field.filename).suffix or ".zip"
                    upload_dir = PROJECT_DIR / "data" / "test" / "plugin_uploads"
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    upload = upload_dir / f"upload_{int(time.time())}{suffix}"
                    with upload.open("wb") as handle:
                        while True:
                            chunk = await field.read_chunk()
                            if not chunk:
                                break
                            handle.write(chunk)
                field = await reader.next()
        else:
            body = await request.json()
            path = str(body.get("path") or "").strip()

        if upload is not None:
            result = install_from_zip(upload, source="local-upload")
            upload.unlink(missing_ok=True)
        elif path:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = PROJECT_DIR / candidate
            if candidate.is_dir():
                result = install_from_directory(candidate, source="local-import")
            elif candidate.is_file():
                result = install_from_zip(candidate, source="local-import")
            else:
                return web.json_response({"ok": False, "error": "路径不存在"}, status=400)
        else:
            return web.json_response({"ok": False, "error": "请上传 zip 或填写路径"}, status=400)
        if result.get("ok"):
            from plugins import registry
            await asyncio.get_running_loop().run_in_executor(None, registry.reload)
        return web.json_response(result, status=200 if result.get("ok") else 400)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_restart(request):
    denied = _community_guard(request)
    if denied is not None:
        return denied
    try:
        request.app["restart_event"] = True

        async def _later():
            await asyncio.sleep(0.6)
            request.app["stop_event"].set()

        asyncio.create_task(_later())
        return web.json_response({"ok": True, "detail": "正在重启面板"})
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": panel.format_error(str(exc))}, status=500
        )


async def handle_cognitive_start(request):
    """总览页：启动全局认知循环。"""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, panel.RUNTIME.start_cognitive)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": panel.format_error(str(e))}, status=500)


async def handle_cognitive_stop(request):
    """总览页：停止全局认知循环（插件服务不受影响）。"""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, panel.RUNTIME.stop_cognitive)
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
    """维护动作：伪造思考 / 管理员记忆注入。"""
    body = await request.json()
    kind = body.get("kind")
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "内容为空"}, status=400)
    loop = asyncio.get_running_loop()
    try:
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
    """连接建立时补发本次会话日志的尾部，避免刷新页面后日志页空白。"""
    try:
        if not panel.SESSION_LOG.exists():
            return
        with panel.SESSION_LOG.open("r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
        for line in lines:
            line = line.rstrip("\n")
            if line:
                cat, level = panel.classify_log_line(line)
                await ws.send_json({
                    "type": "log", "cat": cat, "line": line, "level": level,
                })
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
    app.router.add_get("/api/plugins", handle_plugins_get)
    app.router.add_post("/api/plugins", handle_plugins_post)
    app.router.add_post("/api/plugins/start", handle_plugin_start)
    app.router.add_post("/api/plugins/stop", handle_plugin_stop)
    app.router.add_post("/api/plugins/panel", handle_plugin_panel_save)
    app.router.add_get("/api/plugins/readme", handle_plugin_readme)
    app.router.add_post("/api/plugins/panel/action", handle_plugin_panel_action)
    app.router.add_get("/api/community/plugins", handle_community_plugins)
    app.router.add_get("/api/community/config", handle_community_config)
    app.router.add_post("/api/community/config", handle_community_config)
    app.router.add_get("/api/community/readme", handle_community_readme)
    app.router.add_post("/api/community/install", handle_community_install)
    app.router.add_post("/api/community/update", handle_community_update)
    app.router.add_post("/api/community/uninstall", handle_community_uninstall)
    app.router.add_post("/api/community/import", handle_community_import)
    app.router.add_post("/api/restart", handle_restart)
    app.router.add_post("/api/cognitive/start", handle_cognitive_start)
    app.router.add_post("/api/cognitive/stop", handle_cognitive_stop)
    app.router.add_post("/api/save", handle_save)
    app.router.add_post("/api/shutdown", handle_shutdown)
    app.router.add_post("/api/maintenance", handle_maintenance)
    app.router.add_get("/ws", handle_ws)
    return app


def run(host=None, port=None, open_browser=False):
    """启动 Web 面板（阻塞）。插件服务在独立线程里运行，不受面板开关影响。"""
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
    app["restart_event"] = False
    app["panel_lock"] = lock

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
        if app.get("restart_event"):
            logging.info("Web 控制面板正在重启…")
            try:
                panel.release_panel_lock(app.get("panel_lock"))
            except Exception:
                pass
            args = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
            kwargs = {
                "cwd": str(PROJECT_DIR),
                "close_fds": True,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(args, **kwargs)
            # 旧进程必须彻底退出，避免 venv launcher + 新面板留下双进程。
            os._exit(0)
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
