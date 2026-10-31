# plugins/qq_napcat/runtime.py
# ========================================================================
# 功能：QQ 插件运行时（NapCat 反向 WebSocket 服务端）
# 包含：白名单、姓名映射、CQ码清洗、@/引用解析、强制回复、门控静默、
#       NapCat 身份自动获取（get_login_info）
#
# 本文件是原根目录 qq_bot.py 的实现。迁入插件后，根目录 qq_bot.py
# 仅作为兼容层转发模块属性，控制面板与既有测试无需改动。
# ========================================================================

import asyncio
import base64
import json
import re
import threading
import websockets
import logging
import aiohttp
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import time
import random
from collections import deque
import datetime
import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
for d in [LOG_DIR, PROJECT_DIR / "data" / "test"]:
    d.mkdir(parents=True, exist_ok=True)

_log_file = LOG_DIR / "qq_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(str(_log_file), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

_is_shutting_down = False  # 标志是否处于优雅关停过程中

class _ShutdownHandshakeFilter(logging.Filter):
    """过滤关服瞬态由服务端主动关闭导致的握手失败噪音频出。"""
    def filter(self, record):
        if _is_shutting_down and ("opening handshake failed" in record.getMessage() or "server closing" in record.getMessage()):
            return False
        return True

logging.getLogger("websockets.server").addFilter(_ShutdownHandshakeFilter())

