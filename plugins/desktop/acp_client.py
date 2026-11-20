# plugins/desktop/acp_client.py
# ========================================================================
# 【未接线 · 预留】外联 DeepSeek Harness 的 ACP 客户端。
#
# 当前版本不调用本模块：电脑插件只走 NHY 自己的操作手（desktop_operator.py）。
# 保留它是为了以后明确要做 dsh 外联时，不用重新摸索 ACP 细节。
# 接线方式：在 DesktopPlugin._run_task 里按配置选择 acp_client.run_task 即可。
#
# 链路（以后启用时）：
#   dsh --profile acp  (stdio JSON-RPC，常驻)
#     initialize → session/new(cwd, mcpServers=[])
#     session/prompt(task) → session/update 流 → 摘要 → APP_CONTEXT
#   acp profile 默认没有 dsh-mcp-client，需要用 --patch + `- insert:` 插入。
# ========================================================================

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from config.api_config import config

PROJECT_DIR = Path(__file__).resolve().parents[2]
MCP_SCRIPT = Path(__file__).resolve().parent / "mcp_server.py"
WORKSPACE = PROJECT_DIR / "data" / "test" / "desktop_workspace"
ACP_PATCH = WORKSPACE / "desktop-mcp.patch.yml"

_PROTOCOL_VERSION = 1

_TOOL_LABELS = {
    "snapshot": "看一眼界面",
    "click": "点击",
    "set_value": "写入文字",
    "type_text": "输入文字",
    "press_key": "按键",
    "focus": "聚焦",
    "scroll": "滚动",
    "launch_app": "打开程序",
    "list_windows": "找窗口",
    "close_window": "关窗口",
}


class AcpError(RuntimeError):
    pass


def _candidate_app_dirs():
    """扫描常见用户目录下的桌面端安装位置（当前进程可能不是安装用户）。"""
    names = ("io.github.hairyf.deepseek-harness-desktop",)
    bases = []
    for value in (os.environ.get("APPDATA", ""), str(Path.home() / "AppData" / "Roaming")):
        if value and value not in bases:
            bases.append(value)
    for root in ("C:/Users", "D:/Users", "E:/Users", "F:/Users", "G:/Users"):
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            for user in base.iterdir():
                roaming = user / "AppData" / "Roaming"
                if roaming.is_dir() and str(roaming) not in bases:
                    bases.append(str(roaming))
        except OSError:
            continue
    for base in bases:
        for name in names:
            app_dir = Path(base) / name
            if app_dir.is_dir():
                yield app_dir


def _find_dsh_app():
    return next(_candidate_app_dirs(), None)


def _dsh_home() -> str:
    """定位 DSH_HOME：优先配置，其次 ~/.dsh，最后从安装目录推。"""
    configured = str(config.get("desktop_dsh_home") or "").strip()
    if configured and Path(configured).is_dir():
        return configured
    home = Path.home() / ".dsh"
    if (home / "profiles").is_dir():
        return str(home)
    app_dir = _find_dsh_app()
    if app_dir is not None:
        for parent in app_dir.parents:
            candidate = parent / ".dsh"
            if (candidate / "profiles").is_dir():
                return str(candidate)
    return ""


def _write_patch(plugin=None) -> str:
    """生成 acp profile 的 patch：把 desktop MCP 挂进 dsh 的工具树。

    acp profile 默认没有 dsh-mcp-client，必须在启动时用 --patch 插入；
    `- insert:` 是 loader 的插入语法（普通 id 条目表示“改已有项”）。
    """
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    lines = [
        "- insert:",
        "    - id: desktop-mcp",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        "        serverName: desktop",
        "        transport: stdio",
        f"        command: '{Path(sys.executable).resolve()}'",
        "        args:",
        f"          - '-X'",
        f"          - 'utf8'",
        f"          - '{MCP_SCRIPT}'",
    ]
    endpoint = {}
    if plugin is not None:
        try:
            endpoint = plugin.worker_endpoint() or {}
        except Exception:
            endpoint = {}
    if endpoint.get("port") and endpoint.get("token"):
        lines.extend([
            "        env:",
            f"          NHY_DESKTOP_WORKER_PORT: '{endpoint['port']}'",
            f"          NHY_DESKTOP_WORKER_TOKEN: '{endpoint['token']}'",
        ])
    ACP_PATCH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(ACP_PATCH)


