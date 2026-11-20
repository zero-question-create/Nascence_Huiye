# plugins/desktop/worker_client.py
# ========================================================================
# 插件侧的虚拟桌面进程客户端：启动/停止 worker.py，转发控制请求。
# 只走 127.0.0.1 + 一次性 token，不对外暴露。
# ========================================================================

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = Path(__file__).resolve().parent / "worker.py"
STATE_FILE = PROJECT_DIR / "run" / "desktop_worker.json"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_WAIT_TIMEOUT = 0x102


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    handle = _kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(handle)


def _worker_python() -> str:
    """优先用 venv 里的 Python：PyQt5/uiautomation 都装在那里。

    面板有时会以系统 Python 运行（例如 multiprocessing 的 base executable），
    直接用 sys.executable 拉起 worker 会缺 PyQt5。
    """
    candidate = PROJECT_DIR / "venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


class DesktopWorker:
    def __init__(self, width: int = 1280, height: int = 800, wallpaper: str = ""):
        self.token = os.urandom(8).hex()
        self.width = int(width)
        self.height = int(height)
        self.wallpaper = str(wallpaper or "")
        self.proc = None
        self.port = 0
        self.adopted = False
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        if self.adopted:
            return self.health()
        return self.proc is not None and self.proc.poll() is None

    # ---------- 复用已有工作桌面 ----------
    def adopt(self) -> bool:
        """如果上一次面板留下的 worker 还活着，直接接管它，避免再开第二个窗口。"""
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            port = int(data.get("port") or 0)
            token = str(data.get("token") or "")
        except Exception:
            return False
        if not (pid and port and token) or not _pid_alive(pid):
            self._clear_state()
            return False
        self.port = port
        self.token = token
        self.proc = None
        self.adopted = True
        if self.health():
            return True
        self.port = 0
        self.token = ""
        self.adopted = False
        return False

    @staticmethod
    def _clear_state() -> None:
        try:
            STATE_FILE.unlink()
        except OSError:
            pass

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 12.0) -> bool:
        with self._lock:
            if self.running:
                return True
            if self.adopt():
                return True
            env = os.environ.copy()
            env["NHY_DESKTOP_TOKEN"] = self.token
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            try:
                self.proc = subprocess.Popen(
                    [
                        _worker_python(), "-X", "utf8", str(WORKER_SCRIPT),
                        "--port", "0",
                        "--width", str(self.width),
                        "--height", str(self.height),
                        "--wallpaper", self.wallpaper,
                        "--parent-pid", str(os.getpid()),
                        "--state-file", str(STATE_FILE),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    cwd=str(PROJECT_DIR),
                    creationflags=flags,
                )
            except Exception:
                self.proc = None
                return False

            line = self._read_ready(timeout)
            if not line.startswith("READY"):
                self.stop()
                return False
            try:
                self.port = int(line.split()[1])
            except (IndexError, ValueError):
                self.stop()
                return False
            return self.health()

    def _read_ready(self, timeout: float) -> str:
        box: "queue.Queue[str]" = queue.Queue()

        def _reader():
            try:
                box.put(self.proc.stdout.readline() or "")
            except Exception:
                box.put("")

        threading.Thread(target=_reader, daemon=True).start()
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            return ""

    def stop(self) -> None:
        with self._lock:
            if self.adopted:
                try:
                    self._request("POST", "/shutdown", {}, timeout=2.0)
                except Exception:
                    pass
                self._clear_state()
                self.adopted = False
                self.port = 0
                return
            if self.proc is None:
                self._clear_state()
                return
            try:
                self._request("POST", "/shutdown", {}, timeout=2.0)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3.0)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            self.proc = None
            self.port = 0
            self._clear_state()

    # ---------- 控制 ----------
    def _request(self, method: str, path: str, payload=None, timeout: float = 5.0) -> dict:
        if not self.port:
            raise RuntimeError("worker 还没有启动")
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method != "GET" else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={
                "X-Desktop-Token": self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # worker 的 500 里带的是具体异常文本；保留它，别只报 HTTP 状态。
            body = exc.read().decode("utf-8", "replace")
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
            return {"ok": False, "error": body or str(exc)}

    def health(self) -> bool:
        try:
            return bool(self._request("GET", "/health", timeout=2.0).get("ok"))
        except Exception:
            return False

    def state(self) -> dict:
        try:
            return self._request("GET", "/state")
        except Exception:
            return {}

    def launch(self, name: str) -> dict:
        try:
            return self._request("POST", "/launch", {"name": name}, timeout=20.0)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def place(self, hwnd: int) -> bool:
        try:
            return bool(self._request("POST", "/place", {"hwnd": int(hwnd)}).get("ok"))
        except Exception:
            return False

    def tidy(self) -> dict:
        try:
            return self._request("POST", "/tidy", {})
        except Exception:
            return {"ok": False}

    def set_wallpaper(self, path: str) -> dict:
        try:
            return self._request("POST", "/wallpaper", {"path": str(path or "")})
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def mirror(self, hwnd: int) -> dict:
        try:
            return self._request("POST", "/mirror", {"hwnd": int(hwnd)}, timeout=10.0)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def unmirror(self, hwnd: int) -> bool:
        try:
            return bool(self._request("POST", "/unmirror", {"hwnd": int(hwnd)}).get("ok"))
        except Exception:
            return False

    def embed_window(self, hwnd: int) -> bool:
        try:
            return bool(self._request("POST", "/embed", {"hwnd": int(hwnd)}, timeout=20).get("ok"))
        except Exception:
            return False

    def adopt_window(self, hwnd: int, fill: bool = False) -> bool:
        try:
            return bool(self._request(
                "POST", "/adopt", {"hwnd": int(hwnd), "fill": bool(fill)}, timeout=20
            ).get("ok"))
        except Exception:
            return False

    def show(self) -> bool:
        try:
            return bool(self._request("POST", "/show", {}).get("ok"))
        except Exception:
            return False

    def front(self) -> bool:
        """显式显示并置前（“显示工作桌面”用）。"""
        try:
            return bool(self._request("POST", "/front", {}).get("ok"))
        except Exception:
            return False

    def hide(self) -> bool:
        try:
            return bool(self._request("POST", "/hide", {}).get("ok"))
        except Exception:
            return False