if sys.platform == "linux":
    local_lib = RUN_DIR / "lib" / "usr" / "lib" / "x86_64-linux-gnu"
    if local_lib.is_dir():
        os.environ.setdefault("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = f"{local_lib}:{os.environ['LD_LIBRARY_PATH']}".rstrip(":")
    local_qt = RUN_DIR / "qt5-plugins"
    if (local_qt / "platforminputcontexts").is_dir():
        os.environ["QT_PLUGIN_PATH"] = str(local_qt)
    os.environ.setdefault("QT_IM_MODULE", "fcitx")
    os.environ.setdefault("GTK_IM_MODULE", "fcitx")
    os.environ.setdefault("XMODIFIERS", "@im=fcitx")

from core.llm_interface import describe_image_from_path, describe_audio_from_path, describe_video_from_path, decompose_input
from utils.event_bus import BUS
from utils.metrics_probe import PROBE
from utils.atomic_io import atomic_write_json
from plugins.chat_store import ChatStore

# 导入你现有的核心模块
from core.cognition import inject_message_keywords, inject_keywords, cognitive_loop, extract_keywords_jieba, MODE_HALF_LIFE
from core.memory_engine import create_memory, access_memory, pathfind_activation, retrieve_similar, memories
from core.virtual_clock import clock
from core.biorhythm import BIORHYTHM
from config.constants import BOT_NAME
from config.api_config import config

try:
    from . import rich_message
except ImportError:  # 直接以脚本方式运行 runtime.py 时
    import rich_message

# ---------- 配置常量 ----------
CONFIG_PATH = str(PROJECT_DIR / "config" / "qq_manifest.json")
CONFIG_EXAMPLE_PATH = str(PROJECT_DIR / "config" / "qq_manifest.example.json")
IDENTITY_FILE = str(PROJECT_DIR / "data" / "test" / "qq_identity.json")
WS_HOST = os.environ.get("WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("WS_PORT", 6700))  # NapCat 配置中填写的端口
WS_PATH = "/ws"


def _cfg_str(key, fallback):
    """读取最新配置并返回字符串值（支持运行时热更新）。"""
    try:
        value = config.get(key)
    except Exception:
        value = None
    return str(value or fallback)


def _cfg_int(key, fallback, minimum=None, maximum=None):
    """读取最新配置并夹成整数（支持运行时热更新）。"""
    try:
        value = int(config.get(key, fallback))
    except (TypeError, ValueError):
        value = fallback
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


# ========== QQ 身份（与意识体名字分离）==========
# BOT_NAME 是意识体自己的名字（记忆与思考里只用它）；
# QQ 昵称/QQ 号是她在 QQ 这个平台上的“账号身份”，连接后由 NapCat 的
# get_login_info 自动获取，不从后台配置里读名字。
_login_info = {"user_id": "", "nickname": "", "source": ""}
_identity_lock = threading.RLock()


def _load_identity_file():
    """读取上次自动获取的 QQ 身份，便于未连接时面板仍能显示。"""
    try:
        with open(IDENTITY_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _identity_lock:
                _login_info["user_id"] = str(data.get("user_id") or "").strip()
                _login_info["nickname"] = str(data.get("nickname") or "").strip()
                _login_info["source"] = str(data.get("source") or "").strip()
    except (OSError, json.JSONDecodeError):
        pass


def _save_identity_file():
    try:
        with _identity_lock:
            data = dict(_login_info)
        os.makedirs(os.path.dirname(IDENTITY_FILE), exist_ok=True)
        atomic_write_json(IDENTITY_FILE, data)
    except Exception as e:
        logger.debug(f"[QQ身份] 保存失败: {e}")


def get_bot_qq() -> str:
    """机器人 QQ 号：NapCat 自动获取的优先，未连接时用配置兜底。"""
    with _identity_lock:
        auto = str(_login_info.get("user_id") or "").strip()
    return auto or _cfg_str("bot_qq", "123456")


def get_bot_qq_name() -> str:
    """机器人在 QQ 侧自动获取的昵称；未获取到返回空字符串。

    注意：这里绝不回落到 BOT_NAME。意识体名字与 QQ 昵称是两个概念，
    调用方需要显示 QQ 端身份时应显式处理空值。
    """
    with _identity_lock:
        return str(_login_info.get("nickname") or "").strip()


def get_qq_identity() -> dict:
    """面板/日志用的身份快照。"""
    with _identity_lock:
        data = dict(_login_info)
    return {
        "user_id": str(data.get("user_id") or "").strip() or _cfg_str("bot_qq", ""),
        "nickname": str(data.get("nickname") or "").strip(),
        "source": str(data.get("source") or "").strip() or ("config" if not data.get("user_id") else "napcat"),
        "consciousness_name": BOT_NAME,
    }


def service_status() -> dict:
    """QQ 插件运行状态（供 registry / 面板聚合）。"""
    return {
        "connected": _napcat_websocket is not None,
        "identity": get_qq_identity(),
        "active_group_id": get_active_group_id(),
    }


# ========== 软件状态与读写 ==========
def _app_context():
    # 延迟导入：core.apps 会反向使用 plugins.registry，避免模块级循环。
    from core.apps import APP_CONTEXT
    return APP_CONTEXT


def _conversation_ids() -> list:
    """稳定的对话顺序（按群号排序），用于生成“群聊1/群聊2”这种界面标签。"""
    with _app_lock:
        group_ids = {str(g) for g in manifest.whitelist}
        group_ids.update(str(g) for g in _app_unread)
        group_ids.update(str(g) for g in _app_buffer)
        group_ids.update(str(g) for g in _app_transcript)
        return sorted(group_ids)


def _conversation_label(group_id: str) -> str:
    """给人/行为提示词看的对话名；不暴露原始群号。"""
    group_id = str(group_id or "")
    try:
        index = _conversation_ids().index(group_id) + 1
        return f"群聊{index}"
    except ValueError:
        return "一个群聊"


def app_can_read(group_id: str) -> bool:
    """只有软件打开且正停在这个对话里，消息才直接进入理解层。"""
    with _app_lock:
        return bool(_app_open and str(_app_current_group) == str(group_id))


def is_app_open() -> bool:
    with _app_lock:
        return bool(_app_open)


def get_current_conversation() -> str:
    with _app_lock:
        return str(_app_current_group or "")


def _app_conversation_list() -> list:
    conversations = []
    for index, group_id in enumerate(_conversation_ids(), start=1):
        with _app_lock:
            unread = int(_app_unread.get(group_id, 0))
        conversations.append({
            "id": group_id,
            "label": f"群聊{index}",
            "unread": unread,
        })
    return conversations


def app_status() -> dict:
    """软件当前状态：是否打开、总未读、当前对话、对话列表。"""
    conversations = _app_conversation_list()
    with _app_lock:
        return {
            "id": "qq_napcat",
            "label": "QQ",
            "open": bool(_app_open),
            "unread": sum(int(v) for v in _app_unread.values()),
            "current_conversation": str(_app_current_group or ""),
            "at_bottom": bool(_app_at_bottom.get(str(_app_current_group or ""), True)),
            "conversations": conversations,
        }


def _app_preview_from_data(data: dict) -> str:
    """给未读缓冲用的轻量预览：不调用 LLM，不下载媒体。"""
    group_id = str(data.get("group_id") or "")
    sender_id = str(data.get("user_id") or "")
    sender_name = manifest.name_map.get(group_id, {}).get(sender_id)
    if not sender_name:
        sender = data.get("sender", {}) or {}
        sender_name = sender.get("card") or sender.get("nickname") or sender_id or "某人"

    raw_str = data.get("raw_message", "")
    text, mentions = parse_cq_code(raw_str)
    segments = data.get("message", [])
    if not text:
        text = _text_from_segments(segments)
    if not text and mentions:
        text = (
            f"叫了{BOT_NAME}一声"
            if (get_bot_qq() in mentions or "all" in mentions)
            else "只@了人，没有说话"
        )
    media_count = sum(
        1 for seg in segments
        if isinstance(seg, dict) and seg.get("type") in ("image", "record", "video", "mface")
    )
    notes = []
    if media_count:
        notes.append(f"发送了{media_count}个媒体")
    # 小程序/聊天记录：预览只读卡片元信息，不取正文、不调 LLM
    rich_preview = rich_message.preview_rich_segments(segments)
    if rich_preview:
        notes.append(rich_preview)
    if notes:
        note_text = "；".join(notes)
        text = f"{text}（{note_text}）" if text else f"（{note_text}）"
    if not text:
        text = "（没有说话）"
    if len(text) > 200:
        text = text[:200] + "…"
    return f"{sender_name}说：{text}"


def _message_real_time(data: dict) -> float:
    raw = data.get("time") or data.get("timestamp") or time.time()
    try:
        value = float(raw)
        if value > 1e12:
            value /= 1000.0
        return value
    except (TypeError, ValueError):
        return time.time()


def _archive_preview(data: dict):
    """未读阶段也把消息落盘：时间戳供记忆时间前缀与历史翻读使用。"""
    group_id = str(data.get("group_id") or "")
    message_id = _message_id_of(data)
    sender_id = str(data.get("user_id") or "")
    sender_name = manifest.name_map.get(group_id, {}).get(sender_id)
    if not sender_name:
        sender = data.get("sender", {}) or {}
        sender_name = sender.get("card") or sender.get("nickname") or sender_id or "某人"
    raw_str = data.get("raw_message", "")
    text, _mentions = parse_cq_code(raw_str)
    if not text:
        text = _text_from_segments(data.get("message", []))
    if len(text) > 300:
        text = text[:300] + "…"
    _chat_store.append({
        "conversation_id": group_id,
        "message_id": message_id,
        "ts": _message_real_time(data),
        "sender": sender_name,
        "text": text,
        "line": _app_preview_from_data(data),
    })


def _archive_enriched(group_id: str, message_id: str, sender: str, text: str,
                      media_text: str, line: str, ts: float):
    _chat_store.append({
        "conversation_id": str(group_id or ""),
        "message_id": str(message_id or ""),
        "ts": ts,
        "sender": str(sender or ""),
        "text": str(text or "")[:300],
        "media_text": str(media_text or ""),
        "line": str(line or ""),
    })


def app_buffer_message(data: dict) -> bool:
    """软件未打开/未停在对话时，把消息留在缓冲里，只累计未读。"""
    group_id = str(data.get("group_id") or "")
    message_id = _message_id_of(data)
    if _is_duplicate_message(message_id):
        return False
    _archive_preview(data)
    preview = _app_preview_from_data(data)
    try:
        limit = max(10, int(config.get("qq_unread_buffer_max", 200)))
    except (TypeError, ValueError):
        limit = 200
    with _app_lock:
        buffer = _app_buffer.setdefault(group_id, deque(maxlen=limit))
        buffer.append((message_id, data, preview))
        _app_unread[group_id] = int(_app_unread.get(group_id, 0)) + 1
        unread_now = _app_unread[group_id]
    # 外部活动仍然要记：手机在响，只是她还没点开软件。
    try:
        BIORHYTHM.note_activity()
    except Exception:
        pass
    logger.info(f"[QQ软件] 未读+1（{_conversation_label(group_id)}，当前 {unread_now} 条）")
    return True


def _app_mark_seen(message_id: str) -> bool:
    """返回 True 表示这条消息已经写过对话记录，避免预览与正式处理重复。"""
    message_id = str(message_id or "")
    if not message_id:
        return False
    with _app_lock:
        if message_id in _app_seen_transcript_ids:
            return True
        _app_seen_transcript_ids.add(message_id)
        _app_seen_transcript_order.append(message_id)
        if len(_app_seen_transcript_ids) > 600:
            _app_seen_transcript_ids.clear()
            _app_seen_transcript_ids.update(_app_seen_transcript_order)
        return False


def _app_record_message(group_id: str, message_id: str, line: str, push: bool = True) -> bool:
    """把一条已读内容写入对话记录；push=True 时同时交给主进程。

    返回 True 表示本次确实写入了对话记录；False 表示重复消息被跳过。
    """
    group_id = str(group_id or "")
    line = str(line or "").strip()
    if not group_id or not line:
        return False
    if _app_mark_seen(message_id):
        return False
    with _app_lock:
        transcript = _app_transcript.setdefault(group_id, deque(maxlen=_APP_TRANSCRIPT_MAX))
        at_bottom = bool(_app_at_bottom.get(group_id, True))
        overflow = transcript.maxlen is not None and len(transcript) >= transcript.maxlen
        transcript.append(line)
        if at_bottom:
            _app_read_start[group_id] = max(0, len(transcript) - 6)
        else:
            # 正在往上翻时来了新消息：视图停在原处，不把阅读位置拽回底部。
            start = int(_app_read_start.get(group_id, max(0, len(transcript) - 6)))
            if overflow:
                start = max(0, start - 1)
            _app_read_start[group_id] = min(start, len(transcript))
        _app_at_bottom[group_id] = at_bottom
        can_push = bool(_app_open and str(_app_current_group) == group_id)
    if push and can_push:
        _app_context().remember(line)
    return True


async def app_open() -> dict:
    """打开 QQ：只告知未读，不自动把正文塞给主进程。"""
    global _app_open
    with _app_lock:
        was_open = _app_open
        _app_open = True
        total = sum(int(v) for v in _app_unread.values())
    conversations = _app_conversation_list()
    if was_open:
        detail = "QQ 已经打开了"
    elif total:
        detail = f"我打开了QQ，有 {total} 条未读消息"
    else:
        detail = "我打开了QQ，没有未读消息"
    lines = [
        f"{c['label']}有 {c['unread']} 条未读"
        for c in conversations if c["unread"] > 0
    ]
    logger.info(f"[QQ软件] {detail}")
    return {
        "ok": True,
        "detail": detail,
        "content_lines": lines,
        "conversations": conversations,
        "open": True,
        "unread": total,
    }


async def app_close() -> dict:
    global _app_open, _app_current_group
    with _app_lock:
        _app_open = False
        _app_current_group = ""
    logger.info("[QQ软件] 已关闭 QQ")
    return {"ok": True, "detail": "我关掉了QQ", "open": False}


async def app_open_conversation(group_id: str) -> dict:
    """在已打开的 QQ 里点开一个对话：返回最近未读预览，并在后台正式解析。"""
    global _app_current_group
    group_id = str(group_id or "").strip()
    if not group_id:
        return {"ok": False, "detail": "没有指定要打开哪个对话"}
    if not is_app_open():
        await app_open()
    with _app_lock:
        known = (
            group_id in manifest.whitelist
            or group_id in _app_unread
            or group_id in _app_buffer
            or group_id in _app_transcript
            or group_id == get_active_group_id()
        )
    if not known:
        return {"ok": False, "detail": f"找不到对话 {group_id}"}

    with _app_lock:
        _app_current_group = group_id
        pending = list(_app_buffer.pop(group_id, []))
        _app_unread[group_id] = 0
        transcript = _app_transcript.setdefault(group_id, deque(maxlen=_APP_TRANSCRIPT_MAX))
        previews = []
        archived = _chat_store.tail(group_id, max(20, len(pending) + 6)) if not transcript else []
        if archived:
            for record in archived:
                record_id = str(record.get("message_id") or "")
                if record_id:
                    _app_mark_seen(record_id)
                line = str(record.get("line") or "").strip()
                if line:
                    transcript.append(line)
        for message_id, data, preview in pending:
            # 富消息（小程序/聊天记录）的预览只是占位：正文要等后台解析出摘要。
            # 这里不写进对话记录、也不标记已读，避免摘要被 message_id 去重吞掉。
            if rich_message.has_rich_segments(data.get("message") or []):
                previews.append(preview)
                continue
            if _app_mark_seen(message_id):
                previews.append(preview)
                continue
            transcript.append(preview)
            previews.append(preview)
        _app_read_start[group_id] = max(0, len(transcript) - 6)
        _app_archive_cursor[group_id] = (
            _chat_store.cursor(archived[0]) if archived else None
        )
        _app_at_bottom[group_id] = True
        recent = list(transcript)[-6:]

    # 正文解析（理解层、媒体、记忆写入）在后台进行，不阻塞打开动作。
    for _mid, data, _preview in pending:
        task = asyncio.create_task(handle_group_message(data, from_flush=True))
        _app_flush_tasks.add(task)
        task.add_done_callback(_app_flush_tasks.discard)

    label = _conversation_label(group_id)
    if previews:
        detail = f"我点开了{label}的对话，看到 {len(previews)} 条未读"
    else:
        detail = f"我点开了{label}的对话"
    logger.info(f"[QQ软件] {detail}")
    content_lines = list(recent[-6:])
    transcript_set = set(transcript)
    for line in previews[-8:]:
        if line not in transcript_set and line not in content_lines:
            content_lines.append(line)
    return {
        "ok": True,
        "detail": detail,
        "conversation": group_id,
        "content_lines": content_lines[-12:],
    }


async def app_close_conversation() -> dict:
    """退出当前对话，回到软件首页；软件本身仍保持打开。"""
    global _app_current_group
    with _app_lock:
        if not _app_open:
            return {"ok": False, "detail": "QQ 还没有打开"}
        if not _app_current_group:
            return {"ok": True, "detail": "已经在 QQ 首页了"}
        _app_current_group = ""
    logger.info("[QQ软件] 退出了当前对话")
    return {"ok": True, "detail": "我退出了这个对话，回到了QQ首页"}


async def app_read_earlier(group_id: str = "") -> dict:
    """在当前对话里往回翻 6 条；先读内存，再读持久化历史。"""
    group_id = str(group_id or get_current_conversation() or "").strip()
    if not is_app_open() or not group_id:
        return {"ok": False, "detail": "还没打开任何对话"}
    with _app_lock:
        transcript = _app_transcript.get(group_id)
        items = list(transcript) if transcript else []
        start = int(_app_read_start.get(group_id, max(0, len(items) - 6)))
        start = max(0, min(start, len(items)))
        new_start = max(0, start - 6)
        chunk = items[new_start:start]
        if chunk:
            _app_read_start[group_id] = new_start
            _app_at_bottom[group_id] = False
    if chunk:
        return {
            "ok": True,
            "detail": "我往上翻了翻，看到更早的对话",
            "conversation": group_id,
            "content_lines": chunk,
        }

    # 内存缓冲到头：继续从持久化聊天记录取更早的内容。
    with _app_lock:
        cursor = _app_archive_cursor.get(group_id)
        known_lines = {}
        for line in items:
            known_lines[line] = known_lines.get(line, 0) + 1
        known_ids = set(_app_seen_transcript_ids)
    if cursor:
        if isinstance(cursor, (tuple, list)) and len(cursor) >= 2:
            cursor_ts, cursor_seq = float(cursor[0]), int(cursor[1])
        else:
            cursor_ts, cursor_seq = float(cursor), None
    else:
        cursor_ts, cursor_seq = _chat_store.latest_ts(group_id) + 1.0, None
    older = []
    for _ in range(3):
        batch = _chat_store.earlier(
            group_id, cursor_ts, 30, before_seq=cursor_seq,
        )
        if not batch:
            older = []
            break
        cursor_ts, cursor_seq = _chat_store.cursor(batch[0])
        new_records = []
        for record in batch:
            line = str(record.get("line") or "").strip()
            if not line:
                continue
            record_id = str(record.get("message_id") or "")
            if record_id and record_id in known_ids:
                continue
            if not record_id and known_lines.get(line, 0) > 0:
                known_lines[line] -= 1
                continue
            new_records.append(record)
        if new_records:
            chunk_records = new_records[-6:]
            older = [
                str(record.get("line") or "").strip() for record in chunk_records
            ]
            cursor_ts, cursor_seq = _chat_store.cursor(chunk_records[0])
            break
    with _app_lock:
        _app_archive_cursor[group_id] = (
            (cursor_ts, cursor_seq) if cursor_seq is not None else cursor_ts
        )
        if older:
            _app_at_bottom[group_id] = False
    if older:
        return {
            "ok": True,
            "detail": "我往上翻了翻，从聊天记录里看到更早的对话",
            "conversation": group_id,
            "content_lines": older,
        }
    return {"ok": False, "detail": "再往前翻，已经没有更早的对话了"}


async def app_scroll_to_bottom() -> dict:
    """回到最新消息底部；动作层在历史里时用它。"""
    group_id = str(get_current_conversation() or "").strip()
    if not is_app_open() or not group_id:
        return {"ok": False, "detail": "还没打开任何对话"}
    with _app_lock:
        transcript = _app_transcript.get(group_id)
        items = list(transcript) if transcript else []
        _app_read_start[group_id] = max(0, len(items) - 6)
        _app_at_bottom[group_id] = True
    if not items:
        items = [str(r.get("line") or "") for r in _chat_store.tail(group_id, 6)]
    return {
        "ok": True,
        "detail": "我回到了最新消息",
        "conversation": group_id,
        "content_lines": [line for line in items[-6:] if line],
    }


def set_login_info(user_id: str, nickname: str, source: str = "napcat", persist: bool = True):
    """记录 NapCat 自动获取的登录身份（供测试与 ws_handler 调用）。"""
    with _identity_lock:
        if user_id:
            _login_info["user_id"] = str(user_id).strip()
        if nickname:
            _login_info["nickname"] = str(nickname).strip()
        _login_info["source"] = source
    if persist:
        _save_identity_file()


def get_bot_qq_display_name() -> str:
    """QQ 侧显示名：自动昵称优先，未获取时退回意识体名字。"""
    return get_bot_qq_name() or BOT_NAME


# 上次连接时自动抓到的 QQ 身份（只做展示兜底；连接后仍以 get_login_info 为准）
_load_identity_file()


def get_active_group_id():
    """主动发言的目标群（动态读取，保存设置后无需重启即可生效）。"""
    return _cfg_str("active_group_id", "123456")


def get_napcat_token():
    """NapCat 鉴权 Token（动态读取，保存设置后无需重启即可生效）。"""
    return _cfg_str("napcat_token", "Nascence")


def get_napcat_http_url():
    """NapCat 正向 HTTP 服务地址（支持环境变量 NAPCAT_HTTP_URL 或动态配置，供语音下载等调用）。"""
    raw = os.environ.get("NAPCAT_HTTP_URL") or _cfg_str("napcat_http_url", "http://127.0.0.1:5700")
    raw = str(raw).strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw or "http://127.0.0.1:5700"


# 兼容旧引用（模块内部已改为动态读取）
BOT_QQ = get_bot_qq()
ACTIVE_GROUP_ID = get_active_group_id()
HTTP_ACCESS_TOKEN = get_napcat_token()
WS_ACCESS_TOKEN = get_napcat_token()
HTTP_API_BASE = get_napcat_http_url()

_recent_msg_list = []      # 有序存储 (sender_id, clean_text)
_recent_msg_set = set()    # 快速查找去重
MAX_RECENT_MESSAGES = 20

IGNORE_PREFIX = "#"     # 前缀特殊字符

_final_save_done = False            # 全局保存标识
_sleep_maintenance_done = False     # 本次睡眠是否已做过维护（避免重复全量落盘）
_napcat_websocket = None             # 当前连接的 NapCat 反向 WebSocket
_cognitive_task = None               # 永续认知循环 task
_shutdown_event = None               # 服务停止信号（在事件循环线程内 set）
_action_lock = asyncio.Lock()        # 串行发送 OneBot Action，避免响应混淆
_action_counter = 0
_pending_actions = {}                 # echo -> Future，用于匹配 NapCat Action 响应

# ========== “软件”状态：QQ 是她可以打开/关闭的应用 ==========
# 未打开时：消息只进缓冲与未读数，不解析内容、不写记忆、不注入关键词；
# 打开软件：告知未读；点开对话后才把该对话的内容送入主进程；
# 翻读更早：从本地对话缓冲里往回翻（不重新请求 LLM）。
_app_lock = threading.RLock()
_app_open = bool(config.get("qq_app_default_open", False))
_app_current_group = ""
_app_unread = {}                     # group_id -> 未读数
_app_buffer = {}                     # group_id -> deque[(message_id, data, preview)]
_app_transcript = {}                 # group_id -> deque[str]（已读过的对话内容）
_app_read_start = {}                 # group_id -> 当前翻读窗口起点
_app_seen_transcript_ids = set()     # 防止预览与正式处理重复写入
_app_seen_transcript_order = deque(maxlen=500)
_app_flush_tasks = set()
_app_at_bottom = {}                  # group_id -> 是否停在最新消息处
_app_archive_cursor = {}             # group_id -> 已读取到的最早消息时间戳
_chat_store = ChatStore("qq_chat_history")
_APP_TRANSCRIPT_MAX = 120

# ========== 睡眠配置 ==========
# 睡眠由 core.biorhythm 的睡眠压力动力学 + 学得的作息决定（不硬编码钟点）
_sleeping = False               # 是否处于强制睡眠状态

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QQBot")

# ---------- 加载映射表 ----------
class QQManifest:
    def __init__(self):
        try:
            # utf-8-sig：兼容 Windows 记事本等编辑器保存时写入的 UTF-8 BOM，
            # 否则文件首字符变成 \ufeff，json.load 会直接抛 JSONDecodeError。
            with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            self.whitelist = {str(x) for x in data.get("whitelist_groups", [])}
            self.name_map = data.get("name_mapping", {})
            logger.info(f"映射表加载成功，白名单群组：{self.whitelist}")
        except FileNotFoundError:
            import shutil
            example_path = CONFIG_EXAMPLE_PATH
            if os.path.exists(example_path):
                shutil.copy2(example_path, CONFIG_PATH)
                logger.info(f"已从 {example_path} 创建默认配置文件 {CONFIG_PATH}")
            else:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump({"whitelist_groups": [], "name_mapping": {}}, f, ensure_ascii=False, indent=2)
                logger.info(f"已创建空白的配置文件 {CONFIG_PATH}")
            logger.error("前往qq_manifest.json中修改群聊白名单")
            BUS.task_error.emit("前往qq_manifest.json中修改群聊白名单")
            with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            self.whitelist = {str(x) for x in data.get("whitelist_groups", [])}
            self.name_map = data.get("name_mapping", {})
        except json.JSONDecodeError as e:
            # 配置文件语法损坏（多余逗号、缺引号等）。这里不再让整个 QQ 服务崩掉：
            # 退化为空名单并明确报错，用户改好文件重启即可。
            self.whitelist = set()
            self.name_map = {}
            msg = (
                f"{CONFIG_PATH} 解析失败（{e.msg}，第 {e.lineno} 行）："
                f"请检查 JSON 语法，注意不要有多余逗号或未转义的引号"
            )
            logger.error(msg)
            BUS.task_error.emit(msg)

manifest = QQManifest()

# ---------- 工具函数 ----------
def parse_cq_code(text: str) -> Tuple[str, List[str]]:
    """移除CQ码，提取被@的QQ号列表（兼容扩展字段如 text/name 以及参数顺序）"""
    # 匹配 [CQ:at,qq=123456] 或 [CQ:at,qq=123456,text=@xxx] 或 [CQ:at,text=@xxx,qq=123456] 等
    mentions = []
    for m in re.finditer(r'\[CQ:at,([^\]]+)\]', text):
        params_str = m.group(1)
        qq_match = re.search(r'(?:^|,)qq=(\w+)', params_str)
        if qq_match:
            mentions.append(qq_match.group(1))
    clean_text = re.sub(r'\[CQ:[^\]]+\]', '', text).strip()
    return clean_text, mentions


def build_augmented_input(sender_name: str, raw_text: str, mentions: List[str], group_id: str, is_mentioned_me: bool) -> str:
    """
    构建标准化增强输入（自然语言格式）
    """
    # 如果文本为空，给一个占位符（但正常情况下不会走到这里）
    text_content = raw_text if raw_text else "（没有说话）"
    
    if is_mentioned_me:
        return f'{sender_name}对{BOT_NAME}说：“{text_content}”'
    elif mentions:
        mapped_names = []
        for qq in mentions:
            name = manifest.name_map.get(group_id, {}).get(qq, qq)
            mapped_names.append(name)
        target_str = "、".join(mapped_names)
        return f'{sender_name}对{target_str}说：“{text_content}”'
    else:
        return f'{sender_name}说：“{text_content}”'

def extract_quote_message_id(message_segments: List[Dict]) -> Optional[str]:
    """从message数组中提取引用消息ID"""
    for seg in message_segments:
        if seg.get("type") == "reply":
            return seg.get("data", {}).get("id")
    return None

async def drift_loop():
    """后台发散任务：持续驱动生物钟，并随机激活记忆以强化半衰期、提供热身种子。

    冷热下沉统一由 MemoryWriter / HotSetPlanner 负责（见 core/memory_writer.py），
    这里不再重复调度，避免同一份淘汰工作被三处同时触发。
    """
    logger.info("[浅层意识] 发散任务已启动")
    while True:
        # 由生物钟自行判定入睡/醒来（不再依赖固定钟点）
        BIORHYTHM.tick()
        if BIORHYTHM.is_asleep() and not _sleeping:
            enter_sleep()
        elif not BIORHYTHM.is_asleep() and _sleeping:
            wake_up()

        # 素材暂存清理：与记忆无关，仍在这里按分钟级节流
        now = time.time()
        if now - getattr(drift_loop, "_last_cleanup", 0.0) >= 600:
            drift_loop._last_cleanup = now
            try:
                from core.asset_library import cleanup_pending
                cleanup_pending()
            except Exception as e:
                logger.error(f"[素材暂存] 清理异常: {e}")

        await asyncio.sleep(30)
        if BIORHYTHM.is_asleep():
            continue
        from core.memory_engine import get_random_memory_id
        mem_id = get_random_memory_id()
        if not mem_id:
            continue
        access_memory(mem_id)
        pathfind_activation([mem_id], max_stamina=1.0, top_k=3, max_steps=1)
        logger.info(f"[浅层意识] 激活记忆 {mem_id[:8]}...")

def is_sleeping() -> bool:
    """判断当前是否处于睡眠状态（由生物钟唯一判定）。

    历史问题：此前是 `_sleeping or BIORHYTHM.is_asleep()`，而 _sleeping 要等
    drift_loop 下一轮（最多 30 秒）才被清掉。认知循环自己 tick 到醒来后，
    这 30 秒里所有发送都会被误拦，表现为“醒了但话发不出去”。
    现在以生物钟为唯一权威，_sleeping 只做镜像，供日志与面板观测。
    """
    global _sleeping
    asleep = BIORHYTHM.is_asleep()
    _sleeping = asleep
    return asleep

def enter_sleep():
    """进入睡眠状态。

    睡眠维护（全量落盘 + faiss 重建，实测十几秒）放到线程池执行：
    它由 drift_loop 在事件循环里调用，同步跑会卡住收消息与生物钟 tick。
    睡眠期间 NapCat 必须保持连接——被 @ 唤醒全靠这条通道。
    """
    global _sleeping
    _sleeping = True
    logger.info(f"{BOT_NAME}进入了睡眠状态（NapCat 保持连接以便唤醒）")
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _sleep_maintenance_bg)
    except RuntimeError:
        # 无事件循环时（启动早期/CLI）同步执行
        _sleep_maintenance_bg()

def wake_up():
    """从睡眠中唤醒"""
    global _sleeping, _sleep_maintenance_done
    _sleeping = False
    _sleep_maintenance_done = False   # 下一觉需要重新做维护
    logger.info(f"{BOT_NAME}醒来了！")

def init_sleep_state():
    """启动时调用：只标记睡眠状态，不执行重量级维护。

    必须轻量：它跑在 WS 服务器启动之前，而睡眠维护（全量落盘 + faiss 重建）
    要十几秒。若在这里同步执行，NapCat 连不上来，被 @ 唤醒的通道就不存在了。
    真正的维护由 start_server 起来后异步补做（见 schedule_sleep_maintenance）。
    """
    global _sleeping
    if is_sleeping():
        _sleeping = True
        logger.info(f"{BOT_NAME}处于睡眠时段，等待 NapCat 连接以便被唤醒")
    else:
        logger.info(f"当前不在睡眠状态，{BOT_NAME}保持清醒。")


def schedule_sleep_maintenance():
    """在已有事件循环里异步补做睡眠维护（不阻塞消息收发）。

    睡眠期间 NapCat 必须保持连接（要被 @ 唤醒），因此维护只能放到线程池里跑。
    """
    if not is_sleeping():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环（如 CLI 路径），退化为同步执行
        enter_sleep()
        return
    loop.run_in_executor(None, _sleep_maintenance_bg)


def _sleep_maintenance_bg():
    """线程池执行的睡眠维护，异常只记日志，不影响服务。

    用 _sleep_maintenance_done 去重：同一觉只做一次，避免
    drift_loop 与 start_server 两条路径重复全量落盘。
    """
    global _sleep_maintenance_done
    if _sleep_maintenance_done:
        return
    _sleep_maintenance_done = True
    try:
        from utils.persistence import sleep_cleanup
        sleep_cleanup()
        logger.info("[睡眠维护] 链接剪枝、字词整理、全量保存已完成")
    except Exception as e:
        logger.error(f"[睡眠维护] 执行失败: {e}")

# ---------- 消息处理核心 ----------
# ---------- message_id 幂等 ----------
# NapCat 在重连、重试或事件重复投递时会把同一条消息再发一遍。
# 以 message_id 做短时效幂等，避免重复下载媒体、重复视觉调用、重复理解层。
_seen_message_ids = deque(maxlen=500)
_seen_message_id_set = set()
_SEEN_MESSAGE_TTL = 600.0
_seen_message_ts = {}


def _message_id_of(data: dict) -> str:
    value = data.get("message_id")
    if value is None:
        return ""
    return str(value).strip()


def _is_duplicate_message(message_id: str) -> bool:
    """同一条消息只处理一次。返回 True 表示应跳过。"""
    if not message_id:
        return False
    now = time.time()
    # 清理过期
    while _seen_message_ids:
        old = _seen_message_ids[0]
        if now - _seen_message_ts.get(old, 0) <= _SEEN_MESSAGE_TTL:
            break
        _seen_message_ids.popleft()
        _seen_message_id_set.discard(old)
        _seen_message_ts.pop(old, None)
    if message_id in _seen_message_id_set:
        return True
    _seen_message_ids.append(message_id)
    _seen_message_id_set.add(message_id)
    _seen_message_ts[message_id] = now
    return False


def _text_from_segments(message_segments) -> str:
    """从结构化消息段还原正文（raw_message 为空或只有 CQ 码时兜底）。"""
    if not isinstance(message_segments, list):
        return ""
    parts = []
    for seg in message_segments:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            text = seg.get("data", {}).get("text")
            if text:
                parts.append(str(text))
    return "".join(parts).strip()


def _media_phrase(media_type: str, desc: str) -> str:
    """把媒体类型+描述渲染成自然短语（历史与增强输入共用，保证口径一致）。"""
    if media_type == "sticker":
        return f"一个表情包，内容是“{desc}”"
    if media_type == "image":
        return f"一张图片，内容是“{desc}”"
    if media_type == "record":
        return f"一段录音，内容是“{desc}”"
    return f"一个视频，内容是“{desc}”"


def _app_line_from_message(sender_name: str, clean_text: str, media_list: list,
                           target_name: str = None, quote_info: dict = None,
                           extra_text: str = "") -> str:
    """把一条消息渲染成她“在软件里读到”的一行，供对话记录与主进程使用。

    extra_text 是已经渲染好的富内容（小程序/聊天记录摘要），
    与媒体描述合并进同一个“（发送了……）”里。
    """
    text = str(clean_text or "").strip()
    notes = []
    if media_list:
        notes.append("、".join(_media_phrase(m_type, desc) for m_type, desc in media_list))
    extra_note = str(extra_text or "").strip()
    if extra_note:
        notes.append(extra_note)
    if notes:
        note_text = "、".join(notes)
        text = f"{text}（发送了{note_text}）" if text else f"（发送了{note_text}）"
    if quote_info and quote_info.get("text"):
        text += (
            f"，这是在回应{quote_info.get('sender') or '某人'}之前说的："
            f"“{quote_info.get('text')}”"
        )
    if not text:
        text = "（没有说话）"
    if len(text) > 300:
        text = text[:300] + "…"
    if target_name:
        return f"{sender_name}对{target_name}说：{text}"
    return f"{sender_name}说：{text}"


# NapCat 给群表情包的 summary 常常只是占位词，不是内容描述。
_GENERIC_STICKER_SUMMARIES = {
    "动画表情", "表情", "表情包", "图片", "图片表情", "动态表情",
    "商城表情", "商城表情包", "图片消息", "gif", "mface",
}


def _sticker_desc_from_summary(summary: str) -> str | None:
    """官方摘要能否直接当描述用；占位词返回 None，表示必须走图片解析。

    之前只要摘要里带“表情”就直接采用，于是所有群表情包都叫“[动画表情]”：
    既让记忆里全是同一个描述，也会让素材库按描述把它们去重成一条。
    """
    text = str(summary or "").strip().strip("[]【】（）()").strip()
    if not text or text.lower() in _GENERIC_STICKER_SUMMARIES:
        return None
    return text[:30]


def _is_generic_media_desc(desc: str) -> bool:
    """媒体描述是否为“表情包/商城表情包”这类零信息占位词（不据此建概念）。"""
    text = str(desc or "").strip().strip("[]【】（）()").strip()
    return not text or text.lower() in _GENERIC_STICKER_SUMMARIES


def _attach_media_memory_ids(media_list: list, memory_ids: list, media_refs: list = None):
    """把本轮纯媒体记忆的 ID 回填到对应的暂存条目上。

    动作层决定收藏时，需要这条 ID 才能在“图片记忆”和“我收藏了…”之间
    直接建因果边，让收藏贴着原图，而不是散落在记忆图别处。

    media_refs 与 media_list 一一对应（mface 这类没有暂存的条目为 None）；
    不用 pending 列表的尾部切片——中途可能有别的消息登记了新的暂存条目。
    """
    if not memory_ids or not media_refs:
        return
    try:
        from core import asset_library as ASSETS
        for ref, mem_id in zip(media_refs, memory_ids):
            if not ref:
                continue
            ASSETS.set_pending_memory_ids(ref, [mem_id])
    except Exception as e:
        logger.debug(f"媒体记忆 ID 回填失败: {e}")


# ---------- 回应目标 ----------
# 最近一条等待回应的消息。认知循环发言时据此决定 @ 谁、引用哪条。
_reply_target = None          # {"group_id","qq","name","message_id","ts","mention_me","quoted_me"}
_REPLY_TARGET_TTL = 180.0     # 3 分钟内的消息才视为“正在回应”
_last_at_time = {}            # {(group_id, qq): 上次 @ 的时间}，防刷屏
_quoted_messages = {}         # {(group_id, message_id): 上次引用的时间}，防重复引用
_QUOTED_MESSAGES_MAX = 128    # 引用记录上限，避免长期运行无限增长


def _remember_reply_target(group_id: str, qq: str, name: str, message_id: str,
                           mention_me: bool = False, quoted_me: bool = False):
    """记录本轮消息的回应目标（供认知循环 @ 或引用）。"""
    global _reply_target
    _reply_target = {
        "group_id": str(group_id),
        "qq": str(qq),
        "name": str(name or ""),
        "message_id": str(message_id or ""),
        "ts": time.time(),
        "mention_me": bool(mention_me),
        "quoted_me": bool(quoted_me),
    }


def take_reply_target(mention_only: bool = False, directed_only: bool = False) -> dict | None:
    """取出最近的回应目标；过期或不符合条件时返回 None。

    mention_only=True 时只返回“直接 @ 了机器人”的目标——普通群聊不主动 @ 人。
    directed_only=True 时返回“冲着机器人来的”目标：直接 @ 了它，或引用了它的话。
    """
    target = _reply_target
    if not target:
        return None
    if time.time() - target.get("ts", 0) > _REPLY_TARGET_TTL:
        return None
    if mention_only and not target.get("mention_me"):
        return None
    if directed_only and not (target.get("mention_me") or target.get("quoted_me")):
        return None
    return dict(target)


def _at_cooldown_ok(group_id: str, qq: str, cooldown: float = 30.0) -> bool:
    """同一个人 30 秒内最多被 @ 一次，避免连续回复时刷屏。"""
    key = (str(group_id), str(qq))
    now = time.time()
    last = _last_at_time.get(key, 0.0)
    if now - last < cooldown:
        return False
    _last_at_time[key] = now
    return True


def _quote_once_ok(group_id: str, message_id: str, ttl: float = _REPLY_TARGET_TTL) -> bool:
    """同一条消息在回应窗口内只引用一次。

    与 @ 的冷却不同，这里按 message_id 而不是按人：同一条消息回第二遍时
    再引用一次没有信息量，只会把聊天窗口挤满。
    """
    mid = str(message_id or "").strip()
    if not mid:
        return False
    key = (str(group_id), mid)
    now = time.time()
    if now - _quoted_messages.get(key, 0.0) < ttl:
        return False
    if len(_quoted_messages) >= _QUOTED_MESSAGES_MAX:
        for old_key in [k for k, ts in _quoted_messages.items() if now - ts >= ttl]:
            _quoted_messages.pop(old_key, None)
        # 清理后仍满（短时间内消息极多）：丢最早的一半，保证有界
        if len(_quoted_messages) >= _QUOTED_MESSAGES_MAX:
            for old_key in list(_quoted_messages)[: _QUOTED_MESSAGES_MAX // 2]:
                _quoted_messages.pop(old_key, None)
    _quoted_messages[key] = now
    return True


def reply_options(target_group: str) -> dict:
    """给本地认知循环用的发送参数：只回应“冲着机器人来的”消息。

    普通群聊既不主动 @ 人，也不引用别人的消息；直接 @ 了机器人或引用了
    机器人的话，才在发送时带 @/引用。目标必须属于本轮发送的群，
    多群白名单时把 A 群的消息引用进 B 群会被 NapCat 直接拒发。
    """
    at_qq = None
    quote_msg_id = None
    try:
        reply_target = take_reply_target(directed_only=True)
        if reply_target and str(reply_target.get("group_id", "")).strip() == str(target_group).strip():
            if reply_target.get("mention_me") and reply_target.get("qq") \
                    and _at_cooldown_ok(target_group, reply_target["qq"]):
                at_qq = reply_target["qq"]
            if _quote_once_ok(target_group, reply_target.get("message_id")):
                quote_msg_id = reply_target.get("message_id")
    except Exception as e:
        logger.warning(f"[回应目标] 解析失败: {e}")
    return {"reply_msg_id": quote_msg_id, "at_qq": at_qq}


def emit_outbound(text: str, kind: str = "text", sender: str = None):
    """把机器人说出口/发出的内容广播给面板（来源固定为 QQ）。"""
    BUS.message.emit(sender or BOT_NAME, text, "QQ")


def _resolve_at_mentions(text: str, group_id: str) -> tuple:
    """把文本里的 @名字 解析成 OneBot at 消息段所需的信息。

    只在名字命中群成员映射（含昵称）时才转换；未知名字保持原文，
    绝不猜测 QQ 号。邮箱、URL、代码块里的 @ 一律忽略。

    返回 (qq_list, cleaned_text)。
    """
    if not text or "@" not in text:
        return [], text
    mapping = manifest.name_map.get(str(group_id), {}) or {}
    if not mapping:
        return [], text

    name_to_qq = {}
    for qq, name in mapping.items():
        if name:
            name_to_qq[str(name)] = str(qq)

    resolved = []
    cleaned = text
    # 用“@名字”最长匹配，避免 “@小王” 抢先匹配 “@小王同学” 的前缀
    for name in sorted(name_to_qq, key=len, reverse=True):
        token = f"@{name}"
        if token not in cleaned:
            continue
        pattern = re.escape(token) + r"(?![0-9A-Za-z_\u4e00-\u9fff])"
        if not re.search(pattern, cleaned):
            continue
        qq = name_to_qq[name]
        if qq == get_bot_qq() or qq == "all":
            continue
        resolved.append(qq)
        cleaned = re.sub(pattern, "", cleaned, count=1)
    return resolved, cleaned.strip()


async def _fetch_forward_messages(resid: str):
    """通过 NapCat Action 取合并转发内容；失败返回 None。

    NapCat 不同版本把参数叫 message_id 或 id，依次尝试。
    这里只取当前这一层的内容，节点里若还有聊天记录，由解析层保留占位符。
    """
    websocket = _napcat_websocket
    if websocket is None or not resid:
        return None
    for params in ({"message_id": str(resid)}, {"id": str(resid)}):
        data = await _call_action(websocket, "get_forward_msg", params, timeout=10.0)
        if data:
            return data
    return None


def _summarize_chat_record(sender_name: str, digest, max_chars: int) -> str:
    """同步调用插件 LLM 生成摘要；失败时退化为“前两条 + 总条数”。

    这个函数会阻塞，调用方必须放到线程池里执行。
    """
    profile = None
    try:
        from plugins.registry import get_plugin
        plugin = get_plugin("qq_napcat")
        if plugin is not None:
            profile = plugin.behavior_llm()
    except Exception as e:
        logger.debug(f"[聊天记录] 读取插件 LLM 失败，回落主模型: {e}")

    messages = rich_message.build_chat_record_summary_messages(
        sender_name, digest, max_chars=max_chars
    )
    text = None
    try:
        from core.llm_interface import call_api_thinking
        text = call_api_thinking(
            messages,
            max_tokens=max(120, min(600, int(max_chars) * 2)),
            thinking=False,
            timeout=30.0,
            profile=profile,
        )
    except Exception as e:
        logger.warning(f"[聊天记录] 摘要生成失败: {e}")

    summary = rich_message.normalize_chat_record_summary(text, max_chars=max_chars)
    if not summary:
        summary = rich_message.fallback_chat_record_summary(digest, max_chars=max_chars)
    return summary


async def _collect_rich_content(message_segments, group_id: str, sender_name: str):
    """解析一条消息里的小程序与聊天记录。

    聊天记录只展开一层（get_forward_msg 一次），并用 LLM 概括；
    后续只把摘要交回消息处理层，原始逐条内容不进入记忆与短期上下文。
    """
    extras = rich_message.RichExtras()
    max_messages = _cfg_int("qq_chat_record_max_messages", 80, 1, 500)
    max_chars = _cfg_int("qq_chat_record_max_chars", 4000, 200, 20000)
    max_summary = _cfg_int("qq_chat_record_summary_max_chars", 160, 40, 600)

    collected_lines = []
    used_chars = 0
    truncated = False
    hints = []

    for kind, payload, seg_data in rich_message.iter_rich_segments(message_segments):
        if kind == "miniapp":
            info = rich_message.extract_mini_program(payload)
            if info.title or info.description:
                extras.mini_programs.append(info)
            continue
        if kind != "chat_record":
            continue

        extras.has_chat_record = True
        resid = rich_message.extract_forward_resid(payload, seg_data)
        digest = None
        if resid:
            try:
                data = await _fetch_forward_messages(resid)
            except Exception as e:
                logger.warning(f"[聊天记录] 获取失败（{resid[:12]}…）: {e}")
                data = None
            if data:
                digest = rich_message.flatten_forward_messages(
                    data, max_messages=max_messages, max_chars=max_chars
                )
                extras.chat_total += digest.total

        if not digest or not digest.lines:
            hint = rich_message.chat_record_hint(payload)
            if hint and hint != "一段聊天记录":
                hints.append(hint)
            continue

        truncated = truncated or digest.truncated
        for line in digest.lines:
            # 多条聊天记录合并时也服从全局上限
            if len(collected_lines) >= max_messages or (
                collected_lines and used_chars + len(line) > max_chars
            ):
                truncated = True
                break
            collected_lines.append(line)
            used_chars += len(line)

    if collected_lines:
        digest = rich_message.ForwardDigest(
            lines=collected_lines,
            total=max(extras.chat_total, len(collected_lines)),
            shown=len(collected_lines),
            truncated=truncated,
        )
        extras.chat_summary = await asyncio.to_thread(
            _summarize_chat_record, sender_name, digest, max_summary
        )
        extras.chat_truncated = truncated
    elif hints:
        # 取不到正文（例如 NapCat 拒绝 get_forward_msg）时，至少保留卡片提示
        extras.chat_summary = rich_message.clip_text(
            "；".join(dict.fromkeys(hints)), max_summary
        )

    if extras.has_chat_record:
        logger.info(
            f"[聊天记录] {_conversation_label(group_id)}：展开 {len(collected_lines)} 条"
            f"（共 {extras.chat_total or '未知'} 条，截断={truncated}），"
            f"摘要 {len(extras.chat_summary)} 字"
        )
    for info in extras.mini_programs:
        logger.info(f"[小程序] 已解析卡片（不打开）: {info.title[:40]}")
    return extras


async def _handle_rich_message_shortcut(
    *,
    group_id: str,
    message_id: str,
    sender_name: str,
    clean_text: str,
    media_list: list,
    media_refs: list,
    media_text: str,
    rich_text: str,
    target_name: str,
    quote_info: dict,
    summary: str,
    total: int,
    event_time: float,
):
    """聊天记录快捷路径：摘要直接进短期上下文，不再让理解层拆解整段记录。

    与纯媒体快捷路径一致：不调用 decompose_input，避免为了几十条聊天记录
    再花一次理解层调用；摘要已经由专用提示词压缩过。
    """
    from core.llm_interface import add_to_history

    target_clause = f"对{target_name}" if target_name else ""
    sent_items = "、".join(p for p in (media_text, rich_text) if p)
    if clean_text:
        fragment = f"{sender_name}{target_clause}说：“{clean_text}”"
        if sent_items:
            fragment += f"，同时发送了{sent_items}"
    else:
        fragment = f"{sender_name}{target_clause}发送了{sent_items or '一段聊天记录'}"

    mem_id = None
    try:
        mem_id = create_memory(fragment, half_life=2 * 24 * 3600)
    except Exception as e:
        logger.warning(f"[聊天记录] 记忆写入失败: {e}")

    # 同一条消息里若还带了图片/表情包，把本轮记忆 ID 回填到暂存条目，
    # 收藏时才能贴着原图建链（与纯媒体快捷路径一致）。
    if mem_id and media_refs:
        _attach_media_memory_ids(media_list, [mem_id] * len(media_list), media_refs)

    keywords = extract_keywords_jieba(f"{clean_text} {rich_text} {media_text}")
    if keywords:
        inject_keywords(keywords, "chat_record")

    history_content = clean_text.strip()
    if sent_items:
        history_content = (
            f"{history_content}（发送了{sent_items}）"
            if history_content else f"（发送了{sent_items}）"
        )
    else:
        history_content = "（转发了一段聊天记录）"
    try:
        add_to_history(
            sender_name, history_content, None, "QQ",
            quote=quote_info, target=target_name,
        )
    except Exception as e:
        logger.warning(f"[聊天记录] 对话历史写入失败: {e}")

    # 摘要追加到短期上下文（软件上下文）；预览占位已占用 message_id 时
    # _app_record_message 会跳过，这里补一次 remember，保证摘要一定进上下文。
    line = _app_line_from_message(
        sender_name, clean_text, media_list, target_name, quote_info,
        extra_text=rich_text,
    )
    _archive_enriched(
        group_id, message_id, sender_name, clean_text,
        sent_items, line, event_time,
    )
    if app_can_read(group_id):
        if not _app_record_message(group_id, message_id, line):
            _app_context().remember(line)

    PROBE.bump("message.chat_record_summary")
    logger.info(
        f"[聊天记录] 摘要进入短期上下文（{total or '未知'} 条 → {len(summary)} 字）: "
        f"{summary[:60]}"
    )
    BUS.message.emit(BOT_NAME, "[思考中...]", "QQ")


async def handle_group_message(data: dict, from_flush: bool = False):
    """处理单条群消息

    from_flush=True：来自“打开对话”后的缓冲回放，重复标记已在入缓冲时做过。
    睡眠期间的策略：未被 @ 的消息直接忽略；被 @ 则唤醒后正常回应。
    """
    import time as _time
    _started = _time.perf_counter()
    try:
        await _handle_group_message_impl(data, from_flush=from_flush)
    finally:
        PROBE.record_ms("message.handle_ms", (_time.perf_counter() - _started) * 1000.0)


async def _handle_group_message_impl(data: dict, from_flush: bool = False):
    group_id = str(data.get("group_id"))
    
    # 白名单过滤
    if group_id not in manifest.whitelist:
        logger.debug(f"群 {group_id} 不在白名单，忽略")
        return

    # 幂等：同一条消息重复投递时直接跳过，避免重复媒体下载与 LLM 调用
    message_id = _message_id_of(data)
    if not from_flush and _is_duplicate_message(message_id):
        logger.info(f"重复消息已跳过（message_id={message_id}）")
        return

    # 自己发出的消息不进入感知层：她的“念头”已经由认知循环处理，
    # 平台回显再走一遍理解层会生成重复记忆，也会干扰睡眠判定。
    # self_id 来自事件本身，登录信息尚未取回时也能正确识别自己。
    sender_id = str(data.get("user_id"))
    self_id = str(data.get("self_id") or "").strip()
    if sender_id and (sender_id == get_bot_qq() or (self_id and sender_id == self_id)):
        logger.debug("忽略机器人自己的消息回显")
        return

    # 所有数据先落盘（含软件未打开时的未读、富消息卡片）；时间戳供记忆前缀与
    # 后续“向上翻历史”使用。处理完成后会用带媒体描述的行再覆盖一次。
    _archive_preview(data)
    msg_time = _message_real_time(data)

    # 记录外部活动：作息惯性靠"安静多久"判断是否容易入睡。
    # 只认群消息，不认她自己的发言——认知循环每几秒就自转一次，
    # 若把自己的念头算作活动，她永远睡不着。
    try:
        BIORHYTHM.note_activity()
    except Exception:
        pass

    # 发送人判定：优先配置映射名 → 群昵称(card) → QQ昵称(nickname)
    sender_name = manifest.name_map.get(group_id, {}).get(sender_id)
    if sender_name:
        logger.debug(f"发送人命中配置映射: {sender_id} -> {sender_name}")
    else:
        card = data.get("sender", {}).get("card")
        nickname = data.get("sender", {}).get("nickname")
        if card:
            sender_name = card
        elif nickname:
            sender_name = nickname
        else:
            # 群昵称和QQ昵称均缺失（NapCat 应始终提供 nickname），忽略该消息
            logger.warning(f"发送人 {sender_id} 无昵称信息，忽略该消息")
            return

    raw_str = data.get("raw_message", "")
    clean_text, mentions = parse_cq_code(raw_str)
    message_segments = data.get("message") or []
    if not isinstance(message_segments, list):
        message_segments = []
    # raw_message 可能只含 CQ 码（体积消息、多段消息），此时从结构化消息段补回正文。
    # 否则文本+图片的复合消息会被误判成“纯媒体”，正文永久丢失。
    segment_text = _text_from_segments(message_segments)
    if not clean_text and segment_text:
        clean_text, seg_mentions = parse_cq_code(segment_text)
        for qq in seg_mentions:
            if qq not in mentions:
                mentions.append(qq)
        if not clean_text:
            clean_text = segment_text

    # 从结构化 message 数组补充 @ 目标（处理 NapCat 未以标准 CQ 码上报或多段消息）
    for seg in message_segments:
        if seg.get("type") == "at":
            at_qq = str(seg.get("data", {}).get("qq", "")).strip()
            if at_qq and at_qq not in mentions:
                mentions.append(at_qq)

    # 检查纯文本手打 @（如 "@辉夜" 或 "@她的QQ昵称"）或 @全体成员
    # 意识体名字（BOT_NAME）用于内部记忆；QQ 昵称只用于识别平台上的 @。
    bot_qq = get_bot_qq()
    bot_names = {BOT_NAME}
    qq_nickname = get_bot_qq_name()
    if qq_nickname:
        bot_names.add(qq_nickname)
    text_has_bot_name = any(
        f"@{name}" in raw_str or f"@{name}" in clean_text for name in bot_names if name
    )
    is_mentioned_me = (bot_qq in mentions) or ("all" in mentions) or text_has_bot_name

    # 消息前缀忽略
    if clean_text.lstrip().startswith(IGNORE_PREFIX):
        logger.info(f"消息以忽略前缀 '{IGNORE_PREFIX}' 开头，忽略")
        return
    elif IGNORE_PREFIX in clean_text:           # 包含即忽略
        logger.info(f"消息包含忽略字符 '{IGNORE_PREFIX}' ，忽略")
        return
    for name in sorted(bot_names, key=len, reverse=True):
        if name:
            clean_text = clean_text.replace(f"@{name}", "")
    clean_text = clean_text.strip()

    # 小程序/聊天记录的轻量预览（不取正文、不调 LLM），让面板先看到这类消息
    rich_preview = rich_message.preview_rich_segments(message_segments)
    display_text = clean_text
    if rich_preview:
        display_text = f"{display_text}（{rich_preview}）" if display_text else rich_preview
    if display_text:
        BUS.message.emit(sender_name, display_text, "QQ")

    if is_mentioned_me:
        mentions = [m for m in mentions if m != bot_qq and m != "all"]

    # 解析说话对象：@机器人 -> BOT_NAME；@其他人 -> 映射名称列表
    target_name = None
    if is_mentioned_me:
        target_name = BOT_NAME
    elif mentions:
        mapped_names = []
        for qq in mentions:
            name = manifest.name_map.get(group_id, {}).get(qq, qq)
            mapped_names.append(name)
        target_name = "、".join(mapped_names)

    # 睡眠判定（在解析出 @ 之后）：未被 @ 则忽略；被 @ 则唤醒后继续回应
    if is_sleeping():
        if is_mentioned_me:
            logger.info(f"{BOT_NAME}正在睡觉，被 @ 唤醒")
            BIORHYTHM.wake("qq@")
            wake_up()   # 同步模块级睡眠标志，避免后续消息仍被当作睡眠期丢弃
        else:
            logger.debug(f"{BOT_NAME}正在睡觉，消息未被 @，忽略")
            return

    # 富消息解析（小程序 / 聊天记录）：
    # 聊天记录要先取一层内容再让 LLM 概括，耗时与媒体描述相当；
    # 放到独立任务里，与下面的媒体描述并发，不阻塞事件循环。
    rich_task = None
    if rich_message.has_rich_segments(message_segments):
        rich_task = asyncio.create_task(
            _collect_rich_content(message_segments, group_id, sender_name)
        )

    # 多模态处理
    # 媒体描述改为并发执行（最多 2 个），避免一条多图消息串行等待视觉模型；
    # 同时保持 media_list 顺序与消息段顺序一致。
    media_list = []  # 收集 (type, description)
    media_refs = []  # 与 media_list 对齐的暂存引用（无暂存时为 None）
    media_tasks = [] # [(order, coroutine)]
    _media_sem = asyncio.Semaphore(2)
    _MAX_MEDIA_DESC = 3   # 单条消息最多描述 3 个媒体段，其余只记数量

    async def _describe_one(order, seg_type, seg_data, tmp_path, file_format, owns_tmp_path):
        try:
            async with _media_sem:
                if seg_type == "image":
                    chk_gif = file_format == "gif"
                    chk_mface = bool(seg_data.get("emoji_id") or seg_data.get("emoji_package_id") or seg_data.get("key"))
                    summary = str(seg_data.get("summary") or "").strip()
                    chk_summary = "表情" in summary
                    chk_subtype = str(seg_data.get("sub_type")) == "1"
                    is_sticker = chk_gif or chk_mface or chk_summary or chk_subtype
                    media_type = "sticker" if is_sticker else "image"
                    # 有真正的官方摘要才省掉视觉调用；占位摘要必须走图片解析
                    desc = _sticker_desc_from_summary(summary) if (is_sticker and not chk_gif) else None
                    if desc is None:
                        max_chars = 30 if is_sticker else 60
                        desc = await describe_image_from_path(tmp_path, max_chars=max_chars)
                    if is_sticker and (not desc or desc.startswith("[图片识别失败") or desc.startswith("[图片文件不存在")):
                        desc = _sticker_desc_from_summary(summary) or "表情包"
                elif seg_type == "record":
                    media_type = "record"
                    desc = await describe_audio_from_path(tmp_path)
                else:
                    media_type = "video"
                    desc = await describe_video_from_path(tmp_path)
                return (order, media_type, desc, tmp_path, owns_tmp_path)
        except Exception as e:
            logger.warning(f"处理媒体失败: {e}")
            return (order, None, None, tmp_path, owns_tmp_path)

    for seg in message_segments:
        seg_type = seg.get("type")
        if seg_type not in ("image", "record", "video", "mface"):
            continue
        seg_data = seg.get("data", {})

        if seg_type == "mface":
            # 商城表情包（NapCat 直报 mface）：没有可下载的图，只能用摘要；
            # 摘要若只是“[动画表情]”这类占位词，退化为中性描述。
            summary = str(seg_data.get("summary") or seg_data.get("key") or "")
            media_list.append(("sticker", _sticker_desc_from_summary(summary) or "商城表情包"))
            media_refs.append(None)
            continue

        # 超出描述上限的媒体不再下载：只记录数量，避免一条消息打爆视觉调用
        if len(media_tasks) + len(media_list) >= _MAX_MEDIA_DESC:
            continue

        # NapCat 常直接给出本地文件路径（QQ 缓存目录）。命中时优先复制本地文件，
        # 不再走网络下载，省掉一次远程取图。
        local_file = None
        for key in ("file", "path"):
            candidate = seg_data.get(key)
            if candidate and not str(candidate).startswith(("http://", "https://", "base64://")) and os.path.isfile(str(candidate)):
                local_file = str(candidate)
                break

        file_url = seg_data.get("url") or seg_data.get("path") or seg_data.get("file")
        if seg_type == "record" and file_url and not str(file_url).startswith(("http://", "https://", "base64://")):
            # NapCat 有时只回传文件名或本地路径，通过 get_record 获取可下载文件。
            try:
                napcat_api = get_napcat_http_url()
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{napcat_api}/get_record",
                        params={"file": file_url, "out_format": "wav"},
                        headers={"Authorization": f"Bearer {get_napcat_token()}"},
                        timeout=30,
                    ) as resp:
                        if resp.status == 200:
                            record_result = await resp.json()
                            file_url = record_result.get("data", {}).get("file") or file_url
            except Exception as e:
                logger.warning(f"通过 NapCat 获取语音失败: {e}")

        # 跨容器收图兜底：若既不是本地直接可读文件，也不是 HTTP 直链或 Base64，
        # 则尝试调用 NapCat 的 /get_image 接口获取下载直链或文件路径
        if not local_file and (not file_url or not str(file_url).startswith(("http://", "https://", "base64://"))):
            if seg_type in ("image", "sticker"):
                try:
                    napcat_api = get_napcat_http_url()
                    file_param = seg_data.get("file") or file_url or ""
                    if file_param:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"{napcat_api}/get_image",
                                params={"file": file_param},
                                headers={"Authorization": f"Bearer {get_napcat_token()}"},
                                timeout=30,
                            ) as resp:
                                if resp.status == 200:
                                    img_result = await resp.json()
                                    fetched = img_result.get("data", {}).get("url") or img_result.get("data", {}).get("file")
                                    if fetched:
                                        file_url = fetched
                except Exception as e:
                    logger.warning(f"通过 NapCat 获取图片失败: {e}")

        if not local_file and (not file_url or not str(file_url).startswith(("http://", "https://", "base64://"))):
            if not file_url or not os.path.isfile(str(file_url)):
                logger.warning(f"媒体没有可下载的 URL 或本地文件，类型={seg_type}")
                continue
        tmp_path = None
        owns_tmp_path = False
        try:
            parsed_url = urlparse(str(file_url or local_file))
            query_params = parse_qs(parsed_url.query)
            default_format = os.path.splitext(str(file_url or local_file))[1].lstrip(".") or seg_data.get("format", "amr")
            file_format = query_params.get('format', [default_format])[0]
            file_format = re.sub(r"[^a-zA-Z0-9]", "", file_format).lower() or "amr"

            if local_file:
                # NapCat 已把文件落在本地（同机部署环境），直接读，不复制不下载
                tmp_path = local_file
                owns_tmp_path = False
            elif str(file_url).startswith("base64://"):
                # Base64 编码的二进制数据，直接在内存解码为临时文件
                b64_str = str(file_url)[len("base64://"):]
                raw_bytes = base64.b64decode(b64_str)
                with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_format}") as tmp_file:
                    tmp_file.write(raw_bytes)
                    tmp_path = tmp_file.name
                    owns_tmp_path = True
            elif str(file_url).startswith(("http://", "https://")):
                async with aiohttp.ClientSession() as session:
                    async with session.get(file_url, timeout=30) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"下载媒体失败，HTTP {resp.status}")
                        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_format}") as tmp_file:
                            tmp_file.write(await resp.read())
                            tmp_path = tmp_file.name
                            owns_tmp_path = True
            else:
                tmp_path = str(file_url)
        except Exception as e:
            logger.warning(f"媒体准备失败: {e}")
            if tmp_path and owns_tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            continue

        media_tasks.append(_describe_one(len(media_tasks), seg_type, seg_data, tmp_path, file_format, owns_tmp_path))

    if media_tasks:
        described = await asyncio.gather(*media_tasks)
        for _order, media_type, desc, tmp_path, owns_tmp_path in sorted(described, key=lambda x: x[0]):
            if not media_type or not desc:
                continue
            media_list.append((media_type, desc))
            ref = None
            # 图片/表情包登记为暂存，供本轮动作抉择决定是否收藏
            # （必须在临时文件删除之前登记：暂存会复制一份副本）
            if media_type in ("image", "sticker"):
                try:
                    from core.asset_library import register_pending
                    ref = register_pending(media_type, tmp_path, desc)
                    if ref:
                        logger.info(f"媒体已登记暂存（{ref}）: {desc[:30]}")
                except Exception as e:
                    logger.warning(f"登记暂存媒体失败: {e}")
            media_refs.append(ref)
            if tmp_path and owns_tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.info(f"临时文件删除失败，请及时清理：{tmp_path}")

    # 媒体描述异步执行，期间事件循环仍在处理其他消息；这里更新探针便于观测
    PROBE.bump("message.media_count", len(media_list))

    # 聊天记录摘要与媒体描述是并发跑的，这里统一收取结果
    rich_extras = rich_message.RichExtras()
    rich_failed = False
    if rich_task is not None:
        try:
            rich_extras = await rich_task
        except Exception as e:
            logger.warning(f"[富消息] 解析失败: {e}")
            rich_failed = True
    rich_parts = rich_message.format_rich_parts(
        rich_extras,
        mini_max_chars=_cfg_int("qq_mini_program_max_chars", 200, 40, 600),
    )
    if rich_failed and not rich_parts and rich_preview:
        # 解析异常时宁可退化成卡片提示，也不要把整条消息丢掉
        rich_parts = [rich_preview]
    rich_text = "、".join(rich_parts)

    # 若为纯多媒体消息（无正文文字），向事件总线广播媒体摘要供面板显示
    if not clean_text and media_list:
        first_type, first_desc = media_list[0]
        BUS.message.emit(sender_name, f"[{first_type}] {first_desc}", "QQ")

    # ---------- 构建自然语言增强输入 ----------
    # 媒体与富内容（小程序/聊天记录摘要）共用同一套“发送了……”措辞。
    media_parts = [_media_phrase(m_type, d) for m_type, d in media_list]
    media_text = "、".join(media_parts)
    content_text = "、".join(p for p in (media_text, rich_text) if p)
    if content_text:
        target_clause = f"对{target_name}" if target_name else ""
        if clean_text:  # 有文字伴随
            augmented_input = (
                f"{sender_name}{target_clause}说：“{clean_text}”，"
                f"同时发送了{content_text}"
            )
        else:           # 纯媒体 / 纯富内容
            augmented_input = f"{sender_name}{target_clause}发送了{content_text}"
    else:
        # 没有媒体也没有富内容，沿用原有文本格式（带@和引号）
        augmented_input = build_augmented_input(
            sender_name, clean_text, mentions, group_id, is_mentioned_me
        )

    # 消息去重（基于发送者+内容）
    msg_key = (sender_id, clean_text)
    if msg_key in _recent_msg_set and False:            # 暂时关闭重复判定
        logger.info(f"消息疑似刷量（同发送者重复），忽略: {sender_name}: {clean_text}")
        return
    
    # 加入记录
    _recent_msg_list.append(msg_key)
    _recent_msg_set.add(msg_key)
    
    # 超出容量时移除最旧的
    while len(_recent_msg_list) > MAX_RECENT_MESSAGES:
        old_key = _recent_msg_list.pop(0)
        _recent_msg_set.discard(old_key)

    # 空消息守卫：既无正文、也无媒体/富内容、也没有 @ 任何人的事件（如撤回、系统提示）
    # 直接丢弃，绝不把“（没有说话）”送进理解层浪费一次 LLM。
    if not clean_text and not media_list and not rich_parts and not mentions and not is_mentioned_me:
        logger.debug(f"空消息（无文本/无媒体/无@），已忽略: sender={sender_name}")
        PROBE.bump("message.empty_dropped")
        return

    # 纯@无文本（且无多媒体/富内容）→ 简单回复，记录对话并通知，不存入深层记忆
    # 软件打开/对话内时不自动回复：交给行为/动作层决定要不要回应。
    if is_mentioned_me and not clean_text and not media_list and not rich_parts and not mentions:
        if from_flush or app_can_read(group_id):
            line = f"{sender_name}叫了{BOT_NAME}一声"
            _archive_enriched(group_id, message_id, sender_name, "", "叫了一声", line, msg_time)
            if app_can_read(group_id):
                _app_record_message(group_id, message_id, line)
            return
        # 判断是否处于刚被吵醒的迷糊锁定状态
        if BIORHYTHM.woke_by_user and time.time() < BIORHYTHM.awake_until:
            wake_reply = random.choice([
                "唔……刚被叫醒，脑子还迷迷糊糊的，怎么啦？",
                "嗯……还在犯困呢，找我有什么事吗？",
                "哈欠……醒了醒了，怎么啦？"
            ])
        else:
            wake_reply = random.choice([
                "嗯？我在呢~",
                "怎么啦？我在这呢。",
                "在的，找我有什么事吗？"
            ])
        # 被叫的人就在这一句里，直接 @ 回去并引用这条呼叫，避免对方不知道在跟谁说话。
        # 兼容旧发送签名：自定义/测试发送函数可能不接受新参数，退化为普通发送。
        try:
            await send_group_msg(group_id, wake_reply, at_qq=sender_id,
                                 reply_msg_id=message_id or None)
        except TypeError:
            await send_group_msg(group_id, wake_reply)
        from core.llm_interface import add_to_history
        add_to_history(sender_name, f"（叫了{BOT_NAME}一声）", wake_reply, "QQ", target=BOT_NAME)
        line = f"{sender_name}叫了{BOT_NAME}一声"
        _archive_enriched(group_id, message_id, sender_name, "", "叫了一声", line, msg_time)
        if app_can_read(group_id):
            _app_record_message(group_id, message_id, line)
        BUS.message.emit(BOT_NAME, wake_reply, "QQ")
        return

    # 提取引用消息
    quote_id = extract_quote_message_id(message_segments)
    extra_context = ""
    quote_info = None
    quoted_me = False          # 本条消息引用的是不是机器人自己说过的话
    if quote_id:
        quoted_message = await fetch_quoted_message(quote_id, group_id)
        if quoted_message:
            quoted_sender, quoted_text, quoted_sender_id = quoted_message
            quoted_me = (quoted_sender_id == get_bot_qq())
            extra_context = f"{sender_name}引用了{quoted_sender}之前的一句话：'{quoted_text}'"
            # 结构化引用随历史一起保存，供短期上下文还原引用关系
            quote_info = {"sender": quoted_sender, "text": quoted_text}

    logger.info(f"输入: {augmented_input} | 上下文: {extra_context}")

    from core.memory_engine import create_memory, add_link
    from core.virtual_clock import clock
    from core.llm_interface import add_to_history
    from utils.dialogue_state import set_state
    from utils.persistence import save_state

    # 记录本轮的回应目标：被 @ 时直接 @ 回发送者，被引用时引用回原消息；
    # 其余情况既不 @ 人也不引用。认知循环发言时从这里取“最近一条待回应的目标”。
    _remember_reply_target(group_id, sender_id, sender_name, message_id,
                           mention_me=is_mentioned_me, quoted_me=quoted_me)

    # ========== 聊天记录快捷路径 ==========
    # 摘要已经在 _collect_rich_content 里压缩过，不再送给理解层二次拆解；
    # 只把摘要写进短期上下文（对话历史 + 软件上下文）。原始记录已由
    # _archive_preview/_archive_enriched 持久化，供历史翻读与记忆时间前缀使用。
    if rich_extras.has_chat_record and rich_extras.chat_summary:
        await _handle_rich_message_shortcut(
            group_id=group_id,
            message_id=message_id,
            sender_name=sender_name,
            clean_text=clean_text,
            media_list=media_list,
            media_refs=media_refs,
            media_text=media_text,
            rich_text=rich_text,
            target_name=target_name,
            quote_info=quote_info,
            summary=rich_extras.chat_summary,
            total=rich_extras.chat_total,
            event_time=_message_real_time(data),
        )
        return

    user_mem_ids = []
    # ========== 纯媒体快捷路径 ==========
    # 没有正文的图片/表情包无需走理解层：视觉描述已经是完整信息。
    # 这里直接写记忆 + 提取关键词，省掉一次 LLM 调用与一轮“（没有说话）”垃圾记忆。
    if media_list and not clean_text and not rich_parts:
        half_life = 2 * 24 * 3600
        target_clause = f"对{target_name}" if target_name else ""
        for m_type, desc in media_list:
            frag = f"{sender_name}{target_clause}发送了{_media_phrase(m_type, desc)}"
            try:
                user_mem_ids.append(create_memory(frag, half_life=half_life, occurred_at=msg_time))
            except Exception as e:
                logger.warning(f"媒体记忆写入失败: {e}")
        media_keywords = extract_keywords_jieba(media_text)
        if media_keywords:
            from core.cognition import inject_keywords
            inject_keywords(media_keywords, "media")
        # 纯媒体也要进概念层，否则“你发的那个表情包”这类问题永远召回不了。
        # 描述是占位词时跳过：信息量为零，建概念只会污染召回。
        if user_mem_ids:
            real_media = [(mt, d) for mt, d in media_list if not _is_generic_media_desc(d)]
            if real_media:
                try:
                    from core.concept_store import record_concept
                    m_type, m_desc = real_media[0]
                    record_concept(
                        f"{sender_name}发了{_media_phrase(m_type, m_desc)}",
                        user_mem_ids,
                    )
                except Exception as e:
                    logger.warning(f"概念层写入失败: {e}")
        # 媒体描述对应的记忆 ID 回填给暂存条目：收藏时据此贴近原图建链
        _attach_media_memory_ids(media_list, user_mem_ids, media_refs)
        history_content = f"（发送了{media_text}）"
        add_to_history(sender_name, history_content, None, "QQ", quote=quote_info, target=target_name)
        media_line = _app_line_from_message(sender_name, clean_text, media_list, target_name, quote_info)
        _archive_enriched(group_id, message_id, sender_name, clean_text, media_text, media_line, msg_time)
        if app_can_read(group_id):
            _app_record_message(group_id, message_id, media_line)
        logger.info(f"纯媒体消息：0 次理解层，记忆入库 {len(user_mem_ids)} 条")
        BUS.message.emit(BOT_NAME, f"[思考中...]", "QQ")
        return

    full_input = augmented_input
    if extra_context:
        full_input = f"{extra_context}\n{augmented_input}"

    # ========== 理解层：在解析单线程里执行，保证消息顺序且不阻塞事件循环 ==========
    from core.parse_worker import run_in_parse_worker

    def _parse_pipeline():
        """理解层 + 记忆入库 + 概念归档，整段在解析线程里串行执行。

        返回 (mem_fragments, mode, new_state, keywords, concept, time_intent, mem_ids)
        """
        fragments, parsed_mode, parsed_state, parsed_kws, parsed_concept, parsed_time = decompose_input(full_input)
        # 整句原文交给概念路做向量召回（换说法也能找回同一件事）
        try:
            from core.cognition import inject_user_input
            inject_user_input(full_input)
        except Exception as exc:
            logger.warning(f"整句输入注入失败: {exc}")
        from core.memory_engine import create_memory as _create
        mem_ids = []
        life = MODE_HALF_LIFE.get(parsed_mode, 2 * 24 * 3600)
        for frag in fragments:
            try:
                mem_ids.append(_create(frag, half_life=life, occurred_at=msg_time))
            except Exception as exc:
                logger.warning(f"记忆写入失败: {exc}")
        if parsed_concept and mem_ids:
            try:
                from core.concept_store import record_concept
                # 用户原话作为别名，下次换个相近说法也能靠字面命中
                record_concept(parsed_concept, mem_ids, alias=clean_text or full_input)
            except Exception as exc:
                logger.warning(f"概念层写入失败: {exc}")
        return fragments, parsed_mode, parsed_state, parsed_kws, parsed_concept, parsed_time, mem_ids

    (mem_fragments, mode, new_state, llm_keywords,
     concept, time_intent, user_mem_ids) = await run_in_parse_worker(_parse_pipeline)

    # 更新对话状态（写 JSON，轻量，保持原位）
    if new_state:
        set_state(new_state)
        save_state()

    # 时间指代：交给认知循环，供概念路做时间窗筛选（不做词表解析）
    if time_intent and time_intent != "none":
        try:
            from core.cognition import inject_time_intent
            inject_time_intent(time_intent)
        except Exception as e:
            logger.warning(f"时间指代注入失败: {e}")

    # 将关键词追加进信箱：LLM 提取词 + 正文 jieba 词 + 媒体/富内容描述词
    # （旧实现丢弃了 LLM 关键词、只用 jieba；这里两者合并，来源可追溯）
    from core.cognition import inject_keywords
    merged_keywords = []
    for kw in (llm_keywords or []):
        text = str(kw or "").strip()
        if text:
            merged_keywords.append(text)
    for kw in extract_keywords_jieba(clean_text):
        if kw not in merged_keywords:
            merged_keywords.append(kw)
    for kw in extract_keywords_jieba(content_text):
        if kw not in merged_keywords:
            merged_keywords.append(kw)
    if merged_keywords:
        inject_keywords(merged_keywords, "msg")
        # 兼容旧入口（也进消费队列；信箱合并时会按内容去重）
        inject_message_keywords(merged_keywords)

    # 记录用户消息到对话历史（回复由 cognitive_loop 异步补充）
    # 引用与提及对象以结构化字段随消息保存，短期上下文才能还原"对谁说/在回应什么"
    history_content = clean_text.strip()
    if content_text:
        if history_content:
            history_content = f"{history_content}（发送了{content_text}）"
        else:
            history_content = f"（发送了{content_text}）"
    elif not history_content:
        history_content = "（没有说话）"

    # 软件打开且停在这个对话时，把“她读到的这一条”交给主进程；
    # 未打开/不在这个对话时，消息在 ws_handler 就被缓冲，不会走到这里。
    line = _app_line_from_message(
        sender_name, clean_text, media_list, target_name, quote_info,
        extra_text=rich_text,
    )
    _archive_enriched(
        group_id, message_id, sender_name, clean_text,
        content_text, line, msg_time,
    )
    if app_can_read(group_id):
        _app_record_message(group_id, message_id, line)

    add_to_history(sender_name, history_content, None, "QQ", quote=quote_info, target=target_name)

    logger.info(f"理解层完成，记忆入库 {len(user_mem_ids)} 条，关键词: {merged_keywords}")
    BUS.message.emit(BOT_NAME, f"[思考中...]", "QQ")

