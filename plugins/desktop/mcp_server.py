# plugins/desktop/mcp_server.py
# ========================================================================
# 电脑控制的 MCP stdio 服务：把 UiaBackend 暴露成标准工具。
#
# - 供 dsh（或任何 MCP 客户端）挂载：session/new 时传
#   {"name":"desktop","command":"<venv python>","args":["<此文件>"]}
# - 供 NHY 自己的 operator 直接复用同一套工具语义（进程内调用 backend）。
#
# 设计原则：先 snapshot 拿引用，再按 ref 操作；默认不截图。
# stdout 只输出 JSON-RPC，日志一律走 stderr。
# ========================================================================

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    # 允许 dsh 用 `python plugins/desktop/mcp_server.py` 直接拉起。
    # 脚本目录会排在 sys.path[0]，先摘掉，避免目录里的模块名遮住标准库。
    _script_dir = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _script_dir]
    # _script_dir=...\plugins\desktop，parents[1] 就是项目根
    sys.path.insert(0, str(_script_dir.parents[1]))

from plugins.desktop import uia  # noqa: E402
from plugins.desktop import input as desktop_input  # noqa: E402

try:  # 与 NHY 面板共用同一份开关（dsh 直接拉起时也能读到）
    from config.api_config import config as _CONFIG  # noqa: E402
except Exception:  # pragma: no cover - 独立运行时退回默认值
    _CONFIG = {}

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "nhy-desktop", "version": "0.1.0"}

_LAST_HWND = {"value": 0}
_CFG_PATH = Path(__file__).resolve().parents[2] / "config" / "api_config.json"
_CFG_CACHE = {"mtime": 0.0, "data": {}}


def _cfg(key: str, default):
    """优先读磁盘上的面板配置，让 dsh 里的长驻 MCP 也能看到面板改动。"""
    try:
        mtime = _CFG_PATH.stat().st_mtime
        if mtime != _CFG_CACHE["mtime"]:
            data = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
            _CFG_CACHE["data"] = data if isinstance(data, dict) else {}
            _CFG_CACHE["mtime"] = mtime
        if key in _CFG_CACHE["data"]:
            return _CFG_CACHE["data"][key]
    except Exception:
        pass
    try:
        return _CONFIG.get(key, default)
    except Exception:
        return default


def _last_hwnd() -> int:
    return int(_LAST_HWND.get("value") or 0)


def _worker() -> tuple:
    port = str(os.environ.get("NHY_DESKTOP_WORKER_PORT") or "").strip()
    token = str(os.environ.get("NHY_DESKTOP_WORKER_TOKEN") or "").strip()
    if not port or not token:
        return "", ""
    return port, token


