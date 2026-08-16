# core/win_job.py
# ========================================================================
# 单一 Windows Job Object（进程生命周期统一托管）
#
# 目标：主进程无论以何种方式退出（优雅退出 / 信号 / 强杀），所有子进程
# ——用户 Worker、llama-server——都随之消亡，绝不残留孤儿进程。
#
# 实现要点：
#   1. 全进程共享唯一的 Job Object（模块级单例），user_worker 与
#      model_backend 都通过本模块把子进程绑定到同一个 Job。
#   2. KILL_ON_JOB_CLOSE：Job 对象句柄全部关闭（即主进程退出）时，
#      操作系统自动终止 Job 内所有进程。
#   3. 绑定失败时明确记录日志（含错误码），不静默吞掉，便于排查。
# ========================================================================

import ctypes
import logging
import sys

logger = logging.getLogger("WinJob")

_JOB = None  # 主进程持有的 Job Object 句柄（模块级单例）


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_uint64),
        ('WriteOperationCount', ctypes.c_uint64),
        ('OtherOperationCount', ctypes.c_uint64),
        ('ReadTransferCount', ctypes.c_uint64),
        ('WriteTransferCount', ctypes.c_uint64),
        ('OtherTransferCount', ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', _IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryLimit', ctypes.c_size_t),
        ('PeakJobMemoryLimit', ctypes.c_size_t),
    ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


def get_job():
    """返回共享 Job Object 句柄（惰性创建，线程安全由 GIL 保证）。"""
    global _JOB
    if _JOB is not None:
        return _JOB
    if sys.platform != "win32":
        return None
    try:
        _JOB = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if not _JOB:
            logger.error("CreateJobObjectW 失败（err=%s），子进程将无法随主进程自动回收",
                         ctypes.windll.kernel32.GetLastError())
            _JOB = None
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = ctypes.windll.kernel32.SetInformationJobObject(
            _JOB,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.error("SetInformationJobObject 失败（err=%s），KILL_ON_JOB_CLOSE 未生效",
                         ctypes.windll.kernel32.GetLastError())
    except Exception as e:
        logger.error("初始化 Windows Job Object 失败: %s", e)
        _JOB = None
    return _JOB


def assign_process(process_like):
    """把子进程（multiprocessing.Process 或 subprocess.Popen）绑定到共享 Job。

    绑定成功后，子进程将随主进程退出而自动终止。
    绑定失败时记录明确日志（含错误码），不静默吞掉。
    """
    job = get_job()
    if job is None or process_like is None:
        return False
    handle = getattr(process_like, "_handle", None)
    if handle is None:
        pid = getattr(process_like, "pid", None)
        if not pid:
            return False
        # 无进程句柄时按 PID 打开再绑定（仅作兜底，正常路径应直接拿到句柄）
        handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0001, False, int(pid))  # PROCESS_QUERY_INFORMATION|PROCESS_TERMINATE
        if not handle:
            logger.warning("OpenProcess(%s) 失败（err=%s），无法绑定 Job", pid,
                           ctypes.windll.kernel32.GetLastError())
            return False
        try:
            return assign_handle(handle)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    return assign_handle(handle)


def assign_handle(handle):
    """按进程句柄绑定到共享 Job。"""
    job = get_job()
    if job is None:
        return False
    try:
        ok = ctypes.windll.kernel32.AssignProcessToJobObject(job, int(handle))
        if not ok:
            logger.warning("AssignProcessToJobObject 失败（err=%s），该子进程可能无法随主进程回收",
                           ctypes.windll.kernel32.GetLastError())
            return False
        return True
    except Exception as e:
        logger.warning("AssignProcessToJobObject 异常: %s", e)
        return False


def assign_pid(pid):
    """按 PID 打开进程句柄并绑定到共享 Job（用于没有进程句柄的辅助进程，
    例如 multiprocessing 内部启动的 resource_tracker）。"""
    if get_job() is None or not pid:
        return False
    try:
        # PROCESS_QUERY_INFORMATION(0x0400) | PROCESS_TERMINATE(0x0001)
        handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0001, False, int(pid))
        if not handle:
            logger.warning("OpenProcess(%s) 失败（err=%s），无法绑定 Job", pid,
                           ctypes.windll.kernel32.GetLastError())
            return False
        try:
            return assign_handle(handle)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception as e:
        logger.warning("assign_pid(%s) 异常: %s", pid, e)
        return False