SEND_ACK_TIMEOUT = 5.0   # 等待 NapCat 回执的秒数；超时即视为未送达
MEDIA_ACK_TIMEOUT = 15.0  # 图片/表情包要经 NapCat 上传到 QQ，5 秒经常不够


async def _send_action_and_wait(websocket, payload: dict, echo: str,
                                timeout: float = None) -> bool:
    """发送 OneBot Action 并等待 NapCat 回执，返回是否确认成功。

    与 fetch_quoted_message 复用同一套 echo→Future 机制（ws_handler 主循环
    已在认领所有带 echo 的响应），因此不需要另开子进程或额外连接。
    仅挂起当前协程，事件循环与其他消息处理照常运行。

    判定标准：response['status'] == 'ok'。超时/断线/异常一律视为未送达。
    失败时把 NapCat 的 status/retcode/message 记下来——只写"未送达"时，
    图片发不出去根本看不出是文件、格式还是尺寸被拒。
    """
    wait = SEND_ACK_TIMEOUT if timeout is None else timeout
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    try:
        async with _action_lock:
            _pending_actions[echo] = future
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        response = await asyncio.wait_for(future, timeout=wait)
        ok = str(response.get("status") or "").lower() == "ok"
        if not ok:
            detail = str(response.get("message") or "")
            hint = ""
            if "EACCES" in detail or "permission denied" in detail.lower():
                hint = (
                    "（NapCat 无法写入自己的 temp 目录：检查 /app/.config/QQ/NapCat/temp "
                    "的属主与权限，或给 NapCat 容器设置 NAPCAT_UID/NAPCAT_GID）"
                )
            logger.warning(
                f"NapCat 拒绝发送（echo={echo}，status={response.get('status')}，"
                f"retcode={response.get('retcode')}，message={detail[:200]}）{hint}"
            )
        return ok
    except asyncio.TimeoutError:
        logger.warning(f"发送未收到回执（echo={echo}，超时 {wait:g}s），按未送达处理")
        return False
    except Exception as e:
        logger.error(f"发送等待回执异常（echo={echo}）: {e}")
        return False
    finally:
        _pending_actions.pop(echo, None)