def _worker_state() -> dict:
    """读取工作桌面状态（宿主 hwnd、已嵌入的窗口）；没运行就返回空。"""
    port, token = _worker()
    if not port:
        return {}
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/state",
        method="GET",
        headers={"X-Desktop-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _worker_launch(name: str) -> dict:
    """工作桌面在跑时由它启动并归位，返回窗口句柄。"""
    port, token = _worker()
    if not port:
        return {"ok": False, "error": "工作桌面没有运行"}
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/launch",
        data=json.dumps({"name": name}, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"X-Desktop-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _target_window(window=0) -> int:
    try:
        target = int(window or 0)
    except (TypeError, ValueError):
        target = 0
    return target or _last_hwnd()


def _coordinate_allowed(x: int, y: int, window=0) -> tuple:
    """与 NHY 操作手同一套围栏：开关 + 目标窗口/工作桌面矩形。"""
    if not bool(_cfg("desktop_allow_coordinate_input", True)):
        return False, "坐标输入已在 NHY 面板关闭"
    if not bool(_cfg("desktop_coordinate_fence", True)):
        return True, ""
    rects = []
    target = _target_window(window)
    if target:
        try:
            rects.append(uia.window_rect(target))
        except Exception:
            pass
    host = int(_worker_state().get("hwnd") or 0)
    if host:
        try:
            rects.append(uia.window_rect(host))
        except Exception:
            pass
    rects = [rect for rect in rects if rect and rect[2] > rect[0] and rect[3] > rect[1]]
    if not rects:
        return False, "没有可用的围栏目标窗口，坐标输入被拒绝"
    for left, top, right, bottom in rects:
        if left <= x <= right and top <= y <= bottom:
            return True, ""
    hint = "；".join(f"可用区域 {l},{t},{r},{b}" for l, t, r, b in rects)
    return False, f"坐标不在目标窗口/工作桌面内（{hint}）"


def _coord_pair(args: dict, key: str) -> tuple:
    value = args.get(key)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    return int(args.get("x") or 0), int(args.get("y") or 0)


def _prepare_coordinates(args: dict, points: list) -> dict:
    """所有点先过围栏；再尝试把目标窗口提到前台（失败不阻止点击，点击本身会聚焦）。"""
    for x, y in points:
        allowed, why = _coordinate_allowed(x, y, args.get("window"))
        if not allowed:
            return {"ok": False, "error": why, "x": x, "y": y}
    target = _target_window(args.get("window"))
    if target:
        desktop_input.focus_window(target)
    return {"ok": True}


_TOOLS = [
    {
        "name": "list_windows",
        "description": "列出可操作的顶层窗口（hwnd、标题、类名、pid）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "snapshot",
        "description": (
            "读取窗口的无障碍树文本。先调用它拿元素引用（形如 whwnd eN gG），"
            "再用 click/set_value 按引用操作。diff=true 只返回相对上次的变化。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": ["integer", "string"], "description": "hwnd 或标题片段；省略=前台窗口"},
                "ref": {"type": "string", "description": "只看某个元素的子树"},
                "depth": {"type": "integer", "default": 12},
                "max_nodes": {"type": "integer", "default": 400},
                "diff": {"type": "boolean", "default": False},
                "chrome": {"type": "boolean", "default": False, "description": "是否包含标题栏/滚动条"},
                "with_bounds": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否为可操作元素附上屏幕矩形 @l,t,r,b（坐标点击/键入用）",
                },
            },
        },
    },
    {
        "name": "click",
        "description": "按引用激活元素：优先 Invoke/Toggle/Select/Expand，不移动鼠标、不抢焦点。",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "set_value",
        "description": "把文本直接写进输入框/文档（ValuePattern），比逐键输入快且稳。",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
            "required": ["ref", "text"],
        },
    },
    {
        "name": "type_text",
        "description": "向当前焦点（或指定元素）输入文本；走剪贴板，不改动用户剪贴板内容以外的系统状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "ref": {"type": "string"},
                           "window": {"type": ["integer", "string"]}},
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": "发送按键，例如 Enter、Ctrl+A、Ctrl+Shift+P。",
        "inputSchema": {
            "type": "object",
            "properties": {"keys": {"type": "string"},
                           "window": {"type": ["integer", "string"]}},
            "required": ["keys"],
        },
    },
    {
        "name": "focus",
        "description": "把焦点移到某个元素（不移动鼠标）。",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "scroll",
        "description": "滚动元素或当前焦点区域。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "pages": {"type": "number", "default": 1},
                "ref": {"type": "string"},
            },
            "required": ["direction"],
        },
    },
    {
        "name": "close_window",
        "description": "关闭窗口（走 WindowPattern.Close）。",
        "inputSchema": {
            "type": "object",
            "properties": {"window": {"type": ["integer", "string"]}},
        },
    },
    {
        "name": "launch_app",
        "description": "启动本地程序（内置列表或 exe/lnk/bat 路径，不再限制白名单）。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "screenshot",
        "description": "仅在树读不到内容（画布/游戏/自绘界面）时显式调用；默认不要用。",
        "inputSchema": {
            "type": "object",
            "properties": {"window": {"type": ["integer", "string"]}, "path": {"type": "string"}},
        },
    },
    {
        "name": "focus_window",
        "description": "把目标窗口提到前台（原始鼠标/键盘输入前会自动做，一般不用单独调）。",
        "inputSchema": {
            "type": "object",
            "properties": {"window": {"type": ["integer", "string"]}},
        },
    },
    {
        "name": "move",
        "description": "把鼠标移到屏幕坐标 (x,y)，不点击。坐标必须来自 snapshot 的 @l,t,r,b。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "window": {"type": ["integer", "string"], "description": "围栏目标；省略=上次启动/快照的窗口"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "click_at",
        "description": (
            "在屏幕坐标 (x,y) 点击。只在元素不支持语义点击时用；坐标必须来自 snapshot 的 @l,t,r,b。"
            "默认会受同步面板里的坐标围栏限制。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "default": 1},
                "modifiers": {"type": "array", "items": {"type": "string"}},
                "window": {"type": ["integer", "string"]},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_at",
        "description": (
            "先点击 (x,y) 再键入文字（Unicode，中文/emoji 都行）。"
            "适合画布、自绘输入框、或 ValuePattern 不可用的位置。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "text": {"type": "string"},
                "window": {"type": ["integer", "string"]},
            },
            "required": ["x", "y", "text"],
        },
    },
    {
        "name": "drag",
        "description": "从 from 拖到 to（屏幕坐标，[x,y]）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "to": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "window": {"type": ["integer", "string"]},
            },
            "required": ["from", "to"],
        },
    },
    {
        "name": "scroll_at",
        "description": "在屏幕坐标 (x,y) 滚动滚轮。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                "pages": {"type": "number", "default": 1},
                "window": {"type": ["integer", "string"]},
            },
            "required": ["x", "y"],
        },
    },
]


