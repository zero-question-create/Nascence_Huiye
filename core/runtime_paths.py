# core/runtime_paths.py
# ========================================================================
# 运行时数据目录
#
# 默认为 data/test（与原单机版一致）。网关服务端按用户切换为 data/<user>，
# 各数据模块（memory_engine / persistence / message_history / virtual_clock）
# 通过本模块的 file() 解析路径，切换目录后由注册的刷新回调统一重算路径常量。
# ========================================================================

import os

DATA_DIR = os.environ.get("NASCENCE_DATA_DIR", "data/test")

_refreshers = []   # [(callable)]：切换目录后重新计算各模块路径常量


def register(refresher):
    """注册一个路径刷新回调（由各数据模块在导入时调用）。"""
    _refreshers.append(refresher)


def get_data_dir() -> str:
    return DATA_DIR


def set_data_dir(path: str):
    """切换当前数据目录，并刷新所有已注册模块的路径常量。"""
    global DATA_DIR
    DATA_DIR = str(path)
    os.makedirs(DATA_DIR, exist_ok=True)
    for fn in _refreshers:
        fn()


def set_user(username: str, base_dir: str = None):
    """根据用户名切换数据目录：<base_dir>/data/<username>。"""
    if base_dir:
        target = os.path.join(base_dir, "data", username)
    else:
        target = os.path.join("data", username)
    set_data_dir(target)


def file(name: str) -> str:
    """返回数据目录下某文件的完整路径。"""
    return os.path.join(DATA_DIR, name)