async def _call_action(websocket, action: str, params: dict = None, timeout: float = 10.0) -> Optional[dict]:
    """调用一个 OneBot Action 并返回 data（失败返回 None）。

    与发送路径共用 echo→Future 与 _action_lock；ws_handler 的消息循环负责
    认领响应，因此调用方必须与主循环并发运行（不能在主循环内 await）。
    """
    global _action_counter
    if websocket is None:
        return None
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _action_counter += 1
    echo = f"nascence-{action}-{_action_counter}"
    try:
        async with _action_lock:
            _pending_actions[echo] = future
            await websocket.send(json.dumps({
                "action": action,
                "params": params or {},
                "echo": echo,
            }, ensure_ascii=False))
        response = await asyncio.wait_for(future, timeout=timeout)
        if str(response.get("status") or "").lower() != "ok":
            logger.warning(f"NapCat Action 失败（{action}）: {response}")
            return None
        data = response.get("data")
        # get_forward_msg 等接口可能直接返回数组；调用方自行判断类型
        return data if isinstance(data, (dict, list)) else {}
    except asyncio.TimeoutError:
        logger.warning(f"NapCat Action 超时（{action}，{timeout:g}s）")
        return None
    except Exception as e:
        logger.warning(f"NapCat Action 异常（{action}）: {e}")
        return None
    finally:
        _pending_actions.pop(echo, None)