def _call_tool(backend: uia.UiaBackend, name: str, args: dict) -> dict:
    if name == "list_windows":
        return {"ok": True, "windows": backend.windows()}
    if name == "snapshot":
        window = args.get("window") or _last_hwnd()
        result = backend.snapshot(
            window=window,
            ref=str(args.get("ref") or ""),
            depth=int(args.get("depth") or 12),
            max_nodes=int(args.get("max_nodes") or 400),
            diff=bool(args.get("diff")),
            chrome=bool(args.get("chrome")),
            with_bounds=bool(args.get("with_bounds", True)),
        )
        if int(result.get("hwnd") or 0):
            _LAST_HWND["value"] = int(result["hwnd"])
        return result
    if name == "click":
        ref = str(args.get("ref") or "")
        result = backend.click(ref)
        if result.get("ok"):
            return result
        rect = backend.element_rect(ref)
        if not rect.get("ok"):
            return result
        prepared = _prepare_coordinates(
            {"window": args.get("window") or _last_hwnd()},
            [(int(rect["x"]), int(rect["y"]))],
        )
        if not prepared.get("ok"):
            return {**result, "fallback": "blocked", "detail": prepared.get("error")}
        return {
            **result,
            "via": "click_at",
            "fallback": desktop_input.click(int(rect["x"]), int(rect["y"])),
        }
    if name == "focus_window":
        target = _target_window(args.get("window"))
        if not target:
            return {"ok": False, "error": "没有目标窗口"}
        result = desktop_input.focus_window(target)
        if result.get("ok"):
            _LAST_HWND["value"] = target
        return result
    if name == "set_value":
        return backend.set_value(str(args.get("ref") or ""), str(args.get("text") or ""))
    if name == "type_text":
        if not bool(_cfg("desktop_allow_synthetic_keys", True)):
            return {"ok": False, "error": "合成键入已在 NHY 面板关闭"}
        ref = str(args.get("ref") or "")
        if not ref:
            target = _target_window(args.get("window"))
            if not target:
                return {"ok": False, "error": "没有目标窗口；传 window，或先用 snapshot/launch 确定窗口"}
            desktop_input.focus_window(target)
        return backend.type_text(str(args.get("text") or ""), ref)
    if name == "press_key":
        if not bool(_cfg("desktop_allow_synthetic_keys", True)):
            return {"ok": False, "error": "合成按键已在 NHY 面板关闭"}
        target = _target_window(args.get("window"))
        if not target:
            return {"ok": False, "error": "没有目标窗口；传 window，或先用 snapshot/launch 确定窗口"}
        desktop_input.focus_window(target)
        return desktop_input.press_key(str(args.get("keys") or ""))
    if name == "focus":
        return backend.focus(str(args.get("ref") or ""))
    if name == "scroll":
        return backend.scroll(
            str(args.get("direction") or "down"),
            float(args.get("pages") or 1),
            str(args.get("ref") or ""),
        )
    if name == "move":
        x, y = _coord_pair(args, "x")
        prepared = _prepare_coordinates(args, [(x, y)])
        if not prepared.get("ok"):
            return prepared
        return desktop_input.move(x, y)
    if name == "click_at":
        x, y = _coord_pair(args, "x")
        prepared = _prepare_coordinates(args, [(x, y)])
        if not prepared.get("ok"):
            return prepared
        return desktop_input.click(
            x, y,
            button=str(args.get("button") or "left"),
            clicks=int(args.get("clicks") or 1),
            modifiers=args.get("modifiers") or [],
        )
    if name == "type_at":
        if not bool(_cfg("desktop_allow_synthetic_keys", True)):
            return {"ok": False, "error": "合成键入已在 NHY 面板关闭"}
        x, y = _coord_pair(args, "x")
        prepared = _prepare_coordinates(args, [(x, y)])
        if not prepared.get("ok"):
            return prepared
        return desktop_input.type_at(x, y, str(args.get("text") or ""))
    if name == "drag":
        x1, y1 = _coord_pair(args, "from")
        x2, y2 = _coord_pair(args, "to")
        prepared = _prepare_coordinates(args, [(x1, y1), (x2, y2)])
        if not prepared.get("ok"):
            return prepared
        return desktop_input.drag(x1, y1, x2, y2, button=str(args.get("button") or "left"))
    if name == "scroll_at":
        x, y = _coord_pair(args, "x")
        prepared = _prepare_coordinates(args, [(x, y)])
        if not prepared.get("ok"):
            return prepared
        return desktop_input.scroll(
            x, y,
            direction=str(args.get("direction") or "down"),
            pages=float(args.get("pages") or 1),
        )
    if name == "close_window":
        return backend.close_window(args.get("window"))
    if name == "launch_app":
        app_name = str(args.get("name") or "")
        if _worker()[0]:
            result = _worker_launch(app_name)
        else:
            result = backend.launch_app(app_name)
        if int(result.get("hwnd") or 0):
            _LAST_HWND["value"] = int(result["hwnd"])
        return result
    if name == "screenshot":
        return backend.screenshot(args.get("window"), str(args.get("path") or ""))
    raise KeyError(f"未知工具：{name}")


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def serve() -> None:
    # MCP 走 UTF-8 JSON-RPC；Windows 默认的 GBK 管道会让中文变成乱码。
    for stream, kwargs in ((sys.stdin, {"errors": "replace"}), (sys.stdout, {"errors": "replace"})):
        try:
            stream.reconfigure(encoding="utf-8", **kwargs)
        except Exception:
            pass
    backend = uia.backend()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            _error(None, -32700, "invalid json")
            continue
        method = str(message.get("method") or "")
        req_id = message.get("id")
        params = message.get("params") or {}
        if req_id is None and method.startswith("notifications/"):
            continue
        try:
            if method == "initialize":
                _result(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                })
            elif method == "tools/list":
                _result(req_id, {"tools": _TOOLS})
            elif method == "tools/call":
                name = str(params.get("name") or "")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                payload = _call_tool(backend, name, args)
                _result(req_id, {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                    "isError": not bool(payload.get("ok", True)),
                })
            elif method == "ping":
                _result(req_id, {})
            elif method in ("shutdown", "exit"):
                _result(req_id, {})
                return
            else:
                _error(req_id, -32601, f"method not found: {method}")
        except KeyError as exc:
            _error(req_id, -32602, str(exc))
        except Exception as exc:  # 工具内部错误按 MCP 约定回给调用方
            _result(req_id, {
                "content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)}],
                "isError": True,
            })


if __name__ == "__main__":
    serve()