def _resolve_launcher() -> list:
    """返回 [node.exe, bin.js, --profile, acp, --patch, ...]；找不到返回 []。"""
    configured = str(config.get("desktop_dsh_launcher") or "").strip()
    if configured:
        try:
            argv = shlex.split(configured)
        except ValueError:
            argv = configured.split()
        if argv and "--profile" not in argv:
            argv += ["--profile", "acp"]
    else:
        argv = []
        app_dir = _find_dsh_app()
        if app_dir is not None:
            node = app_dir / "runtime" / "node.exe"
            bin_js = app_dir / "dependencies" / "dsh" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
            if node.is_file() and bin_js.is_file():
                argv = [str(node), str(bin_js), "--profile", "acp"]
        if not argv:
            which = shutil_which("dsh")
            if which:
                argv = [which, "--profile", "acp"]
    if argv and ACP_PATCH.exists() and "--patch" not in argv:
        argv += ["--patch", str(ACP_PATCH)]
    return argv


def shutil_which(name: str) -> str:
    from shutil import which

    return which(name) or ""


def _api_key() -> str:
    key = str(config.get("primary_api_key") or "").strip()
    if key and "deepseek" in str(config.get("primary_base_url") or ""):
        return key
    return str(config.get("secondary_api_key") or "").strip()