async def fetch_login_info(websocket) -> Optional[dict]:
    """从 NapCat 自动获取当前登录账号的 QQ 号与昵称。

    这是 QQ 端名字的唯一来源：后台配置不提供“机器人昵称”，避免把意识体
    名字（BOT_NAME）误当成 QQ 昵称。bot_qq 配置只在尚未连接时兜底。
    """
    data = await _call_action(websocket, "get_login_info", {}, timeout=8.0)
    if not data:
        logger.warning("[QQ身份] 未能通过 get_login_info 获取登录信息，暂用配置兜底")
        return None
    user_id = str(data.get("user_id") or "").strip()
    nickname = str(data.get("nickname") or "").strip()
    if not user_id and not nickname:
        logger.warning("[QQ身份] get_login_info 返回为空")
        return None
    set_login_info(user_id, nickname, source="napcat")
    logger.info(
        f"[QQ身份] 已从 NapCat 自动获取：QQ={user_id or '(未知)'}，"
        f"QQ昵称={nickname or '(未知)'}；意识体名字仍为 {BOT_NAME}"
    )
    return get_qq_identity()


async def send_group_msg(group_id: str, text: str, reply_msg_id: int = None, at_qq=None) -> bool:
    """
    发送群消息，支持引用回复与 @。

    reply_msg_id: 被引用消息的 message_id（整数）
    at_qq: 要 @ 的 QQ 号（单个字符串/整数，或多个的列表）。
           只接受数字 QQ；机器人自己与 "all" 会被剥离。
    返回：是否确认送达（NapCat 回执 status=ok 才算成功）

    调用方必须依据返回值决定是否记入历史：此前无条件记"我说"，
    发送失败时会造成"说过但其实没说出口"的虚假记忆（F20）。
    """
    if is_sleeping():
        logger.debug("睡眠期间禁止发送消息，已拦截。")
        return False
    websocket = _napcat_websocket
    if websocket is None:
        logger.error("消息发送失败：当前没有连接的 NapCat WebSocket")
        return False

    # 文本中的 @名字 先解析成 at 段（只认群成员映射，绝不猜 QQ 号）
    parsed_at, text = _resolve_at_mentions(str(text or ""), group_id)
    # 只 @ 不说话时 NapCat 容易拒绝空文本段：给一个空格兜底
    if not text and parsed_at:
        text = " "

    # 显式 at_qq 与文本解析出的 @ 合并去重
    at_list = list(parsed_at)
    if at_qq is not None:
        candidates = at_qq if isinstance(at_qq, (list, tuple, set)) else [at_qq]
        for item in candidates:
            qq = str(item or "").strip()
            if not qq or qq == get_bot_qq() or qq == "all":
                continue
            if not qq.isdigit():
                continue
            if qq not in at_list:
                at_list.append(qq)

    global _action_counter
    _action_counter += 1
    echo = f"nascence-send-{_action_counter}"

    if reply_msg_id is not None or at_list:
        # 结构化消息段：引用 + @ + 正文。比 CQ 码更稳，也避免正文里出现 CQ 语法时被误解析。
        segments = []
        if reply_msg_id is not None:
            segments.append({"type": "reply", "data": {"id": str(reply_msg_id)}})
        for qq in at_list:
            segments.append({"type": "at", "data": {"qq": qq}})
        segments.append({"type": "text", "data": {"text": (" " if at_list else "") + text}})
        message_payload = segments
    else:
        message_payload = text

    payload = {
        "action": "send_group_msg",
        "params": {
            "group_id": int(group_id),
            "message": message_payload,
        },
        "echo": echo,
    }

    ok = await _send_action_and_wait(websocket, payload, echo)
    if ok:
        logger.info(f"已发送并确认送达（echo={echo}，引用={reply_msg_id}，@={at_list}）: {text}")
        from core.memory_engine import _bump_message_sent, _bump_self_ref
        _bump_message_sent()
        if "我" in text:
            _bump_self_ref()
        if app_can_read(group_id):
            _app_record_message(group_id, None, f"我说：{text}")
        try:
            _chat_store.append({
                "conversation_id": str(group_id), "message_id": "",
                "ts": time.time(), "sender": get_bot_qq_display_name(),
                "text": str(text)[:300], "line": f"我说：{text}",
            })
        except Exception:
            pass
    else:
        logger.warning(f"消息未确认送达（echo={echo}）: {text}")
    return ok

