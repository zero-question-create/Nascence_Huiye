# utils/atomic_io.py
# ========================================================================
# 统一原子写工具。
#
# 目的：任何 JSON/文本状态文件都通过“临时文件 + 落盘 + os.replace”写入，
# 避免进程被强杀或并发读写时留下半截文件。
#
# Windows 上 os.replace 偶发被杀毒/索引服务瞬态占用（WinError 5/32），
# 这类占用转瞬即逝，短重试即可。
# ========================================================================

import json
import os
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:
    fcntl = None

_path_locks = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    key = os.path.abspath(path)
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _path_locks[key] = lock
        return lock


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> bool:
    """原子写文本。成功返回 True，失败返回 False 且不留下半截目标文件。"""
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    lock = _lock_for(path)
    with lock:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            for attempt in range(5):
                try:
                    os.replace(tmp_path, path)
                    return True
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            return False
        except OSError:
            return False
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass


def atomic_write_json(path: str, data, indent: int = 2) -> bool:
    try:
        text = json.dumps(data, ensure_ascii=False, indent=indent)
    except (TypeError, ValueError):
        return False
    return atomic_write_text(path, text)


def atomic_append_line(path: str, line: str, encoding: str = "utf-8") -> bool:
    """追加一行（带进程内锁）。跨进程追加依赖 O_APPEND 的原子性。"""
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    lock = _lock_for(path)
    with lock:
        try:
            os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding=encoding, newline="") as f:
                f.write(line.rstrip("\n") + "\n")
            return True
        except OSError:
            return False


class FileLock:
    """跨进程文件锁（Linux flock / Windows msvcrt）。

    用于保证同一份数据文件只有一个写者。用法：
        with FileLock("run/runtime.lock"):
            ...
    拿不到锁时抛 TimeoutError。
    """

    def __init__(self, path: str, timeout: float = 0.0):
        self.path = os.path.abspath(path)
        self.timeout = max(0.0, float(timeout))
        self._fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        deadline = time.time() + self.timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except (OSError, BlockingIOError):
                if time.time() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"文件锁被占用: {self.path}")
                time.sleep(0.05)

    def release(self):
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
