# plugins/desktop/uia.py
# ========================================================================
# Windows UIA 执行器：文本树优先，按引用操作，默认不截图。
#
# - 感知：把窗口的无障碍树压成文本（角色 + 名字 + 值 + 可用动作 + 引用）；
# - 动作：先走 UIA Pattern（不移动鼠标、不抢焦点），最后才考虑坐标输入；
# - 引用：w<hwnd>e<序号>g<世代>，解析时按路径重新定位并校验角色+名字，
#   对不上就提示重新 snapshot，避免误点。
#
# 这里不做任何视觉/OCR；截图只在显式调用 screenshot() 时发生。
# ========================================================================

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import uiautomation as auto
except Exception:  # pragma: no cover - 运行期缺少依赖时给出明确提示
    auto = None

# UIA 属性 ID：判断元素支持哪些动作，比逐个 GetPattern 便宜。
_PROP_INVOKE = 30031
_PROP_VALUE = 30043
_PROP_TOGGLE = 30041
_PROP_SELECTION_ITEM = 30037
_PROP_EXPAND = 30035
_PROP_SCROLL = 30039

_PROJECT_DIR = Path(__file__).resolve().parents[2]
_APPS_FILE = _PROJECT_DIR / "config" / "desktop_apps.json"
_APPS_EXAMPLE = _PROJECT_DIR / "config" / "desktop_apps.example.json"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:  # pragma: no cover
    dwmapi = None

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if dwmapi is not None:
        try:
            # DWMWA_EXTENDED_FRAME_BOUNDS = 9：比 GetWindowRect 更贴近可见区域。
            out = wintypes.RECT()
            hr = dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), ctypes.c_uint(9), ctypes.byref(out), ctypes.sizeof(out)
            )
            if hr == 0:
                return int(out.left), int(out.top), int(out.right), int(out.bottom)
        except Exception:
            pass
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def list_windows(only_titled: bool = True, include_hidden: bool = False) -> List[dict]:
    """枚举顶层窗口；只用 Win32，不触发 UIA。

    include_hidden=True 时也枚举最小化到托盘的隐藏窗口（QQ 主窗口会这样）。
    """
    result: List[dict] = []

    def _callback(hwnd, _lparam):
        try:
            if not include_hidden and not user32.IsWindowVisible(hwnd):
                return True
            title = _window_text(hwnd)
            if only_titled and not title.strip():
                return True
            pid = _window_pid(hwnd)
            left, top, right, bottom = _window_rect(hwnd)
            result.append(
                {
                    "hwnd": int(hwnd),
                    "title": title.strip(),
                    "class": _window_class(hwnd),
                    "pid": pid,
                    "rect": [left, top, right, bottom],
                    "foreground": int(user32.GetForegroundWindow()) == int(hwnd),
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                }
            )
        except Exception:
            pass
        return True

    user32.EnumWindows(_EnumWindowsProc(_callback), 0)
    return result


def foreground_window() -> int:
    return int(user32.GetForegroundWindow())


def window_title(hwnd: int) -> str:
    """窗口标题（含嵌入的子窗口）。"""
    try:
        return _window_text(int(hwnd))
    except Exception:
        return ""


def window_exists(hwnd: int) -> bool:
    try:
        return bool(user32.IsWindow(int(hwnd)))
    except Exception:
        return False


def window_pid(hwnd: int) -> int:
    try:
        return _window_pid(int(hwnd))
    except Exception:
        return 0


def window_rect(hwnd: int):
    try:
        return _window_rect(int(hwnd))
    except Exception:
        return (0, 0, 0, 0)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.GetWindowDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]


