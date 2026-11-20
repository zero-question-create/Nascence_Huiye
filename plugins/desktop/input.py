# plugins/desktop/input.py
# ========================================================================
# 虚拟鼠标 / 键盘：SendInput 原语。
#
# - 语义点击由 UIA Pattern 负责（不动光标）；这里只做“坐标点击/键入”兜底；
# - 坐标统一是屏幕坐标；嵌入窗口的 UIA 矩形也是屏幕坐标，可直接用；
# - 原始输入会移动真实光标并依赖前台窗口，调用方需先 focus_window。
# ========================================================================

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_WHEEL = 0x0800

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_SW_RESTORE = 9

if not hasattr(wintypes, "ULONG_PTR"):
    wintypes.ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL


def _send(items) -> bool:
    if not items:
        return True
    array = (_INPUT * len(items))(*items)
    sent = user32.SendInput(len(items), array, ctypes.sizeof(_INPUT))
    return int(sent) == len(items)


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    item = _INPUT(type=INPUT_MOUSE)
    item.union.mi = _MOUSEINPUT(dx, dy, data, flags, 0, 0)
    return item


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    item = _INPUT(type=INPUT_KEYBOARD)
    item.union.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


def _normalized(x: int, y: int) -> tuple:
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    dx = int(round((int(x) - vx) * 65535 / max(1, vw - 1)))
    dy = int(round((int(y) - vy) * 65535 / max(1, vh - 1)))
    return dx, dy


def move(x: int, y: int) -> dict:
    dx, dy = _normalized(x, y)
    ok = _send([_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx, dy)])
    return {"ok": bool(ok), "x": int(x), "y": int(y)}


def focus_window(hwnd: int) -> dict:
    try:
        if user32.IsIconic(wintypes.HWND(int(hwnd))):
            user32.ShowWindow(wintypes.HWND(int(hwnd)), _SW_RESTORE)
            time.sleep(0.15)
        ok = user32.SetForegroundWindow(wintypes.HWND(int(hwnd)))
        time.sleep(0.1)
        return {"ok": bool(ok), "hwnd": int(hwnd)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    "l": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "r": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "m": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

_MODIFIERS = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "super": 0x5B,
}

_KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "insert": 0x2D, "capslock": 0x14, "printscreen": 0x2C,
}


def _vk_for(token: str) -> int:
    token = str(token or "").strip().lower()
    if token in _MODIFIERS:
        return _MODIFIERS[token]
    if token in _KEYS:
        return _KEYS[token]
    if len(token) == 1:
        char = token.upper()
        if "A" <= char <= "Z" or "0" <= char <= "9":
            return ord(char)
    if token.startswith("f") and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    if token in ("+", "-", "=", ",", ".", "/", ";", "'", "[", "]", "\\", "`"):
        return ord(token.upper())
    return 0


def click(x: int, y: int, button: str = "left", clicks: int = 1, modifiers: list = None) -> dict:
    down, up = _BUTTONS.get(str(button or "left").lower(), _BUTTONS["left"])
    try:
        count = max(1, min(3, int(clicks)))
    except (TypeError, ValueError):
        count = 1
    mods = [m for m in (modifiers or []) if str(m).lower() in _MODIFIERS]
    items = []
    for mod in mods:
        items.append(_key(_MODIFIERS[str(mod).lower()]))
    move(x, y)
    for _ in range(count):
        items.append(_mouse(down))
        items.append(_mouse(up))
        time.sleep(0.04)
    for mod in reversed(mods):
        items.append(_key(_MODIFIERS[str(mod).lower()], flags=KEYEVENTF_KEYUP))
    ok = _send(items)
    return {"ok": bool(ok), "x": int(x), "y": int(y), "button": button, "clicks": count}


def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.4, button: str = "left") -> dict:
    down, up = _BUTTONS.get(str(button or "left").lower(), _BUTTONS["left"])
    move(x1, y1)
    time.sleep(0.05)
    _send([_mouse(down)])
    steps = max(4, min(30, int(float(duration or 0.4) * 40)))
    for index in range(1, steps + 1):
        ratio = index / steps
        move(int(x1 + (x2 - x1) * ratio), int(y1 + (y2 - y1) * ratio))
        time.sleep(max(0.0, float(duration or 0.4) / steps))
    _send([_mouse(up)])
    return {"ok": True, "from": [int(x1), int(y1)], "to": [int(x2), int(y2)]}


def scroll(x: int, y: int, direction: str = "down", pages: float = 1.0) -> dict:
    try:
        amount = max(1.0, min(20.0, float(pages)))
    except (TypeError, ValueError):
        amount = 1.0
    direction = str(direction or "down").lower()
    delta = -120 * amount if direction == "down" else 120 * amount
    move(x, y)
    ok = _send([_mouse(MOUSEEVENTF_WHEEL, data=int(delta))])
    return {"ok": bool(ok), "x": int(x), "y": int(y), "direction": direction, "pages": amount}


def type_text(text: str, interval: float = 0.0) -> dict:
    """Unicode 输入：中文、emoji 都不依赖输入法。"""
    payload = str(text or "")
    if not payload:
        return {"ok": False, "error": "没有要输入的内容"}
    units = payload.encode("utf-16-le")
    items = []
    for index in range(0, len(units), 2):
        code = int.from_bytes(units[index:index + 2], "little")
        items.append(_key(scan=code, flags=KEYEVENTF_UNICODE))
        items.append(_key(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if interval:
            time.sleep(interval)
    ok = _send(items)
    return {"ok": bool(ok), "chars": len(payload)}


def press_key(keys: str) -> dict:
    tokens = [token for token in str(keys or "").replace(" ", "").split("+") if token]
    if not tokens:
        return {"ok": False, "error": "没有按键"}
    vks = []
    for token in tokens:
        vk = _vk_for(token)
        if not vk:
            return {"ok": False, "error": f"不认识的按键：{token}"}
        vks.append(vk)
    items = [_key(vk) for vk in vks]
    items.extend(_key(vk, flags=KEYEVENTF_KEYUP) for vk in reversed(vks))
    ok = _send(items)
    return {"ok": bool(ok), "keys": str(keys)}


def type_at(x: int, y: int, text: str) -> dict:
    clicked = click(x, y)
    typed = type_text(text)
    return {"ok": bool(clicked.get("ok") and typed.get("ok")), "click": clicked, "type": typed}