async def send_group_media(group_id: str, file_path: str, caption: str = "", as_sticker: bool = False) -> bool:
    """发送本地图片/表情包（消息段数组形式）。成功返回 True。

    as_sticker=True 时按表情包发送：NapCat 会把该消息段的 sub_type 传给
    createValidSendPicElement，最终落到 QQ 的 picSubType 字段——这才是 QQ
    区分"普通图片"与"收藏表情"的开关。只把文件改成 .gif 并不足以让它显示为表情包。
    """
    if is_sleeping():
        logger.debug("睡眠期间禁止发送媒体，已拦截。")
        return False
    global _action_counter
    websocket = _napcat_websocket
    if websocket is None:
        logger.error("媒体发送失败：当前没有连接的 NapCat WebSocket")
        return False
    if not file_path or not os.path.isfile(str(file_path)):
        logger.error(f"媒体发送失败：文件不存在 {file_path}")
        return False

    segments = []
    if caption:
        segments.append({"type": "text", "data": {"text": caption}})

    # 跨容器支持：采用 base64:// 协议发送二进制图片，完全不依赖容器间共享文件系统，
    # NapCat 无论在宿主机、其他容器或远程均可从 WebSocket 内存解码并上传至 QQ。
    try:
        file_size = os.path.getsize(str(file_path))
        if file_size <= 0:
            logger.error(f"媒体发送失败：文件为空 {file_path}")
            return False
        with open(str(file_path), "rb") as f:
            b64_content = base64.b64encode(f.read()).decode("utf-8")
        image_data = {"file": f"base64://{b64_content}"}
        logger.info(
            f"发送媒体：{os.path.basename(str(file_path))}"
            f"（{file_size / 1048576:.2f} MB，base64 {len(b64_content) / 1048576:.2f} MB，"
            f"sticker={as_sticker}）"
        )
    except Exception as e:
        logger.error(f"媒体 Base64 编码失败 ({file_path}): {e}")
        return False

    if as_sticker:
        # OneBot 图片子类型 1 = 表情包，NapCat 会映射为 picSubType
        image_data["sub_type"] = 1
    segments.append({"type": "image", "data": image_data})

    _action_counter += 1
    echo = f"nascence-media-{_action_counter}"
    payload = {
        "action": "send_group_msg",
        "params": {"group_id": int(group_id), "message": segments},
        "echo": echo,
    }
    # 与文本发送一样等待回执：此前只判断"本地 send 未抛异常"就返回 True，
    # NapCat 拒绝时也会被记为发送成功（F20）。
    ok = await _send_action_and_wait(websocket, payload, echo, timeout=MEDIA_ACK_TIMEOUT)
    if ok:
        logger.info(f"已发送并确认送达媒体（echo={echo}）: {file_path}")
        from core.memory_engine import _bump_message_sent
        _bump_message_sent()
        if app_can_read(group_id):
            tag = "表情包" if as_sticker else "图片"
            _app_record_message(group_id, None, f"我发了一个{tag}")
        try:
            tag = "表情包" if as_sticker else "图片"
            _chat_store.append({
                "conversation_id": str(group_id), "message_id": "",
                "ts": time.time(), "sender": get_bot_qq_display_name(),
                "text": tag, "line": f"我发了一个{tag}",
            })
        except Exception:
            pass
    else:
        logger.warning(f"媒体未确认送达（echo={echo}）: {file_path}")
    return ok