def capture_window(hwnd: int, flags: int = 2):
    """用 PrintWindow 把窗口内容抓成 PIL Image；失败返回 None。

    flags=2 是 PW_RENDERFULLCONTENT（Chromium/Electron 需要），
    有些程序反而只认 flags=0，调用方可以两种都试。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    left, top, right, bottom = _window_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    hdc = _user32.GetWindowDC(wintypes.HWND(hwnd))
    if not hdc:
        return None
    mem_dc = _gdi32.CreateCompatibleDC(hdc)
    if not mem_dc:
        _user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
        return None
    bitmap = _gdi32.CreateCompatibleBitmap(hdc, width, height)
    old = _gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = _user32.PrintWindow(wintypes.HWND(hwnd), mem_dc, int(flags))
        if not ok:
            return None
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = _gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)
        if lines == 0:
            return None
        return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).copy()
    finally:
        _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(wintypes.HWND(hwnd), hdc)


def image_blank(image) -> bool:
    try:
        gray = image.convert("L")
        low, high = gray.getextrema()
        return high - low < 4 and high < 8
    except Exception:
        return False


def capture_window_visible(hwnd: int):
    """抓窗口内容；被遮挡时先把窗口提到最前（不抢焦点）再抓。

    仅用于“宿主正在用这个窗口”的兜底分析；返回 PIL Image 或 None。
    """
    image = capture_window(hwnd, 2)
    if image is not None and not image_blank(image):
        return image
    try:
        _user32.SetWindowPos(
            wintypes.HWND(int(hwnd)), wintypes.HWND(0), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010,  # NOSIZE | NOMOVE | NOACTIVATE
        )
        time.sleep(0.15)
        for flags in (2, 0):
            image = capture_window(hwnd, flags)
            if image is not None and not image_blank(image):
                return image
    except Exception:
        pass
    return None


def process_image(pid: int) -> str:
    """进程可执行文件路径；读不到返回空串。

    浏览器这类“启动器进程退出、窗口归已有进程”的程序，必须按 exe 路径
    找窗口，不能只靠新进程的 pid。
    """
    if not pid:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _clean(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


_uia_thread = threading.local()


def _ensure_uia_thread() -> None:
    """uiautomation 需要按线程初始化 COM；认知循环/任务线程都要各自初始化。"""
    if auto is None or getattr(_uia_thread, "ready", False):
        return
    try:
        auto.InitializeUIAutomationInCurrentThread()
    except Exception:
        return
    _uia_thread.ready = True


def _role(control) -> str:
    name = str(getattr(control, "ControlTypeName", "") or "")
    if name.endswith("Control"):
        name = name[: -len("Control")]
    return name or "Element"


def _resolve_program_path(name: str) -> str:
    """把程序名、exe 路径或快捷方式解析成本机可执行路径。

    面板/模型可以给内置表里的名字，也可以直接给 exe / lnk / bat 路径；
    不再限制在 config/desktop_apps.json 的白名单内。
    """
    raw = str(name or "").strip().strip('"')
    if not raw:
        return ""
    candidates = [raw]
    if not os.path.splitext(raw)[1]:
        candidates.extend(raw + ext for ext in (".exe", ".bat", ".cmd", ".lnk"))
    for candidate in candidates:
        try:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        except OSError:
            continue
    found = shutil.which(raw)
    return os.path.abspath(found) if found else ""


class UiaBackend:
    """按窗口维护引用与树快照。线程安全（UIA 调用串行化）。"""

    def __init__(self):
        if auto is None:
            raise RuntimeError("缺少 uiautomation/comtypes，请先安装：pip install uiautomation comtypes")
        _ensure_uia_thread()
        self._lock = threading.RLock()
        self._refs: Dict[str, dict] = {}
        self._gen: Dict[int, int] = {}
        self._last: Dict[int, Dict[str, str]] = {}
        auto.SetGlobalSearchTimeout(2.0)

    # ---------- 窗口 ----------
    @staticmethod
    def windows(only_titled: bool = True) -> List[dict]:
        return list_windows(only_titled=only_titled)

    @staticmethod
    def resolve_window(window: Optional[object] = None) -> int:
        """window 可以是 hwnd、标题片段；None 表示前台窗口。"""
        if window in (None, "", 0, "0"):
            hwnd = foreground_window()
            if not hwnd:
                raise RuntimeError("没有前台窗口")
            return hwnd
        if isinstance(window, int) or (isinstance(window, str) and window.isdigit()):
            hwnd = int(window)
            if not user32.IsWindow(hwnd):
                raise RuntimeError(f"窗口不存在：{window}")
            return hwnd
        needle = str(window).strip().lower()
        for item in list_windows(only_titled=False):
            if needle in item["title"].lower() or needle == str(item["hwnd"]):
                return int(item["hwnd"])
        raise RuntimeError(f"找不到窗口：{window}")

    # ---------- 引用 ----------
    def _ref(self, hwnd: int, index: int) -> str:
        return f"w{hwnd:x}e{index}g{self._gen.get(hwnd, 1)}"

    def _resolve(self, ref: str):
        _ensure_uia_thread()
        with self._lock:
            entry = self._refs.get(str(ref or ""))
        if not entry:
            raise RuntimeError("引用无效或已过期，请重新 snapshot")
        hwnd = int(entry["hwnd"])
        control = auto.ControlFromHandle(hwnd)
        for index in entry["path"]:
            children = control.GetChildren()
            if index >= len(children):
                raise RuntimeError("界面已经变化（引用越界），请重新 snapshot")
            control = children[index]
        expected_role = entry.get("role") or ""
        expected_name = entry.get("name") or ""
        if expected_role and _role(control) != expected_role:
            raise RuntimeError("界面已经变化（角色不符），请重新 snapshot")
        if expected_name and str(control.Name or "").strip() != expected_name:
            raise RuntimeError("界面已经变化（名称不符），请重新 snapshot")
        return control, entry

    def element_rect(self, ref: str) -> dict:
        """元素在屏幕上的矩形与中心点（供坐标点击兜底）。"""
        control, entry = self._resolve(ref)
        try:
            rect = control.BoundingRectangle
        except Exception as exc:
            return {"ok": False, "ref": ref, "error": str(exc)}
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        if right <= left or bottom <= top:
            return {"ok": False, "ref": ref, "error": "元素没有可见区域"}
        return {
            "ok": True,
            "ref": ref,
            "name": entry.get("name") or "",
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "x": (left + right) // 2,
            "y": (top + bottom) // 2,
        }

    def element_info(self, ref: str) -> dict:
        """元素角色/名字，用于判断 copy/paste 目标是否合理。"""
        _control, entry = self._resolve(ref)
        return {
            "ok": True,
            "ref": ref,
            "role": entry.get("role") or "",
            "name": entry.get("name") or "",
        }

    # ---------- 观察 ----------
    def snapshot(
        self,
        window: Optional[object] = None,
        ref: str = "",
        depth: int = 12,
        max_nodes: int = 400,
        diff: bool = False,
        chrome: bool = False,
        with_bounds: bool = False,
    ) -> dict:
        _ensure_uia_thread()
        hwnd = self.resolve_window(window)
        with self._lock:
            self._gen[hwnd] = self._gen.get(hwnd, 1) + 1
            generation = self._gen[hwnd]
        start_root = None
        start_path: List[int] = []
        if ref:
            start_root, entry = self._resolve(ref)
            start_path = list(entry["path"])
            hwnd = int(entry["hwnd"])

        root = start_root or auto.ControlFromHandle(hwnd)
        if root is None:
            raise RuntimeError(f"读不到窗口：{hwnd}")

        lines: List[str] = []
        signatures: Dict[str, str] = {}
        refs: Dict[str, dict] = {}
        counter = {"n": 0, "skipped": 0}
        base_depth = len(start_path)

        if not ref:
            title = _clean(_window_text(hwnd), 60)
            lines.append(
                f"[窗口 w{hwnd:x}] {title} — {_window_class(hwnd)} (pid={_window_pid(hwnd)})"
            )

        def walk(control, path: List[int], level: int, indent: str):
            if level > depth or counter["n"] >= max_nodes:
                counter["skipped"] += 1
                return
            try:
                if level > 0 and bool(getattr(control, "IsOffscreen", False)):
                    return
            except Exception:
                pass
            try:
                children = control.GetChildren()
            except Exception:
                children = []

            show = level > 0 or bool(ref)
            if show:
                role = _role(control)
                # 标题栏按钮、滚动条按钮对“做事”几乎没用，默认折叠省 token。
                if not chrome and role in ("TitleBar", "ScrollBar") and level > 0:
                    return
                name = _clean(getattr(control, "Name", "") or "")
                value = ""
                actions = []
                try:
                    if control.GetPropertyValue(_PROP_VALUE):
                        value = _clean(control.GetValuePattern().Value, 120)
                        actions.append("setValue")
                except Exception:
                    pass
                try:
                    if control.GetPropertyValue(_PROP_INVOKE):
                        actions.append("invoke")
                except Exception:
                    pass
                try:
                    if control.GetPropertyValue(_PROP_TOGGLE):
                        actions.append("toggle")
                except Exception:
                    pass
                try:
                    if control.GetPropertyValue(_PROP_SELECTION_ITEM):
                        actions.append("select")
                except Exception:
                    pass
                try:
                    if control.GetPropertyValue(_PROP_EXPAND):
                        actions.append("expand")
                except Exception:
                    pass
                try:
                    if control.GetPropertyValue(_PROP_SCROLL):
                        actions.append("scroll")
                except Exception:
                    pass
                enabled = True
                try:
                    enabled = bool(control.IsEnabled)
                except Exception:
                    pass

                # 空容器且没有可见文本时折叠，避免树被 Pane/Group 撑爆。
                if not name and not value and not actions and not children:
                    return

                index = counter["n"]
                counter["n"] += 1
                ref_key = self._ref(hwnd, index)
                signatures["/".join(str(x) for x in path) or "root"] = (
                    f"{role}|{name}|{value}|{int(enabled)}"
                )
                refs[ref_key] = {
                    "hwnd": hwnd,
                    "path": list(path),
                    "role": role,
                    "name": name,
                }
                tail = ""
                if value:
                    tail += f' value="{value}"'
                if actions:
                    tail += " actions=" + ",".join(actions)
                if with_bounds and actions:
                    try:
                        rect = control.BoundingRectangle
                        tail += f" @{int(rect.left)},{int(rect.top)},{int(rect.right)},{int(rect.bottom)}"
                    except Exception:
                        pass
                if not enabled:
                    tail += " [disabled]"
                lines.append(f"{indent}{ref_key} {role} \"{name}\"{tail}")

            child_indent = indent + "  " if show else indent
            for idx, child in enumerate(children):
                walk(child, path + [idx], level + 1, child_indent)

        try:
            walk(root, list(start_path), 0, "")
        finally:
            with self._lock:
                self._refs.update(refs)

        if counter["skipped"]:
            lines.append(f"…（还有 {counter['skipped']} 个节点未展开，可 snapshot(ref=…) 看子树）")

        text = "\n".join(lines)
        result = {
            "ok": True,
            "hwnd": hwnd,
            "title": _clean(_window_text(hwnd), 60),
            "generation": generation,
            "nodes": counter["n"],
            "chars": len(text),
            "text": text,
        }
        if not ref:
            previous = self._last.get(hwnd) or {}
            if diff:
                changes = []
                for key, sig in signatures.items():
                    if key not in previous:
                        changes.append(("+", key, sig))
                    elif previous[key] != sig:
                        changes.append(("~", key, sig))
                for key in previous:
                    if key not in signatures:
                        changes.append(("-", key, previous[key]))
                delta = [
                    f"{mark} {key} → {sig}" for mark, key, sig in changes[: max_nodes]
                ]
                result["diff"] = "\n".join(delta)
                result["changed"] = len(changes)
            # 无论是否输出 diff 都记录本次签名，下一步才能对比。
            self._last[hwnd] = signatures
        return result

    # ---------- 动作 ----------
    def click(self, ref: str) -> dict:
        control, entry = self._resolve(ref)
        attempts = [
            ("invoke", lambda: control.GetInvokePattern().Invoke()),
            ("toggle", lambda: control.GetTogglePattern().Toggle()),
            ("select", lambda: control.GetSelectionItemPattern().Select()),
            ("expand", lambda: control.GetExpandCollapsePattern().Expand()),
            ("default", lambda: control.GetLegacyIAccessiblePattern().DoDefaultAction()),
        ]
        errors = []
        for name, action in attempts:
            try:
                action()
                return {"ok": True, "ref": ref, "via": name, "name": entry.get("name") or ""}
            except Exception as exc:
                errors.append(f"{name}:{exc}")
        return {"ok": False, "ref": ref, "error": "元素不支持直接激活", "detail": errors[-3:]}

    def set_value(self, ref: str, text: str) -> dict:
        control, entry = self._resolve(ref)
        try:
            control.GetValuePattern().SetValue(str(text))
            return {"ok": True, "ref": ref, "via": "value", "name": entry.get("name") or ""}
        except Exception as exc:
            errors = [f"value:{exc}"]
        try:
            control.GetLegacyIAccessiblePattern().SetValue(str(text))
            return {"ok": True, "ref": ref, "via": "legacy", "name": entry.get("name") or ""}
        except Exception as exc:
            errors.append(f"legacy:{exc}")
        return {"ok": False, "ref": ref, "error": "这个控件不能直接设值", "detail": errors}

    def append_value(self, ref: str, text: str) -> dict:
        """把文本追加到输入框/文档末尾，保留用户已经写的内容。"""
        control, _entry = self._resolve(ref)
        try:
            old = str(control.GetValuePattern().Value or "")
        except Exception:
            old = ""
        new = old + ("\n" if old and not old.endswith("\n") else "") + str(text or "")
        return self.set_value(ref, new)

    def focus(self, ref: str) -> dict:
        control, entry = self._resolve(ref)
        try:
            control.SetFocus()
            return {"ok": True, "ref": ref, "name": entry.get("name") or ""}
        except Exception as exc:
            return {"ok": False, "ref": ref, "error": str(exc)}

    def focus_at(self, x: int, y: int) -> dict:
        """把键盘焦点交给屏幕坐标处的控件（宿主点击嵌入窗口时用）。"""
        _ensure_uia_thread()
        try:
            control = auto.ControlFromPoint(int(x), int(y))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if control is None:
            return {"ok": False, "error": "这个位置没有控件"}
        try:
            focusable = bool(control.IsKeyboardFocusable)
        except Exception:
            focusable = True
        if not focusable:
            return {"ok": False, "error": "这个控件不接受键盘焦点"}
        hwnd = 0
        try:
            hwnd = int(control.NativeWindowHandle or 0)
        except Exception:
            pass
        try:
            control.SetFocus()
            return {
                "ok": True,
                "x": int(x),
                "y": int(y),
                "hwnd": hwnd,
                "pid": _window_pid(hwnd) if hwnd else 0,
                "name": str(getattr(control, "Name", "") or ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "hwnd": hwnd}

    def clipboard_get(self, limit: int = 4000) -> dict:
        """读取当前 Windows 剪贴板文本；用于复制后回读验证。"""
        _ensure_uia_thread()
        try:
            text = str(auto.GetClipboardText() or "")
        except Exception as exc:
            return {"ok": False, "error": f"读取剪贴板失败：{exc}"}
        return {
            "ok": True,
            "text": text[: max(1, int(limit))],
            "chars": len(text),
        }

    def clipboard_set(self, text: str) -> dict:
        """写入 Windows 剪贴板文本。"""
        _ensure_uia_thread()
        payload = str(text or "")
        if not payload:
            return {"ok": False, "error": "没有要写入剪贴板的文字"}
        try:
            auto.SetClipboardText(payload)
            return {"ok": True, "chars": len(payload)}
        except Exception as exc:
            return {"ok": False, "error": f"写入剪贴板失败：{exc}"}

    def type_text(self, text: str, ref: str = "") -> dict:
        _ensure_uia_thread()
        if ref:
            focused = self.focus(ref)
            if not focused.get("ok"):
                return focused
        try:
            auto.SetClipboardText(str(text))
            auto.SendKeys("{Ctrl}v", waitTime=0.05)
            return {"ok": True, "via": "clipboard", "chars": len(str(text))}
        except Exception as exc:
            return {"ok": False, "error": f"输入失败：{exc}"}

    def press_key(self, keys: str) -> dict:
        _ensure_uia_thread()
        try:
            auto.SendKeys(str(keys), waitTime=0.05)
            return {"ok": True, "keys": str(keys)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def scroll(self, direction: str, pages: float = 1.0, ref: str = "") -> dict:
        _ensure_uia_thread()
        direction = str(direction or "down").lower()
        amount = max(-100.0, min(100.0, float(pages)))
        try:
            if ref:
                control, _entry = self._resolve(ref)
            else:
                control = auto.GetFocusedControl() or auto.ControlFromHandle(foreground_window())
            pattern = control.GetScrollPattern()
            horizontal = direction in ("left", "right")
            sign = -1.0 if direction in ("up", "left") else 1.0
            if horizontal:
                pattern.Scroll(sign * amount / 100.0, 0)
            else:
                pattern.Scroll(0, sign * amount / 100.0)
            return {"ok": True, "direction": direction, "pages": amount}
        except Exception as exc:
            # 没有 ScrollPattern 时退回滚轮语义的键盘翻页。
            try:
                key = {"down": "{PageDown}", "up": "{PageUp}", "left": "{Left}", "right": "{Right}"}[direction]
                auto.SendKeys(key)
                return {"ok": True, "via": "key", "keys": key}
            except Exception:
                return {"ok": False, "error": str(exc)}

    def close_window(self, window: Optional[object] = None) -> dict:
        _ensure_uia_thread()
        hwnd = self.resolve_window(window)
        try:
            control = auto.ControlFromHandle(hwnd)
            control.GetWindowPattern().Close()
            return {"ok": True, "hwnd": hwnd}
        except Exception as exc:
            return {"ok": False, "hwnd": hwnd, "error": str(exc)}

    # ---------- 启动（内置表 + 任意可执行路径） ----------
    @staticmethod
    def apps() -> Dict[str, dict]:
        for path in (_APPS_FILE, _APPS_EXAMPLE):
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    if isinstance(data, dict):
                        return data
            except Exception:
                continue
        return {}

    def launch_app(self, name: str, args: Optional[List[str]] = None) -> dict:
        table = self.apps()
        key = str(name or "").strip().lower()
        spec = None
        for app_name, entry in table.items():
            if str(app_name).lower() == key:
                spec = entry if isinstance(entry, dict) else {"path": entry}
                break
        if spec is None:
            # 不再用白名单拦截：允许直接给 exe / lnk / bat 路径，
            # 也允许 PATH 里能解析到的程序名（notepad、calc 等）。
            exe = _resolve_program_path(name)
            if not exe:
                return {"ok": False, "error": f"找不到程序：{name}"}
            spec = {"path": exe}
        exe = str(spec.get("path") or "").strip()
        if not exe or not os.path.exists(exe):
            return {"ok": False, "error": f"程序路径不存在：{exe}"}
        cmd = [exe] + [str(x) for x in (args if args is not None else (spec.get("args") or []))]
        try:
            if exe.lower().endswith(".lnk"):
                # .lnk 交给 ShellExecute，CreateProcess 不能直接执行。
                os.startfile(exe)
                return {"ok": True, "launched": [exe], "pid": 0, "via": "startfile"}
            proc = subprocess.Popen(cmd, cwd=str(spec.get("cwd") or os.path.dirname(exe) or "."))
            return {"ok": True, "launched": cmd, "pid": int(proc.pid)}
        except Exception as exc:
            return {"ok": False, "error": f"启动失败：{exc}"}

    # ---------- 截图（仅显式调用） ----------
    def screenshot(self, window: Optional[object] = None, path: str = "") -> dict:
        hwnd = self.resolve_window(window)
        left, top, right, bottom = _window_rect(hwnd)
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
            if path:
                image.save(path)
            return {
                "ok": True,
                "hwnd": hwnd,
                "size": [right - left, bottom - top],
                "saved": path or "",
            }
        except Exception as exc:
            return {"ok": False, "hwnd": hwnd, "error": str(exc)}


BACKEND_LOCK = threading.RLock()
_BACKEND: Optional[UiaBackend] = None


def backend() -> UiaBackend:
    global _BACKEND
    with BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = UiaBackend()
        return _BACKEND
