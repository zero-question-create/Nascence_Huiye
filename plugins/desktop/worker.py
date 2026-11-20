# plugins/desktop/worker.py
# ========================================================================
# 虚拟桌面宿主窗口（独立进程）。
#
# - 用系统壁纸铺底，左侧桌面图标，底部任务栏；
# - 启动程序后用 SetParent 把真窗口嵌进宿主（不是浮在上面）；
# - 嵌入失败（UWP/跨 DPI）时退回“移入区域”并标记 embedded=false；
# - 通过 127.0.0.1 的 HTTP 控制接口与插件通信（token 校验）。
#
# 感知仍走 UIA 树，宿主窗口只负责“看起来像一台电脑”。
# ========================================================================

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import math
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
# 直接运行本文件时，脚本目录会排在 sys.path[0]：
# 目录里的模块名（例如 operator）会遮住标准库，先把脚本目录摘掉。
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != SCRIPT_DIR]
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5.QtCore import QPoint, QSize, Qt, QTimer  # noqa: E402
from PyQt5.QtGui import QColor, QIcon, QImage, QPainter, QPixmap  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication,
    QFileIconProvider,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from plugins.desktop import uia  # noqa: E402
from config.api_config import config  # noqa: E402

def _to_qimage(image):
    """PIL.Image → QImage。

    Pillow 12 的 ImageQt 只支持 Qt6；worker 用 PyQt5，所以直接按 BGRA
    原始字节构造，再 copy() 一次让 QImage 自己持有缓冲区。
    """
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    data = image.tobytes("raw", "BGRA")
    qimage = QImage(
        data, image.width, image.height, image.width * 4, QImage.Format_ARGB32
    )
    if qimage.isNull():
        raise RuntimeError("PIL 图像转换为 QImage 失败")
    return qimage.copy()


def _is_shell_window(hwnd: int) -> bool:
    """桌面、任务栏、WorkerW 等 shell 窗口是否禁止搬进工作区。

    默认放行（desktop_allow_shell_windows=True）；关闭开关后恢复保护，
    避免把整个桌面/任务栏误当成普通程序窗口搬走。
    """
    if bool(config.get("desktop_allow_shell_windows", True)):
        return False
    hwnd = int(hwnd or 0)
    if not hwnd:
        return True
    try:
        if hwnd in (int(user32.GetShellWindow() or 0), int(user32.GetDesktopWindow() or 0)):
            return True
    except Exception:
        pass
    try:
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 128)
        cls = buf.value
    except Exception:
        cls = ""
    if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return True
    try:
        title = uia.window_title(hwnd)
    except Exception:
        title = ""
    return title.strip() == "Program Manager"


def _is_blank(image) -> bool:
    try:
        gray = image.convert("L")
        low, high = gray.getextrema()
        return high - low < 4 and high < 8
    except Exception:
        return False

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")


class _DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", wintypes.RECT),
        ("rcSource", wintypes.RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


dwmapi.DwmRegisterThumbnail.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p),
]
dwmapi.DwmRegisterThumbnail.restype = ctypes.c_long
dwmapi.DwmUpdateThumbnailProperties.argtypes = [
    wintypes.HWND, ctypes.POINTER(_DWM_THUMBNAIL_PROPERTIES),
]
dwmapi.DwmUnregisterThumbnail.argtypes = [ctypes.c_void_p]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_WAIT_TIMEOUT = 0x102


def _parent_alive(pid: int) -> bool:
    """父进程（面板）是否还活着；打不开句柄也视为已退出。"""
    if not pid:
        return True
    handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)

_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CHILD = 0x40000000
_WS_POPUP = 0x80000000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020
_SWP_SHOWWINDOW = 0x0040
_SWP_NOACTIVATE = 0x0010
_SW_RESTORE = 9
_SW_MINIMIZE = 6
_GW_OWNER = 4
_GW_HWNDPREV = 3
_GA_ROOT = 2
_VK_LBUTTON = 0x01
_WH_MOUSE_LL = 14
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_LOWLEVELMOUSEPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.SetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetShellWindow.restype = wintypes.HWND
user32.GetDesktopWindow.restype = wintypes.HWND
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, _LOWLEVELMOUSEPROC, wintypes.HANDLE, wintypes.DWORD,
]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.CallNextHookEx.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL

try:
    user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    _HAS_DPI_CTX = True
except Exception:  # pragma: no cover
    _HAS_DPI_CTX = False


def _system_wallpaper() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            path, _ = winreg.QueryValueEx(key, "WallPaper")
        return path if path and os.path.isfile(path) else ""
    except Exception:
        return ""