async def fetch_quoted_message(msg_id: str, fallback_group_id: str = "") -> Optional[Tuple[str, str, str]]:
    """通过 WebSocket 获取引用消息，返回（原发送者名称，消息正文，原发送者 QQ）。"""
    global _action_counter
    websocket = _napcat_websocket
    if websocket is None:
        logger.error("获取引用消息失败：当前没有连接的 NapCat WebSocket")
        return None

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    try:
        async with _action_lock:
            _action_counter += 1
            echo = f"nascence-get-msg-{_action_counter}"
            _pending_actions[echo] = future
            await websocket.send(json.dumps({
                "action": "get_msg",
                "params": {"message_id": int(msg_id)},
                "echo": echo,
            }, ensure_ascii=False))
        response = await asyncio.wait_for(future, timeout=10)
        if response.get("status") != "ok":
            logger.error("获取引用消息失败 (msg_id=%s): %s", msg_id, response)
            return None
        message_data = response.get("data", {})
        raw_msg = message_data.get("raw_message", "")
        if not raw_msg:
            message = message_data.get("message", "")
            if isinstance(message, list):
                raw_msg = "".join(
                    str(segment.get("data", {}).get("text", ""))
                    for segment in message
                    if segment.get("type") == "text"
                )
            else:
                raw_msg = str(message)
        clean_msg, _ = parse_cq_code(raw_msg)
        if not clean_msg:
            return None

        quoted_sender_data = message_data.get("sender", {}) or {}
        quoted_sender_id = str(
            quoted_sender_data.get("user_id")
            or message_data.get("user_id")
            or ""
        )
        quoted_group_id = str(message_data.get("group_id") or fallback_group_id)
        if quoted_sender_id == get_bot_qq():
            # QQ 端的引用显示用自动获取的 QQ 昵称；意识体名字 BOT_NAME
            # 只用于记忆与思考，不冒充平台账号名。
            quoted_sender = get_bot_qq_name() or BOT_NAME
        else:
            quoted_sender = (
                manifest.name_map.get(quoted_group_id, {}).get(quoted_sender_id)
                or quoted_sender_data.get("card")
                or quoted_sender_data.get("nickname")
                or quoted_sender_id
                or "未知发送者"
            )

        logger.info(
            "通过 WebSocket 获取引用消息成功 (msg_id=%s, sender=%s): %s",
            msg_id,
            quoted_sender,
            clean_msg,
        )
        return quoted_sender, clean_msg, quoted_sender_id
    except asyncio.TimeoutError:
        logger.error("获取引用消息超时 (msg_id=%s)", msg_id)
    except Exception as e:
        logger.error(f"获取引用消息失败 (msg_id={msg_id}): {e}")
    finally:
        if 'echo' in locals():
            _pending_actions.pop(echo, None)
    return None