class AcpClient:
    """一个 dsh ACP 子进程 + JSON-RPC 请求/通知分发。"""

    def __init__(self, argv: list, env: dict, cwd: str):
        self._lock = threading.RLock()
        self._next_id = 0
        self._pending: dict = {}
        self._updates: "queue.Queue[dict]" = queue.Queue()
        self._stderr: deque = deque(maxlen=30)
        self._closed = False
        self.final_message = ""
        self.last_stderr = ""
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
        threading.Thread(target=self._read_stdout, name="AcpStdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="AcpStderr", daemon=True).start()

    # ---------- 传输 ----------
    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._lock:
                    waiter = self._pending.pop(message["id"], None)
                if waiter is not None:
                    waiter.put(message)
                continue
            method = str(message.get("method") or "")
            if method == "session/update":
                self._updates.put(message.get("params") or {})
            elif method == "session/request_permission":
                self._answer_permission(message)
            elif "id" in message:
                # 未声明的客户端方法：明确回不支持，避免 dsh 等超时。
                self._write({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"unsupported client method: {method}"},
                })

    def _read_stderr(self):
        for line in self.proc.stderr:
            text = line.strip()
            if text:
                self._stderr.append(text)
                self.last_stderr = "\n".join(self._stderr)

    def _write(self, payload: dict):
        if self._closed or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def _answer_permission(self, message: dict):
        params = message.get("params") or {}
        options = params.get("options") or []
        choice = next((o for o in options if str(o.get("optionId")) == "allow-once"), None)
        if choice is None:
            choice = next(
                (o for o in options if str(o.get("kind", "")).startswith("allow")), None
            )
        if choice is not None:
            result = {"outcome": {"outcome": "selected", "optionId": choice.get("optionId")}}
        else:
            result = {"outcome": {"outcome": "cancelled"}}
        self._write({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    def _send(self, method: str, params: dict) -> "queue.Queue[dict]":
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            waiter: "queue.Queue[dict]" = queue.Queue(maxsize=1)
            self._pending[req_id] = waiter
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return waiter

    def request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        waiter = self._send(method, params)
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty:
            raise AcpError(f"{method} 超时（{timeout:.0f}s）")
        if "error" in message:
            error = message.get("error") or {}
            raise AcpError(f"{method} 失败：{error.get('message') or error}")
        return message.get("result") or {}

    # ---------- ACP ----------
    def initialize(self) -> dict:
        return self.request(
            "initialize",
            {"protocolVersion": _PROTOCOL_VERSION, "clientCapabilities": {}},
            timeout=30.0,
        )

    def new_session(self, cwd: str, mcp_servers: list) -> str:
        result = self.request(
            "session/new", {"cwd": cwd, "mcpServers": mcp_servers}, timeout=120.0
        )
        session_id = str(result.get("sessionId") or "")
        if not session_id:
            raise AcpError("session/new 没有返回 sessionId")
        return session_id

    def prompt(
        self,
        session_id: str,
        text: str,
        timeout: float,
        on_update: Optional[Callable[[dict], None]] = None,
    ) -> str:
        waiter = self._send(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )
        deadline = time.time() + timeout
        result = None
        while time.time() < deadline:
            self._drain_updates(session_id, on_update)
            try:
                message = waiter.get(timeout=0.4)
            except queue.Empty:
                continue
            result = message
            break
        self._drain_updates(session_id, on_update)
        if result is None:
            self._write({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
            raise AcpError(f"dsh 超过 {timeout:.0f}s 没有结束")
        if "error" in result:
            error = result.get("error") or {}
            raise AcpError(f"session/prompt 失败：{error.get('message') or error}")
        return str((result.get("result") or {}).get("stopReason") or "end_turn")

    def _drain_updates(self, session_id: str, on_update):
        while True:
            try:
                params = self._updates.get_nowait()
            except queue.Empty:
                return
            if str(params.get("sessionId") or "") != session_id:
                continue
            update = params.get("update") or {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    self.final_message += str(content.get("text") or "")
            if on_update is not None:
                try:
                    on_update(update)
                except Exception:
                    pass

    def close(self):
        with self._lock:
            self._closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self._updates.put({})
        if self._stderr:
            self.last_stderr = "\n".join(self._stderr)
        else:
            self.last_stderr = ""


def _tool_label(title: str) -> str:
    low = str(title or "").lower()
    for key, label in _TOOL_LABELS.items():
        if key in low:
            return label
    return str(title or "操作")[:20]


def run_task(plugin, task: str, context: str = "") -> dict:
    """用常驻 ACP 把任务交给 dsh；进度写回插件上下文。"""
    _write_patch(plugin)
    argv = _resolve_launcher()
    if not argv:
        return {
            "ok": False,
            "summary": "没找到可用的 dsh（可在面板里填 dsh 命令）",
            "lines": [],
        }
    key = _api_key()
    env = os.environ.copy()
    env["DSH_TELEMETRY_DISABLED"] = "1"
    home = _dsh_home()
    if home:
        env["DSH_HOME"] = home
    if key:
        env["DEEPSEEK_API_KEY"] = key
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    try:
        timeout = max(30, int(config.get("desktop_task_timeout", 180)))
    except (TypeError, ValueError):
        timeout = 180

    client = None
    lines = []
    last_label = ""
    try:
        client = AcpClient(argv, env, str(WORKSPACE))
        plugin.on_progress("把这件事交给 dsh 了")
        client.initialize()
        # MCP 通过 --patch 挂在 profile 层，session 层不再重复挂载。
        session_id = client.new_session(str(WORKSPACE), [])

        def on_update(update: dict):
            nonlocal last_label
            kind = str(update.get("sessionUpdate") or "")
            if kind == "tool_call":
                label = _tool_label(update.get("title") or "")
                if label != last_label:
                    last_label = label
                    lines.append(label)
                    plugin.on_progress(f"dsh：{label}")
            elif kind == "agent_thought_chunk":
                content = update.get("content") or {}
                text = str(content.get("text") or "").strip()
                if text:
                    plugin.on_progress(f"dsh 在想：{text[:40]}")

        prompt_text = task if not context else f"{task}\n\n（背景：{context}）"
        stop_reason = client.prompt(session_id, prompt_text, timeout, on_update)
        summary = client.final_message.strip() or (lines[-1] if lines else "dsh 做完了")
        return {
            "ok": stop_reason in ("end_turn", "max_tokens"),
            "summary": summary[:400],
            "lines": lines[-4:],
        }
    except Exception as exc:
        tail = (client.last_stderr if client is not None else "").strip().splitlines()[-2:]
        detail = f"{exc}" + (f" | {tail}" if tail else "")
        return {"ok": False, "summary": f"dsh 那边没走通：{detail}"[:400], "lines": lines[-3:]}
    finally:
        if client is not None:
            client.close()