class DesktopWindow(QWidget):
    def __init__(self, width: int, height: int, wallpaper: str = ""):
        super().__init__()
        self.setWindowTitle("辉夜的工作桌面")
        self.resize(width, height)
        self.setStyleSheet("color:#e8e8e8;")

        self.apps: dict = {}
        self.last_hwnd = 0
        self._backend = uia.backend()
        self._embedded: dict = {}
        self._mirrors: dict = {}
        self._owned: set = set()
        self._app_pids: set = set()
        self._launch_names: set = set()
        self._preexisting_hwnds: set = set()
        self._floating: set = set()
        self._float_syncs = 0
        self._last_pos = None
        self._mirror_timer = QTimer()
        self._mirror_timer.timeout.connect(self._refresh_mirrors)
        self._adopt_timer = QTimer()
        self._adopt_timer.timeout.connect(self._adopt_stray_windows)
        self._adopt_timer.start(800)
        self._mouse_click_seen = False
        self._mouse_hook = None
        self._mouse_hook_hits = 0
        self._focus_debug = {}
        self._mouse_hook_proc = _LOWLEVELMOUSEPROC(self._on_global_mouse)
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._mouse_hook = user32.SetWindowsHookExW(
                _WH_MOUSE_LL, self._mouse_hook_proc, hinst, 0
            )
        except Exception:
            self._mouse_hook = None
        self._floating_timer = QTimer()
        self._floating_timer.timeout.connect(self._sync_floating)
        self._floating_timer.start(350)
        self._wallpaper_path = ""
        self._wallpaper = QPixmap()
        self.set_wallpaper(wallpaper or _system_wallpaper())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(16, 12, 16, 0)
        self._icon_panel = QWidget()
        self._icon_signature = None
        self._icon_panel.setFixedWidth(112)
        self._icon_panel.setStyleSheet("background: transparent;")
        self._icon_layout = QGridLayout(self._icon_panel)
        self._icon_layout.setContentsMargins(0, 0, 0, 0)
        self._icon_layout.setSpacing(6)
        body.addWidget(self._icon_panel, 0, Qt.AlignTop)
        body.addStretch(1)
        root.addLayout(body, 1)

        self._open_row = QHBoxLayout()
        self._open_row.setContentsMargins(16, 2, 16, 2)
        self._open_row.setSpacing(8)
        root.addLayout(self._open_row)

        bar = QFrame()
        bar.setFixedHeight(52)
        bar.setStyleSheet("background:#23262d;border-radius:10px;")
        self._bar_layout = QHBoxLayout(bar)
        self._bar_layout.setContentsMargins(10, 6, 10, 6)
        self._bar_layout.setSpacing(6)
        bar_wrap = QHBoxLayout()
        bar_wrap.setContentsMargins(12, 0, 12, 0)
        bar_wrap.addWidget(bar)
        root.addLayout(bar_wrap)

        self._status = QLabel("就绪")
        self._status.setStyleSheet("color:#cfd6e0;font-size:12px;padding:2px 16px 6px;")
        root.addWidget(self._status)

        self._build_icons()
        self._build_taskbar()

    # ---------- 壁纸 ----------
    def set_wallpaper(self, path: str) -> bool:
        """切换壁纸：空字符串表示回到系统壁纸。"""
        target = str(path or "").strip() or _system_wallpaper()
        pixmap = QPixmap(target) if target else QPixmap()
        if pixmap.isNull() and target:
            # 路径无效时退回系统壁纸，避免桌面变成纯色。
            target = _system_wallpaper()
            pixmap = QPixmap(target) if target else QPixmap()
        self._wallpaper_path = target
        self._wallpaper = pixmap
        self.update()
        return not pixmap.isNull()

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self._wallpaper.isNull():
            scaled = self._wallpaper.scaled(
                self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            x = max(0, (scaled.width() - self.width()) // 2)
            y = max(0, (scaled.height() - self.height()) // 2)
            painter.drawPixmap(-x, -y, scaled)
            painter.fillRect(self.rect(), QColor(10, 12, 16, 55))
        else:
            painter.fillRect(self.rect(), QColor("#1b1d22"))
        painter.end()

    # ---------- 桌面图标 ----------
    def _unique_apps(self):
        seen, result = set(), []
        for name, spec in self._backend.apps().items():
            path = str(spec.get("path") if isinstance(spec, dict) else spec)
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append((str(name), path))
        return result

    def _note_dirs(self) -> list:
        raw = str(config.get("desktop_notes_dirs") or "").strip()
        candidates = []
        if raw:
            for part in raw.replace(";", ",").split(","):
                part = os.path.expanduser(part.strip())
                if part:
                    candidates.append(Path(part))
        else:
            home = Path.home()
            candidates = [
                PROJECT_DIR.parent,
                home / "Desktop",
                home / "Documents",
                home / "Downloads",
                home / "OneDrive" / "Desktop",
                home / "OneDrive" / "Documents",
            ]
        result = []
        seen = set()
        for path in candidates:
            try:
                if not path.is_dir():
                    continue
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                result.append(path)
            except Exception:
                continue
        return result

    def _note_files(self) -> list:
        files = {}
        for directory in self._note_dirs():
            try:
                for path in list(directory.glob("*.txt")) + list(directory.glob("*.TXT")):
                    if not path.is_file() or path.name.startswith("~$"):
                        continue
                    files[str(path.resolve())] = path
            except Exception:
                continue
        items = list(files.values())
        try:
            items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        return items[:8]

    def _icon_button(self, name: str, path: str, note: bool = False):
        from PyQt5.QtCore import QFileInfo

        button = QToolButton()
        shown = name if len(name) <= 14 else name[:13] + "…"
        button.setText(shown)
        button.setToolTip(path)
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        try:
            icon = QFileIconProvider().icon(QFileInfo(path))
            if not icon.isNull():
                button.setIcon(icon)
        except Exception:
            pass
        button.setIconSize(QSize(40, 40))
        button.setFixedSize(96, 78)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(
            "QToolButton{background:transparent;color:#f0f3f7;border:none;"
            "font-size:12px;padding-top:4px;}"
            "QToolButton:hover{background:rgba(255,255,255,0.14);border-radius:8px;}"
        )
        if note:
            button.clicked.connect(lambda _=False, p=path: self.open_note(p))
        else:
            button.clicked.connect(lambda _=False, n=name: self.launch(n))
        button.show()
        return button

    def _refresh_icons(self, force: bool = False):
        notes = self._note_files()
        apps = self._unique_apps()
        signature = (
            tuple((str(p), round(float(p.stat().st_mtime), 1)) for p in notes),
            tuple((name, path) for name, path in apps),
        )
        if not force and signature == self._icon_signature:
            return
        self._icon_signature = signature
        while self._icon_layout.count():
            item = self._icon_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        row = 0
        # 保存的笔记放最上面，保证新保存的文件一眼能看到。
        for path in notes:
            self._icon_layout.addWidget(
                self._icon_button(path.name, str(path), note=True), row, 0
            )
            row += 1
        for name, path in apps:
            self._icon_layout.addWidget(self._icon_button(name, path), row, 0)
            row += 1
        try:
            self._icon_layout.activate()
            self._icon_panel.updateGeometry()
        except Exception:
            pass
        self._icon_labels = [
            self._icon_layout.itemAt(i).widget().text()
            for i in range(self._icon_layout.count())
            if self._icon_layout.itemAt(i).widget() is not None
        ]
        self._icon_geometry = [
            (
                self._icon_layout.itemAt(i).widget().text(),
                self._icon_layout.itemAt(i).widget().geometry().getRect(),
                self._icon_layout.itemAt(i).widget().isVisible(),
            )
            for i in range(self._icon_layout.count())
            if self._icon_layout.itemAt(i).widget() is not None
        ]

    def open_note(self, path: str) -> dict:
        path = str(path or "")
        if not path or not os.path.isfile(path):
            self._status.setText("笔记文件不存在")
            return {"ok": False, "error": "笔记文件不存在"}
        try:
            result = self._backend.launch_app("记事本", args=[path])
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        if not result.get("ok"):
            # 内置表里没有记事本时，仍按系统默认方式打开。
            try:
                os.startfile(path)
                self._status.setText(f"打开笔记：{os.path.basename(path)}")
                return {"ok": True, "path": path, "via": "startfile"}
            except Exception as exc:
                self._status.setText(f"打开笔记失败：{exc}")
                return {"ok": False, "error": str(exc)}
        pid = int(result.get("pid") or 0)
        if pid:
            self._app_pids.add(pid)
        self._launch_names.add("记事本")
        self._status.setText(f"打开笔记：{os.path.basename(path)}")
        return {"ok": True, "path": path, "pid": pid}

    def _build_icons(self):
        self._refresh_icons(force=True)
        self._icon_timer = QTimer()
        self._icon_timer.timeout.connect(self._refresh_icons)
        self._icon_timer.start(2500)

    # ---------- 任务栏 ----------
    def _build_taskbar(self):
        for name, _path in self._unique_apps()[:10]:
            button = QPushButton(str(name))
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton{background:#2f3440;color:#e8e8e8;border:none;"
                "border-radius:6px;padding:5px 9px;font-size:12px;}"
                "QPushButton:hover{background:#3a4150;}"
            )
            button.clicked.connect(lambda _=False, n=str(name): self.launch(n))
            self._bar_layout.addWidget(button)
        tidy = QPushButton("整理窗口")
        tidy.setCursor(Qt.PointingHandCursor)
        tidy.setStyleSheet(
            "QPushButton{background:#1f3b34;color:#cfe9df;border:none;"
            "border-radius:6px;padding:5px 10px;font-size:12px;}"
            "QPushButton:hover{background:#2a4d44;}"
        )
        tidy.clicked.connect(self.tidy)
        self._bar_layout.addWidget(tidy)
        self._bar_layout.addStretch(1)

    def _refresh_open_row(self):
        while self._open_row.count():
            item = self._open_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        live = {name: hwnd for name, hwnd in self.apps.items() if user32.IsWindow(hwnd)}
        self.apps = live
        if not live:
            return
        label = QLabel("已打开：")
        label.setStyleSheet("color:#eef2f7;font-size:12px;background:rgba(20,22,28,0.55);"
                            "border-radius:8px;padding:2px 6px;")
        self._open_row.addWidget(label)
        for name, hwnd in live.items():
            chip = QPushButton(str(name))
            chip.setCursor(Qt.PointingHandCursor)
            chip.setStyleSheet(
                "QPushButton{background:rgba(35,38,45,0.85);color:#dfe5ec;border:none;"
                "border-radius:9px;padding:3px 9px;font-size:12px;}"
                "QPushButton:hover{background:rgba(58,65,80,0.95);}"
            )
            chip.clicked.connect(lambda _=False, h=int(hwnd): self.place(h))
            self._open_row.addWidget(chip)
        self._open_row.addStretch(1)

    # ---------- 区域（子窗口用客户区坐标） ----------
    def area(self):
        # 桌面风格：窗口占工作区约 72%，左侧留出图标、四周露出壁纸。
        bottom = 52 + 26 + 24 + 8
        max_w = max(200, int(self.width()) - 16)
        max_h = max(200, int(self.height()) - bottom - 8)
        w = max(480, int(max_w * 0.72))
        h = max(320, int(max_h * 0.72))
        x = 96 + max(0, (max_w - w) // 2)
        y = 16 + max(0, (max_h - h) // 3)
        return [x, y, w, h]

    # ---------- 启动 / 嵌入 ----------
    def _app_spec(self, name: str) -> dict:
        try:
            table = self._backend.apps()
        except Exception:
            return {}
        wanted = str(name or "").strip().lower()
        for key, spec in table.items():
            if str(key).lower() != wanted:
                continue
            if isinstance(spec, dict):
                return dict(spec)
            return {"path": str(spec or "")}
        # 不在内置表里也放行：直接给 exe/lnk/bat 路径。
        raw = str(name or "").strip().strip('"')
        if raw and os.path.isfile(raw):
            return {"path": os.path.abspath(raw)}
        return {}

    def _launch_mode(self, spec: dict) -> str:
        """auto=有窗口就复用镜像，否则新开；new=总是新开；reuse=只复用镜像。"""
        mode = str((spec or {}).get("launch_mode") or "").strip().lower()
        if not mode:
            # 面板的全局默认跟文件走，worker 长驻时也能看到面板改动。
            try:
                data = json.loads(
                    (PROJECT_DIR / "config" / "api_config.json").read_text(encoding="utf-8-sig")
                )
                mode = str(data.get("desktop_launch_mode") or "").strip().lower()
            except Exception:
                mode = ""
            if not mode:
                mode = str(config.get("desktop_launch_mode") or "auto").strip().lower()
        return mode if mode in ("auto", "new", "reuse") else "auto"

    def _window_mode(self, spec: dict) -> str:
        mode = str((spec or {}).get("window_mode") or "").strip().lower()
        if not mode:
            try:
                data = json.loads(
                    (PROJECT_DIR / "config" / "api_config.json").read_text(encoding="utf-8-sig")
                )
                mode = str(data.get("desktop_window_mode") or "").strip().lower()
            except Exception:
                mode = ""
            if not mode:
                mode = str(config.get("desktop_window_mode") or "float").strip().lower()
        return mode if mode in ("float", "embed") else "float"

    def _reuse_mode(self, spec: dict) -> str:
        mode = str((spec or {}).get("reuse_mode") or "").strip().lower()
        if not mode:
            try:
                data = json.loads(
                    (PROJECT_DIR / "config" / "api_config.json").read_text(encoding="utf-8-sig")
                )
                mode = str(data.get("desktop_reuse_mode") or "").strip().lower()
            except Exception:
                mode = ""
            if not mode:
                mode = str(config.get("desktop_reuse_mode") or "mirror").strip().lower()
        return mode if mode in ("mirror", "float") else "mirror"

    def _find_existing_window(self, name: str, spec: dict) -> int:
        """找已经打开、并且可以镜像的窗口（不改动它）。"""
        exe = str((spec or {}).get("path") or "").strip()
        target = os.path.normcase(os.path.abspath(exe)) if exe else ""
        wanted = str(name or "").strip().lower()
        candidates = []
        for item in uia.list_windows(only_titled=True):
            try:
                hwnd = int(item["hwnd"])
            except (KeyError, TypeError, ValueError):
                continue
            if hwnd == int(self.winId()) or hwnd in self._embedded:
                continue
            if _is_shell_window(hwnd):
                continue
            image = uia.process_image(int(item.get("pid") or 0))
            same_exe = bool(
                target and image and os.path.normcase(os.path.abspath(image)) == target
            )
            title = str(item.get("title") or "")
            same_title = bool(wanted and wanted in title.lower())
            if not same_exe and not (not target and same_title):
                continue
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                continue
            rect = item.get("rect") or [0, 0, 0, 0]
            try:
                area = max(0, int(rect[2]) - int(rect[0])) * max(
                    0, int(rect[3]) - int(rect[1])
                )
            except (IndexError, TypeError, ValueError):
                area = 0
            candidates.append((area, hwnd))
        candidates.sort(reverse=True)
        return candidates[0][1] if candidates else 0

    def _register_launched(self, name: str, hwnd: int) -> str:
        key = str(name)
        existing = self.apps.get(key)
        if existing and int(existing) != hwnd and user32.IsWindow(int(existing)):
            # 同名程序可以开多个窗口：第二次起用带句柄后缀的键。
            key = f"{name} #{hwnd & 0xFFFF:x}"
        self.apps[key] = hwnd
        self.last_hwnd = hwnd
        try:
            self._app_pids.add(uia.window_pid(hwnd))
            self._launch_names.add(str(name or "").strip().lower())
        except Exception:
            pass
        self._refresh_open_row()
        return key

    def launch(self, name: str) -> dict:
        spec = self._app_spec(name)
        mode = self._launch_mode(spec)
        window_mode = self._window_mode(spec)
        reuse_mode = self._reuse_mode(spec)
        self._launch_names.add(str(name or "").strip().lower())
        try:
            self._preexisting_hwnds.update(
                int(item["hwnd"]) for item in uia.list_windows(only_titled=True)
            )
        except Exception:
            pass

        # 复用：默认只镜像；配置 reuse_mode=float 时把真实窗口移进工作区，
        # 这样宿主键鼠才能点击/输入（QQ 登录窗等）。
        if mode in ("auto", "reuse"):
            existing = self._find_existing_window(name, spec)
            if existing:
                if reuse_mode == "float" and window_mode == "float":
                    floating = bool(self._place_over_host(existing, fill=True))
                    if floating:
                        self._floating.add(existing)
                        self._register_launched(name, existing)
                        self._layout_workspace()
                        self._status.setText(f"已移入工作区：{uia.window_title(existing) or name}")
                        return {
                            "ok": True,
                            "hwnd": existing,
                            "embedded": False,
                            "floating": True,
                            "mirrored": False,
                            "mirror_mode": "",
                            "launch_mode": "reuse",
                            "window_mode": window_mode,
                            "reuse_mode": "float",
                            "reused": True,
                            "launched": [],
                        }
                mirrored = self.mirror(existing)
                if mirrored.get("ok"):
                    self._register_launched(name, existing)
                    self._status.setText(f"正在镜像：{uia.window_title(existing) or name}")
                    return {
                        "ok": True,
                        "hwnd": existing,
                        "embedded": False,
                        "floating": False,
                        "mirrored": True,
                        "mirror_mode": mirrored.get("mode") or "capture",
                        "launch_mode": "reuse",
                        "window_mode": window_mode,
                        "reuse_mode": reuse_mode,
                        "reused": True,
                        "launched": [],
                    }
                if mode == "reuse":
                    return {
                        "ok": False,
                        "hwnd": existing,
                        "launch_mode": mode,
                        "error": "已经打开，但窗口无法复制："
                        + str(mirrored.get("reason") or mirrored.get("error") or "未知原因"),
                    }
                # auto：窗口在但复制不了（例如最小化），继续尝试新开。

        if mode == "reuse":
            return {
                "ok": False,
                "launch_mode": mode,
                "error": f"{name} 还没有打开；reuse 模式不会新开进程",
            }

        # 新开：先记录现有窗口，便于识别“启动后新出现”的窗口。
        before = {int(w["hwnd"]) for w in uia.list_windows(only_titled=True)}
        result = self._backend.launch_app(name)
        if not result.get("ok"):
            self._status.setText(f"启动失败：{result.get('error')}")
            return result
        pid = int(result.get("pid") or 0)
        if pid:
            self._app_pids.add(pid)
        hwnd = self._find_launched_window(self._exe_for(name), before, pid, timeout=10.0)
        if not hwnd:
            self._status.setText(f"打开了 {name}，但没找到窗口")
            return {**result, "hwnd": 0, "launch_mode": mode}

        # 新开窗口：float 直接浮在工作区里，宿主键鼠原生可用；
        # embed 才尝试 SetParent 真嵌入，失败再退回 float。
        reused_existing = hwnd in before
        if not reused_existing:
            self._owned.add(hwnd)
        embedded = False
        floating = False
        mirrored = False
        mirror_mode = ""
        if reused_existing:
            # 程序只把旧窗口提到前台：复制旧窗口，不抢用户的原窗口。
            mirrored_result = self.mirror(hwnd)
            mirrored = bool(mirrored_result.get("ok"))
            mirror_mode = str(mirrored_result.get("mode") or "")
            if not mirrored:
                self._place_over_host(hwnd)
        elif window_mode == "float":
            floating = bool(self._place_over_host(hwnd, fill=True))
            if floating:
                self._floating.add(hwnd)
        else:
            embedded = self.embed(hwnd)
            if not embedded:
                floating = bool(self._place_over_host(hwnd, fill=True))
                if floating:
                    self._floating.add(hwnd)
        if not embedded and not floating and not mirrored:
            mirrored_result = self.mirror(hwnd)
            mirrored = bool(mirrored_result.get("ok"))
            mirror_mode = str(mirrored_result.get("mode") or "")
            if not mirrored:
                self._place_over_host(hwnd)

        self._register_launched(name, hwnd)
        if floating or mirrored:
            self._layout_workspace()
        if embedded:
            state = "已嵌入桌面"
        elif floating:
            state = "浮在工作区（可键鼠输入）"
        elif mirrored:
            state = "已镜像到桌面"
        else:
            state = "未能放入工作区，浮在桌面上"
        self._status.setText(f"打开了 {name}（{state}）")
        return {
            **result,
            "hwnd": hwnd,
            "embedded": embedded,
            "floating": floating,
            "mirrored": mirrored,
            "mirror_mode": mirror_mode,
            "launch_mode": mode,
            "window_mode": window_mode,
            "reused": reused_existing,
        }

    def _find_window(self, pid: int, timeout: float = 6.0) -> int:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for item in uia.list_windows(only_titled=True):
                if pid and item["pid"] == pid:
                    return int(item["hwnd"])
            time.sleep(0.2)
        return 0

    def _exe_for(self, name: str) -> str:
        table = self._backend.apps()
        for key, spec in table.items():
            if str(key).lower() == str(name).lower():
                if isinstance(spec, dict):
                    return str(spec.get("path") or "")
                return str(spec or "")
        raw = str(name or "").strip().strip('"')
        if raw and os.path.isfile(raw):
            return os.path.abspath(raw)
        return ""

    def _find_launched_window(self, exe_path: str, before: set, pid: int, timeout: float = 10.0) -> int:
        """找刚启动/刚被激活的窗口。

        浏览器这类程序：启动器进程很快退出，新窗口属于**已有进程**，
        只按新 pid 找必然失败。这里按 exe 路径匹配，并兼容“只把旧窗口
        提到前台”的情况。
        """
        target = os.path.normcase(os.path.abspath(exe_path)) if exe_path else ""
        started = time.time()
        deadline = started + timeout
        while time.time() < deadline:
            windows = uia.list_windows(only_titled=True)
            # 1) 启动后新出现、且属于同一 exe 的窗口
            for item in windows:
                hwnd = int(item["hwnd"])
                if hwnd in before or _is_shell_window(hwnd):
                    continue
                if target:
                    image = uia.process_image(int(item["pid"]))
                    if image and os.path.normcase(os.path.abspath(image)) == target:
                        return hwnd
            # 2) 新进程直接拥有的窗口
            if pid:
                for item in windows:
                    if int(item["pid"]) == pid and not _is_shell_window(int(item["hwnd"])):
                        return int(item["hwnd"])
            # 3) 已有实例只是被提到前台：先给“新窗口”几秒钟时间，避免抢走用户原窗口
            if time.time() - started >= 4.0:
                fg = uia.foreground_window()
                if fg and target and not _is_shell_window(fg):
                    image = uia.process_image(uia.window_pid(fg))
                    if image and os.path.normcase(os.path.abspath(image)) == target:
                        return int(fg)
            time.sleep(0.25)
        return 0

    # ---------- 窗口镜像（宿主正在用的窗口，复制渲染不抢走） ----------
    def _layout_workspace(self) -> None:
        """把浮动窗口和镜像一起平铺，避免镜像被浮动窗口盖住。"""
        floats = [int(h) for h in self._floating if user32.IsWindow(int(h))]
        mirrors = list(self._mirrors.items())
        count = len(floats) + len(mirrors)
        if not count:
            return
        client_x, client_y, w, h = self.area()
        screen_x, screen_y, _, _ = self._area_origin()
        gap = 10
        cols = 1 if count == 1 else 2 if count == 2 else int(math.ceil(math.sqrt(count)))
        rows = int(math.ceil(count / cols))
        cell_w = max(120, (w - gap * (cols - 1)) // cols)
        cell_h = max(90, (h - gap * (rows - 1)) // rows)

        for index, hwnd in enumerate(floats):
            col = index % cols
            row = index // cols
            left = screen_x + col * (cell_w + gap)
            top = screen_y + row * (cell_h + gap)
            user32.SetWindowPos(
                wintypes.HWND(hwnd), wintypes.HWND(0),
                left, top, cell_w, cell_h, _SWP_SHOWWINDOW,
            )

        offset = len(floats)
        for index, (hwnd, item) in enumerate(mirrors):
            slot = offset + index
            col = slot % cols
            row = slot // cols
            left = client_x + col * (cell_w + gap)
            top = client_y + row * (cell_h + gap)
            if item.get("mode") == "dwm":
                dest = int(item.get("dest") or 0)
                if dest and user32.IsWindow(dest):
                    user32.SetWindowPos(
                        wintypes.HWND(dest), wintypes.HWND(0),
                        left, top, cell_w, cell_h, _SWP_SHOWWINDOW,
                    )
                    try:
                        props = _DWM_THUMBNAIL_PROPERTIES()
                        props.dwFlags = 0x1 | 0x4 | 0x8 | 0x10
                        props.rcDestination = wintypes.RECT(0, 0, cell_w, cell_h)
                        props.opacity = 255
                        props.fVisible = True
                        props.fSourceClientAreaOnly = True
                        dwmapi.DwmUpdateThumbnailProperties(dest, ctypes.byref(props))
                    except Exception:
                        pass
            else:
                label = item.get("label")
                if label is not None:
                    label.setGeometry(left, top, cell_w, cell_h)
                    label.show()
                    label.raise_()

    def _raise_mirror(self, hwnd: int) -> None:
        item = self._mirrors.get(int(hwnd or 0))
        if not item:
            return
        if item.get("mode") == "dwm":
            dest = int(item.get("dest") or 0)
            if dest:
                user32.SetWindowPos(
                    wintypes.HWND(dest), wintypes.HWND(0),
                    0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW,
                )
        else:
            label = item.get("label")
            if label is not None:
                label.raise_()

    def mirror(self, hwnd: int) -> dict:
        hwnd = int(hwnd or 0)
        if _is_shell_window(hwnd):
            return {"ok": False, "reason": "shell-window"}
        if not hwnd or not user32.IsWindow(hwnd):
            return {"ok": False, "reason": "no-window"}
        if hwnd in self._embedded or user32.GetParent(hwnd) == int(self.winId()):
            return {"ok": False, "reason": "already-embedded"}
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return {"ok": False, "reason": "not-visible"}
        self.unmirror(hwnd)

        # 1) DWM 缩略图：真正的实时复制，Chromium 也能显示，而且不动原窗口。
        dest = self._create_thumbnail_window(0, 0, 1, 1)
        if dest:
            thumb = ctypes.c_void_p()
            hr = dwmapi.DwmRegisterThumbnail(dest, hwnd, ctypes.byref(thumb))
            if hr == 0:
                self._mirrors[hwnd] = {
                    "mode": "dwm",
                    "dest": dest,
                    "thumb": thumb,
                    "title": uia.window_title(hwnd),
                }
                self._layout_workspace()
                self._status.setText(f"正在镜像：{uia.window_title(hwnd) or hwnd}")
                return {"ok": True, "hwnd": hwnd, "mode": "dwm"}
            user32.DestroyWindow(dest)

        # 2) 退回到抓帧显示
        image = uia.capture_window(hwnd, 2)
        if image is None or _is_blank(image):
            image = uia.capture_window(hwnd, 0)
        if image is None or _is_blank(image):
            return {"ok": False, "reason": "capture-failed"}
        try:
            pixmap = QPixmap.fromImage(_to_qimage(image))
        except Exception as exc:
            return {"ok": False, "reason": "image-convert-failed", "error": str(exc)}
        label = QLabel(self)
        label.setStyleSheet("background:#000;")
        label.setGeometry(0, 0, 1, 1)
        label.setScaledContents(True)
        label.setPixmap(pixmap)
        label.show()
        self._mirrors[hwnd] = {"mode": "capture", "label": label, "title": uia.window_title(hwnd)}
        self._layout_workspace()
        if not self._mirror_timer.isActive():
            self._mirror_timer.start(500)
        self._status.setText(f"正在镜像：{uia.window_title(hwnd) or hwnd}")
        return {"ok": True, "hwnd": hwnd, "mode": "capture"}

    def _create_thumbnail_window(self, x: int, y: int, w: int, h: int) -> int:
        try:
            hinst = kernel32.GetModuleHandleW(None)
            style = 0x40000000 | 0x10000000  # WS_CHILD | WS_VISIBLE
            hwnd = user32.CreateWindowExW(
                0, "STATIC", None, style, int(x), int(y), int(w), int(h),
                wintypes.HWND(int(self.winId())), None, hinst, None,
            )
            return int(hwnd or 0)
        except Exception:
            return 0

    def unmirror(self, hwnd: int) -> None:
        item = self._mirrors.pop(int(hwnd or 0), None)
        if item:
            if item.get("mode") == "dwm":
                try:
                    dwmapi.DwmUnregisterThumbnail(item.get("thumb"))
                except Exception:
                    pass
                try:
                    user32.DestroyWindow(wintypes.HWND(int(item.get("dest") or 0)))
                except Exception:
                    pass
            else:
                label = item.get("label")
                if label is not None:
                    label.deleteLater()
        if not self._mirrors:
            self._mirror_timer.stop()
        else:
            self._layout_workspace()

    def register_embedded(self, hwnd: int) -> None:
        """把外部嵌入进来的窗口记进“已打开”，芯片与概览里能看到。"""
        hwnd = int(hwnd or 0)
        if not hwnd or any(int(value) == hwnd for value in self.apps.values()):
            return
        title = uia.window_title(hwnd) or f"窗口 {hwnd:x}"
        key = title
        if key in self.apps:
            key = f"{title} #{hwnd & 0xFFFF:x}"
        self.apps[key] = hwnd
        self.last_hwnd = hwnd
        self._refresh_open_row()

    def unmirror_all(self) -> None:
        for hwnd in list(self._mirrors):
            self.unmirror(hwnd)

    def _refresh_mirrors(self) -> None:
        for hwnd, item in list(self._mirrors.items()):
            if not user32.IsWindow(hwnd) or hwnd in self._embedded:
                self.unmirror(hwnd)
                continue
            if item.get("mode") == "dwm":
                if not user32.IsWindow(int(item.get("dest") or 0)):
                    self.unmirror(hwnd)
                continue
            image = uia.capture_window(hwnd, 2)
            if image is None or _is_blank(image):
                image = uia.capture_window(hwnd, 0)
            if image is None or _is_blank(image):
                self.unmirror(hwnd)
                continue
            label = item.get("label")
            if label is not None:
                try:
                    label.setPixmap(QPixmap.fromImage(_to_qimage(image)))
                except Exception:
                    self.unmirror(hwnd)

    def _fit_size(self, hwnd: int, fill: bool = True) -> tuple:
        x, y, w, h = self.area()
        if fill:
            return w, h
        rect = uia.window_rect(hwnd)
        width = max(1, int(rect[2]) - int(rect[0]))
        height = max(1, int(rect[3]) - int(rect[1]))
        scale = min(1.0, w / width, h / height)
        return max(200, int(width * scale)), max(120, int(height * scale))

    def _position_child(self, hwnd: int, fill: bool = True, offset: int = 0) -> None:
        x, y, w, h = self.area()
        width, height = self._fit_size(hwnd, fill)
        left = x + max(0, (w - width) // 2) + offset
        top = y + max(0, (h - height) // 2) + offset
        user32.SetWindowPos(
            hwnd, None, left, top, width, height,
            _SWP_FRAMECHANGED | _SWP_SHOWWINDOW,
        )

    def _area_origin(self) -> tuple:
        point = wintypes.POINT(0, 0)
        user32.ClientToScreen(wintypes.HWND(int(self.winId())), ctypes.byref(point))
        x, y, w, h = self.area()
        return int(point.x) + x, int(point.y) + y, w, h

    def _window_inside(self, hwnd: int) -> bool:
        try:
            rect = uia.window_rect(hwnd)
            left, top, w, h = self._area_origin()
            return (
                rect[0] >= left - 4 and rect[1] >= top - 4
                and rect[2] <= left + w + 4 and rect[3] <= top + h + 4
            )
        except Exception:
            return False

    def _on_global_mouse(self, n_code, w_param, l_param):
        try:
            if n_code >= 0 and int(w_param) == _WM_LBUTTONUP:
                self._mouse_hook_hits += 1
                info = ctypes.cast(
                    l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)
                ).contents
                x, y = int(info.pt.x), int(info.pt.y)
                QTimer.singleShot(0, lambda: self._focus_at_screen(x, y))
        except Exception:
            pass
        return user32.CallNextHookEx(self._mouse_hook, n_code, w_param, l_param)

    def _focus_at_screen(self, x: int, y: int) -> None:
        """宿主点进嵌入窗口后，把键盘焦点交给点中的控件。

        SetParent 到另一个进程后，Windows 不会自动把焦点交给子窗口；
        这里用 UIA SetFocus，宿主键盘和输入法才能正常打进记事本等程序。
        """
        try:
            self._focus_debug = {"x": x, "y": y}
            if not self.isVisible():
                self._focus_debug["why"] = "hidden"
                return
            host = int(self.winId())
            rect = uia.window_rect(host)
            if not (rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]):
                self._focus_debug["why"] = "outside"
                return
            hit = int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)
            root = int(user32.GetAncestor(wintypes.HWND(hit), _GA_ROOT) or 0) if hit else 0
            self._focus_debug.update(hit=hit, root=root, host=host)
            if not hit or root != host:
                self._focus_debug["why"] = "hit-not-host"
                return
            result = uia.backend().focus_at(x, y)
            self._focus_debug["result"] = result
            if not result.get("ok"):
                return
            target = int(result.get("hwnd") or 0)
            if not target or target == host:
                return
            if int(user32.GetAncestor(wintypes.HWND(target), _GA_ROOT) or 0) != host:
                return
            if int(result.get("pid") or 0) == os.getpid():
                return
            self._status.setText(f"键盘焦点：{result.get('name') or target}")
        except Exception:
            pass

    def _sync_floating(self) -> None:
        """让浮在工作区里的窗口保持可见，并跟在宿主窗口上方。"""
        if not self._floating:
            return
        self._float_syncs += 1
        host = int(self.winId())
        visible = self.isVisible()
        if visible and any(
            not self._window_inside(hwnd)
            for hwnd in self._floating
            if user32.IsWindow(hwnd) and not user32.IsIconic(hwnd)
        ):
            self._layout_workspace()
        for hwnd in list(self._floating):
            if not user32.IsWindow(hwnd):
                self._floating.discard(hwnd)
                continue
            if not visible:
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
                continue
            if user32.IsIconic(hwnd):
                continue
            if not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            above = int(user32.GetWindow(wintypes.HWND(host), _GW_HWNDPREV) or 0)
            user32.SetWindowPos(
                hwnd, wintypes.HWND(above), 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )

    def _adopt_stray_windows(self) -> None:
        """把新建的弹窗/独立窗口收进工作区（保存框、下载框、计算器之类）。"""
        try:
            if not self.isVisible():
                return
            host = int(self.winId())
            app_hwnds = {int(h) for h in self.apps.values() if user32.IsWindow(int(h))}
            pids = set(self._app_pids)
            for hwnd in app_hwnds:
                pids.add(uia.window_pid(hwnd))
            floating_pids = {
                uia.window_pid(h) for h in self._floating if user32.IsWindow(int(h))
            }
            names = {n for n in self._launch_names if n}
            for item in uia.list_windows(only_titled=True):
                try:
                    hwnd = int(item["hwnd"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not hwnd or hwnd == host:
                    continue
                if _is_shell_window(hwnd):
                    continue
                if hwnd in self._embedded or hwnd in self._mirrors:
                    continue
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    continue
                owner = int(user32.GetWindow(wintypes.HWND(hwnd), _GW_OWNER) or 0)
                pid = int(item.get("pid") or 0)
                title = str(item.get("title") or "")
                lower = title.strip().lower()
                is_owned = owner == host or owner in app_hwnds
                by_pid = pid in pids
                # 短名字（QQ/微信）不用标题匹配；启动前就存在的同名窗口也不碰。
                by_name = hwnd not in self._preexisting_hwnds and any(
                    len(name) >= 3 and (
                        lower == name
                        or lower.startswith(name + " ")
                        or lower.endswith(" - " + name)
                    )
                    for name in names
                )
                if not (is_owned or by_pid or by_name):
                    continue
                if self._window_inside(hwnd):
                    continue
                prefer_float = hwnd in self._floating or pid in floating_pids
                self.adopt(hwnd, fill=False, embed=not prefer_float)
        except Exception:
            pass

    def embed(self, hwnd: int, fill: bool = True) -> bool:
        """把真窗口变成宿主的子窗口；失败返回 False。"""
        hwnd = int(hwnd or 0)
        if _is_shell_window(hwnd):
            return False
        if not hwnd:
            return False
        self.unmirror(hwnd)
        host = int(self.winId())
        style = int(user32.GetWindowLongPtrW(hwnd, _GWL_STYLE))
        parent = user32.GetParent(hwnd)
        if parent == host and (style & _WS_CHILD):
            # 已经是子窗口（例如上次嵌入的弹窗），只调整位置。
            self._position_child(hwnd, fill=fill)
            return True
        old_context = self._set_dpi_context(hwnd)
        try:
            user32.ShowWindow(hwnd, _SW_RESTORE)
            exstyle = int(user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE))
            self._embedded[hwnd] = {
                "parent": parent,
                "style": style,
                "exstyle": exstyle,
            }
            new_style = (style | _WS_CHILD) & ~_WS_POPUP
            user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, new_style)
            user32.SetParent(hwnd, host)
            if user32.GetParent(hwnd) != host:
                self._embedded.pop(hwnd, None)
                user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, style)
                return False
            self._position_child(hwnd, fill=fill)
            self._floating.discard(hwnd)
            return True
        except Exception:
            return False
        finally:
            self._restore_dpi_context(old_context)

    def adopt(self, hwnd: int, fill: bool = False, embed: bool = True) -> bool:
        """把弹窗/独立窗口收进工作区：优先真嵌入，失败就浮动到工作区上方。"""
        hwnd = int(hwnd or 0)
        if _is_shell_window(hwnd):
            return False
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        if hwnd == int(self.winId()) or hwnd in self._mirrors:
            return False
        if embed and self.embed(hwnd, fill=fill):
            return True
        return self._place_over_host(hwnd, fill=fill)

    def _place_over_host(self, hwnd: int, offset: int = 0, fill: bool = True) -> bool:
        """兜底：浮在宿主上方（视觉上在工作区，但不是子窗口）。"""
        if _is_shell_window(int(hwnd or 0)):
            return False
        x, y, w, h = self._area_origin()
        width, height = self._fit_size(hwnd, fill)
        left = x + max(0, (w - width) // 2) + offset
        top = y + max(0, (h - height) // 2) + offset
        try:
            user32.ShowWindow(hwnd, _SW_RESTORE)
            return bool(
                user32.SetWindowPos(
                    hwnd, wintypes.HWND(0),
                    left, top, width, height,
                    _SWP_SHOWWINDOW,
                )
            )
        except Exception:
            return False

    def bring_to_front(self) -> bool:
        """显式“显示工作桌面”时用：抬到普通窗口最前，但不设置 always-on-top。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()
        try:
            hwnd = wintypes.HWND(int(self.winId()))
            flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW
            user32.SetWindowPos(hwnd, wintypes.HWND(0), 0, 0, 0, 0, flags)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            return True
        except Exception:
            return False

    def place(self, hwnd: int, offset: int = 0) -> bool:
        if _is_shell_window(int(hwnd or 0)):
            return False
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        if hwnd in self._mirrors:
            # 镜像是复制件；不要把用户的原窗口拖到宿主上面。
            return bool(self.mirror(hwnd).get("ok"))
        if hwnd in self._embedded:
            x, y, w, h = self.area()
            user32.SetWindowPos(hwnd, None, x + offset, y + offset, w, h, _SWP_SHOWWINDOW)
        else:
            self._place_over_host(hwnd, offset)
        self.last_hwnd = int(hwnd)
        return True

    def _unembed(self, hwnd: int) -> None:
        info = self._embedded.pop(hwnd, None)
        if not info:
            return
        try:
            user32.SetParent(hwnd, info["parent"])
            user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, info["style"])
            user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, info["exstyle"])
            user32.SetWindowPos(
                hwnd, None, 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED,
            )
        except Exception:
            pass

    def unembed_all(self) -> None:
        for hwnd in list(self._embedded):
            self._unembed(hwnd)

    def _uninstall_mouse_hook(self) -> None:
        if self._mouse_hook:
            try:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None

    def release_apps(self) -> int:
        """退出前把程序交还给系统：先解除嵌入/镜像，再最小化，避免突然弹到桌面。"""
        self._uninstall_mouse_hook()
        self.unmirror_all()
        for hwnd in list(self._embedded):
            self._unembed(hwnd)
        released = 0
        for hwnd in list(self.apps.values()):
            # 只收自己启动的程序；复用/镜像的是用户原窗口，不能替用户最小化。
            if hwnd in self._owned and user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, _SW_MINIMIZE)
                released += 1
        self.apps.clear()
        self._owned.clear()
        self._floating.clear()
        return released

    @staticmethod
    def _set_dpi_context(hwnd: int):
        if not _HAS_DPI_CTX:
            return None
        try:
            context = user32.GetWindowDpiAwarenessContext(wintypes.HWND(hwnd))
            return user32.SetThreadDpiAwarenessContext(context)
        except Exception:
            return None

    @staticmethod
    def _restore_dpi_context(old_context):
        if not _HAS_DPI_CTX or old_context is None:
            return
        try:
            user32.SetThreadDpiAwarenessContext(old_context)
        except Exception:
            pass

    def tidy(self) -> dict:
        placed = {}
        for index, (name, hwnd) in enumerate(list(self.apps.items())):
            placed[name] = self.place(int(hwnd), offset=index * 26)
        self._refresh_open_row()
        self._status.setText("把窗口都放回工作区了")
        return placed

    def state(self) -> dict:
        live = {name: hwnd for name, hwnd in self.apps.items() if user32.IsWindow(hwnd)}
        return {
            "hwnd": int(self.winId()),
            "area": self.area(),
            "area_origin": list(self._area_origin()),
            "apps": live,
            "embedded": {name: int(hwnd) in self._embedded for name, hwnd in live.items()},
            "last_hwnd": int(self.last_hwnd),
            "visible": bool(self.isVisible()),
            "wallpaper": self._wallpaper_path,
            "mirrors": [int(hwnd) for hwnd in self._mirrors],
            "floating": [int(hwnd) for hwnd in self._floating],
            "float_syncs": int(self._float_syncs),
            "input_hook": bool(self._mouse_hook),
            "mouse_hook_hits": int(self._mouse_hook_hits),
            "focus_debug": dict(self._focus_debug),
            "icon_labels": list(getattr(self, "_icon_labels", [])),
            "icon_geometry": list(getattr(self, "_icon_geometry", [])),
        }

    def moveEvent(self, event):
        super().moveEvent(event)
        try:
            pos = self.pos()
            if self._last_pos is not None:
                dx = pos.x() - self._last_pos.x()
                dy = pos.y() - self._last_pos.y()
                if dx or dy:
                    for hwnd in list(self._floating):
                        if user32.IsWindow(hwnd):
                            rect = uia.window_rect(hwnd)
                            user32.SetWindowPos(
                                wintypes.HWND(hwnd), wintypes.HWND(0),
                                rect[0] + dx, rect[1] + dy, 0, 0,
                                _SWP_NOSIZE | _SWP_NOZORDER | _SWP_SHOWWINDOW,
                            )
            self._last_pos = pos
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self._layout_workspace()
        except Exception:
            pass

    def closeEvent(self, event):
        # 点右上角 X 不等于“关机”：最小化，程序还留在桌面里。
        # 真正退出由插件 stop() → /shutdown 触发，退出时会把程序最小化交还系统。
        event.ignore()
        self.showMinimized()
        self._status.setText("工作桌面已最小化，里面的程序还在")


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, token: str, window: DesktopWindow):
        super().__init__(address, handler)
        self.token = token
        self.window = window
        self.ui_queue: "queue.Queue" = queue.Queue()
        self._timer = QTimer()
        self._timer.timeout.connect(self._drain)
        self._timer.start(40)

    def _drain(self):
        while True:
            try:
                fn, holder = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                holder["value"] = fn()
            except Exception as exc:  # pragma: no cover
                holder["error"] = str(exc)

    def run_on_ui(self, fn, timeout: float = 15.0):
        holder: dict = {}
        self.ui_queue.put((fn, holder))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "value" in holder or "error" in holder:
                break
            time.sleep(0.02)
        if "error" in holder:
            raise RuntimeError(holder["error"])
        return holder.get("value")


class Handler(BaseHTTPRequestHandler):
    server: ControlServer

    def log_message(self, fmt, *args):
        return

    def _authorized(self) -> bool:
        return self.headers.get("X-Desktop-Token", "") == self.server.token

    def _reply(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        if not self._authorized():
            return self._reply({"ok": False, "error": "unauthorized"}, 401)
        if self.path == "/health":
            return self._reply({"ok": True})
        if self.path == "/state":
            try:
                return self._reply({"ok": True, **self.server.run_on_ui(self.server.window.state)})
            except Exception as exc:
                return self._reply({"ok": False, "error": str(exc)}, 500)
        return self._reply({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._reply({"ok": False, "error": "unauthorized"}, 401)
        body = self._body()
        try:
            if self.path == "/launch":
                name = str(body.get("name") or "")
                result = self.server.run_on_ui(lambda: self.server.window.launch(name), timeout=25)
                return self._reply({"ok": bool(result.get("ok")), **result})
            if self.path == "/place":
                hwnd = int(body.get("hwnd") or 0)
                ok = self.server.run_on_ui(lambda: self.server.window.place(hwnd))
                return self._reply({"ok": bool(ok)})
            if self.path == "/tidy":
                placed = self.server.run_on_ui(self.server.window.tidy)
                return self._reply({"ok": True, "placed": placed})
            if self.path == "/mirror":
                hwnd = int(body.get("hwnd") or 0)
                result = self.server.run_on_ui(lambda: self.server.window.mirror(hwnd))
                return self._reply(result)
            if self.path == "/unmirror":
                hwnd = int(body.get("hwnd") or 0)
                self.server.run_on_ui(lambda: self.server.window.unmirror(hwnd))
                return self._reply({"ok": True})
            if self.path == "/adopt":
                hwnd = int(body.get("hwnd") or 0)
                fill = bool(body.get("fill"))
                ok = self.server.run_on_ui(
                    lambda: self.server.window.adopt(hwnd, fill=fill), timeout=20
                )
                return self._reply({"ok": bool(ok)})
            if self.path == "/embed":
                hwnd = int(body.get("hwnd") or 0)
                ok = self.server.run_on_ui(lambda: self.server.window.embed(hwnd), timeout=20)
                if ok:
                    self.server.run_on_ui(lambda: self.server.window.register_embedded(hwnd))
                return self._reply({"ok": bool(ok)})
            if self.path == "/wallpaper":
                path = str(body.get("path") or "")
                ok = self.server.run_on_ui(lambda: self.server.window.set_wallpaper(path))
                return self._reply({"ok": bool(ok), "path": self.server.window._wallpaper_path})
            if self.path == "/front":
                ok = self.server.run_on_ui(self.server.window.bring_to_front)
                return self._reply({"ok": bool(ok)})
            if self.path == "/show":
                self.server.run_on_ui(self.server.window.showNormal)
                self.server.run_on_ui(self.server.window.raise_)
                self.server.run_on_ui(self.server.window.activateWindow)
                return self._reply({"ok": True})
            if self.path == "/hide":
                self.server.run_on_ui(self.server.window.hide)
                return self._reply({"ok": True})
            if self.path == "/shutdown":
                self.server.run_on_ui(self.server.window.release_apps)
                self._reply({"ok": True})
                threading.Timer(0.2, QApplication.instance().quit).start()
                return
        except Exception as exc:
            return self._reply({"ok": False, "error": str(exc)}, 500)
        return self._reply({"ok": False, "error": "not found"}, 404)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("NHY_DESKTOP_PORT", "0")))
    parser.add_argument("--token", default=os.environ.get("NHY_DESKTOP_TOKEN", ""))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--wallpaper", default=os.environ.get("NHY_DESKTOP_WALLPAPER", ""))
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--state-file", default="")
    args = parser.parse_args()
    token = args.token or os.urandom(8).hex()

    app = QApplication(sys.argv)
    window = DesktopWindow(args.width, args.height, args.wallpaper)
    window.show()

    server = ControlServer(("127.0.0.1", args.port), Handler, token, window)
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, name="DesktopControl", daemon=True).start()

    state_path = Path(args.state_file) if args.state_file else None
    if state_path is not None:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"pid": os.getpid(), "port": port, "token": token}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            state_path = None

    print(f"READY {port}", flush=True)

    # 面板被强行结束时，工作桌面跟着退出，不留下孤儿窗口。
    if args.parent_pid:
        watchdog = QTimer()
        def _check_parent():
            if _parent_alive(args.parent_pid):
                return
            window.release_apps()
            app.quit()
        watchdog.timeout.connect(_check_parent)
        watchdog.start(1000)

    try:
        return int(app.exec_())
    finally:
        window.release_apps()
        if state_path is not None:
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                if int(data.get("pid") or 0) == os.getpid():
                    state_path.unlink()
            except Exception:
                pass
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
