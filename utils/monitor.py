"""统一处理日志接口，由控制面板或标准日志处理器显示。"""

import logging


logger = logging.getLogger("Nascence.Processing")


def monitor_start():
    """保留原调用接口；图形监控已整合进控制面板。"""
    logger.info("处理日志监控已接入控制面板")


def clear_log():
    """日志必须完整保留，因此不再从处理阶段自动清空。"""
    return None


def append_log(text: str):
    """记录完整处理日志，多行内容保持原样。"""
    logger.info("%s", text)