# ---------- WebSocket 服务器 ----------
def _start_cognitive_loop():
    """确保全局认知循环已启动。

    认知循环不再由 QQ 连接持有：QQ 只是众多并行软件之一，连接/断开
    不改变唯一认知循环；语言模型始终是全局 primary。
    """
    try:
        import panel_runtime
        panel_runtime.RUNTIME.start_cognitive()
    except Exception as e:
        logger.warning("[认知循环] 启动全局认知循环失败: %s", e)
        return
    logger.info("[认知循环] 已确保全局认知循环运行（QQ 仅作为软件接入）")


async def _bootstrap_connection(websocket):
    """连接就绪后先取 QQ 身份，再启动认知循环。

    get_login_info 的响应由 ws_handler 的消息循环认领，因此这里必须作为
    独立 task 与消息循环并发运行，不能在消息循环里同步 await。
    被取消（连接已断开）时不启动认知循环。
    """
    await fetch_login_info(websocket)
    if _napcat_websocket is websocket:
        _start_cognitive_loop()


async def ws_handler(websocket):
    """处理 NapCat 发来的 WebSocket 连接"""
    request = getattr(websocket, "request", None)
    request_path = getattr(request, "path", None) or getattr(websocket, "path", "/")
    headers = getattr(request, "headers", {}) if request else getattr(websocket, "request_headers", {})
    authorization = headers.get("Authorization", "") if headers else ""
    query_token = ""
    if "?" in request_path:
        request_path, query = request_path.split("?", 1)
        query_token = parse_qs(query).get("access_token", [""])[0]
    header_token = authorization.removeprefix("Bearer ").strip()
    if request_path != WS_PATH:
        logger.warning("拒绝非 /ws WebSocket 连接: %s", request_path)
        await websocket.close(code=1008, reason="invalid path")
        return
    ws_token = get_napcat_token()
    if ws_token and header_token != ws_token and query_token != ws_token:
        logger.warning("拒绝未通过 token 校验的 WebSocket 连接")
        await websocket.close(code=1008, reason="invalid token")
        return
    global _napcat_websocket, _cognitive_task
    _napcat_websocket = websocket
    logger.info("NapCat 已连接，已启用 WebSocket Action 发送")

    # 先并发获取 QQ 身份，拿到后再启动认知循环（避免用配置兜底号误判自己）
    startup_task = asyncio.create_task(_bootstrap_connection(websocket))

    message_tasks = set()
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                echo = data.get("echo")
                if echo in _pending_actions:
                    future = _pending_actions.pop(echo)
                    if not future.done():
                        future.set_result(data)
                    continue
                if data.get("post_type") == "message" and data.get("message_type") == "group":
                    group_id = str(data.get("group_id") or "")
                    if group_id not in manifest.whitelist:
                        logger.debug(f"群 {group_id} 不在白名单，忽略")
                        continue
                    if app_can_read(group_id):
                        task = asyncio.create_task(handle_group_message(data))
                        message_tasks.add(task)
                        task.add_done_callback(message_tasks.discard)
                    else:
                        # 软件未打开或不在这个对话：只累计未读，不解析正文
                        app_buffer_message(data)
            except json.JSONDecodeError:
                logger.warning("收到非JSON消息")
            except Exception as e:
                logger.error(f"消息处理异常: {e}")
    except websockets.exceptions.ConnectionClosed:
        logger.warning("NapCat 连接已断开")
    except Exception as e:
        logger.error(f"WebSocket 处理异常: {e}")
    finally:
        # 连接在身份获取完成前就断开：取消启动任务，别在断线后再拉起认知循环
        if startup_task and not startup_task.done():
            startup_task.cancel()
            try:
                await startup_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        elif startup_task and startup_task.done() and not startup_task.cancelled():
            try:
                startup_task.exception()
            except Exception:
                pass

        if _napcat_websocket is websocket:
            _napcat_websocket = None

        # NapCat 断开：认知循环完成当前轮（输出回复，不复搜）再释放
        if _cognitive_task and not _cognitive_task.done():
            from core.cognition import request_graceful_stop
            request_graceful_stop()
            logger.info("[认知循环] NapCat 断开，等待当前轮完成…")
            try:
                await asyncio.wait_for(_cognitive_task, timeout=60)
            except asyncio.TimeoutError:
                _cognitive_task.cancel()
                try:
                    await _cognitive_task
                except asyncio.CancelledError:
                    pass
                logger.warning("[认知循环] 超时强制取消")
            _cognitive_task = None
            logger.info("[认知循环] 已释放")

        # NapCat 断开相当于她已经不在使用 QQ：软件状态回到关闭，
        # 未读缓冲保留，重连后由本地软件门控重新决定是否打开。
        try:
            if is_app_open():
                await app_close()
        except Exception as e:
            logger.warning(f"[QQ软件] 断开时关闭软件失败: {e}")

        for future in list(_pending_actions.values()):
            if not future.done():
                future.set_exception(ConnectionError("NapCat WebSocket 已断开"))
        _pending_actions.clear()
        if message_tasks:
            await asyncio.gather(*message_tasks, return_exceptions=True)
        logger.info("NapCat WebSocket 已释放")

def request_shutdown():
    """请求 QQ 服务优雅停止（由控制面板在 GUI 线程内 call_soon_threadsafe 触发）。

    仅置位停止信号；start_server 检测到后先等认知循环完成当前轮，再断开 NapCat。
    在事件循环线程中调用，Event.set() 线程安全。
    """
    if _shutdown_event is not None:
        _shutdown_event.set()

async def start_server():
    """启动 WebSocket 服务器，附带定时保存"""
    logger.info(f"当前记忆数: {len(memories)}")
    if not memories:
        logger.warning("[主动发言] 记忆库为空，发散任务将空闲等待")
    logger.info(f"启动 WebSocket 服务器: ws://{WS_HOST}:{WS_PORT}{WS_PATH}")

    global _shutdown_event, _napcat_websocket, _cognitive_task
    _shutdown_event = asyncio.Event()

    # ========== 后台定时保存任务 ==========
    async def auto_save():
        """每10分钟自动保存一次记忆和状态。

        下沉不再在这里先做一遍：热集淘汰由 HotSetPlanner 定时规划、
        MemoryWriter 统一落库，这里只负责保存快照。
        """
        while True:
            try:
                await asyncio.sleep(600)  # 10分钟
                from utils.persistence import save_all_data, save_state
                from core.memory_writer import PLANNER
                # 应用规划器算好的淘汰名单（若期间条目被访问，CAS 会跳过）
                try:
                    PLANNER.apply_plan(PLANNER.take_plan())
                except Exception as e:
                    logger.error(f"[定时下沉] 应用淘汰计划失败: {e}")
                save_all_data()
                save_state()
                logger.info("定时保存完成")
                try:
                    from utils.metrics_probe import PROBE
                    snap = PROBE.snapshot()
                    counters = snap.get("counters", {})
                    logger.info(
                        "[观测] LLM调用=%s 视觉=%s(缓存命中%s) 事件循环阻塞p95=%sms 锁等待p95=%sms",
                        counters.get("llm.calls", 0),
                        counters.get("vision.calls", 0),
                        counters.get("vision.cache_hit", 0),
                        snap.get("timings", {}).get("message.handle_ms", {}).get("p95_ms", 0),
                        snap.get("timings", {}).get("lock.data.wait", {}).get("p95_ms", 0),
                    )
                except Exception:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时保存失败: {e}")
    
    save_task = asyncio.create_task(auto_save())

    # 浅层意识发散任务（记忆热身，认知循环随 NapCat 连接启动）
    drift_task = asyncio.create_task(drift_loop())

    global _is_shutting_down
    _is_shutting_down = False

    # 针对 Windows 平台 ProactorEventLoop 关机时底层 IOCP 的 _attach AssertionError 竞态进行静默过滤
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    def _graceful_exception_handler(current_loop, context):
        exc = context.get("exception")
        handle = str(context.get("handle", ""))
        if isinstance(exc, AssertionError) and "_attach" in handle:
            return
        if "BaseProactorEventLoop._start_serving" in handle:
            return
        if orig_handler:
            orig_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(_graceful_exception_handler)

    # 扩容 WebSocket 单帧上限至 32MB（默认仅 1MB），支持收发 Base64 图片与表情包不被截断
    server = await websockets.serve(ws_handler, WS_HOST, WS_PORT, max_size=32 * 1024 * 1024)
    try:
        logger.info(f"WebSocket 服务器已启动，等待 NapCat 连接: ws://{WS_HOST}:{WS_PORT}{WS_PATH}")
        # 服务器已就绪：此时补做睡眠维护，保证"启动即睡眠"也能被 @ 唤醒。
        # 若在 start_server 之前做，这十几秒里 NapCat 连不上来。
        if is_sleeping() and not _sleep_maintenance_done:
            schedule_sleep_maintenance()
        await _shutdown_event.wait()

        # ========== 服务停止：优雅关停 ==========
        _is_shutting_down = True
        logger.info("[服务停止] 请求认知循环完成当前轮…")
        from core.cognition import request_graceful_stop
        request_graceful_stop()
        if _cognitive_task and not _cognitive_task.done():
            try:
                # 认知循环当前轮含多次 LLM 调用，收尾时间收紧到 20 秒，
                # 避免单次 API 挂起把整个关停拖满一分钟以上。
                await asyncio.wait_for(_cognitive_task, timeout=20)
            except asyncio.TimeoutError:
                _cognitive_task.cancel()
                try:
                    await _cognitive_task
                except asyncio.CancelledError:
                    pass
                logger.warning("[服务停止] 认知循环超时强制取消")
            _cognitive_task = None
            logger.info("[服务停止] 认知循环已结束")
        else:
            logger.info("[服务停止] 认知循环未运行")

        # 认知循环结束后先断开 NapCat
        if _napcat_websocket is not None:
            try:
                await _napcat_websocket.close(code=1000, reason="service stopping")
                logger.info("[服务停止] NapCat 已断开")
            except Exception:
                logger.warning("[服务停止] NapCat 断开失败（可能已断开）")

        # 显式关闭监听服务器并等待底层资源释放
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.05)  # 留出短暂缓冲排空底层残余事件
    finally:
        _is_shutting_down = True
        global _final_save_done
        _napcat_websocket = None
        _final_save_done = False
        save_task.cancel()
        drift_task.cancel()
        if _cognitive_task and not _cognitive_task.done():
            _cognitive_task.cancel()
            try:
                await _cognitive_task
            except Exception:
                pass
        await asyncio.gather(save_task, drift_task, return_exceptions=True)
        try:
            server.close()
            await server.wait_closed()
        except Exception:
            pass
        loop.set_exception_handler(orig_handler)
        if not _final_save_done:
            _final_save_done = True
            from utils.persistence import save_all_data, save_state
            # 关停的最后一次保存必须落盘，绕过节流
            save_all_data(force=True)
            save_state()
            # 写者/规划器/解析线程收尾：stop 会清空队列并等待 commit
            try:
                from core.memory_writer import stop_default_workers
                stop_default_workers()
            except Exception as e:
                logger.warning(f"[服务停止] 记忆写者关闭失败: {e}")
            try:
                from core.parse_worker import stop_parse_worker
                stop_parse_worker()
            except Exception:
                pass
            logger.info("最终保存：记忆和状态已持久化")

# ---------- 启动入口 ----------
def main():
    try:
        # ========== 初始化记忆库（与 main.py 保持一致）==========
        from core.memory_engine import memories, get_model
        from utils.persistence import load_all_data, load_state, save_all_data, save_state
        from core.virtual_clock import clock
        from main import cold_start_batch_injection
        from utils.monitor import monitor_start
        from core.memory_engine import log_metrics_status
        from core.memory_writer import start_default_workers

        # 设置时间倍速（1倍）
        clock.enable_qq_mode()
        print(f"当前时间倍速：{clock.set_speed(1)}")

        # 启用服务端检测窗
        monitor_start()

        # 初始化语义模型
        get_model()

        # 加载记忆和状态
        load_all_data()
        load_state()
        log_metrics_status()
        start_default_workers()

        # 加载短期对话历史
        from core.llm_interface import load_dialogue_history
        load_dialogue_history()

        # 初始化指标计数器（从基线恢复累计值）
        from core.memory_engine import _init_metrics_counters
        _init_metrics_counters()

        # 如果记忆库为空，注入冷启动记忆
        if not memories:
            cold_start_batch_injection()
            save_all_data()
            save_state()
            print("[初始化] 冷启动记忆已注入")
        else:
            print(f"[初始化] 已加载 {len(memories)} 条记忆")

        # ========== 启动 WebSocket 服务器 ==========
        init_sleep_state()
        logger.info(f"启动 WebSocket 服务器: ws://{WS_HOST}:{WS_PORT}")
        asyncio.run(start_server())

    except KeyboardInterrupt:
        logger.info("Bot 已停止")
    except Exception as e:
        logger.error(f"启动失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
