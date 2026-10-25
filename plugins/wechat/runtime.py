# plugins/wechat/runtime.py
# ========================================================================
# 微信插件运行时：本地桥接 WebSocket 服务端。
#
# 微信协议不像 OneBot 那样统一，因此这里定义一层“微信桥接协议”：
# 由使用者选择的微信桥（WeChatFerry / ntchat / 其他本地桥）作为客户端
# 连入 ws://<host>:6701/ws，按 plugins/wechat/BRIDGE.md 收发 JSON。
#
# 与 QQ 插件一致：
#   - 软件默认关闭，未打开时只缓冲未读；
#   - 打开软件/对话后内容才进入主进程；
#   - 微信昵称由桥自动上报，后台不配置平台昵称。
# ========================================================================

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs

import websockets

from config.api_config import config
from config.constants import BOT_NAME
from core.biorhythm import BIORHYTHM
from core.llm_interface import add_to_history
from core.memory_engine import create_memory
from core.virtual_clock import clock
from utils.event_bus import BUS
from plugins.chat_store import ChatStore

PROJECT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = str(PROJECT_DIR / "config" / "wechat_manifest.json")
CONFIG_EXAMPLE_PATH = str(PROJECT_DIR / "config" / "wechat_manifest.example.json")

logger = logging.getLogger("WeChat")

_is_shutting_down = False
_napcat_like_ws = None
_shutdown_event = None
_cognitive_task = None
_action_lock = asyncio.Lock()
_action_counter = 0
_pending_actions = {}
_seen_ids = deque(maxlen=500)
_seen_id_set = set()
_seen_ts = {}


def _cfg_str(key, fallback):
    try:
        value = config.get(key)
    except Exception:
        value = None
    return str(value or fallback)


def get_ws_host():
    return _cfg_str("wechat_ws_host", "0.0.0.0")


def get_ws_port():
    try:
        return int(config.get("wechat_ws_port", 6701))
    except (TypeError, ValueError):
        return 6701


def get_token():
    return _cfg_str("wechat_token", "Nascence")


def get_active_conversation():
    with _app_lock:
        return str(_app_current or "")


# ---------- 微信身份（桥自动上报）----------
_identity_lock = threading.RLock()
_self_identity = {"id": "", "name": ""}


def get_self_identity() -> dict:
    with _identity_lock:
        return dict(_self_identity)


def set_self_identity(user_id: str, name: str):
    with _identity_lock:
        if user_id:
            _self_identity["id"] = str(user_id).strip()
        if name:
            _self_identity["name"] = str(name).strip()


def get_self_id() -> str:
    with _identity_lock:
        return _self_identity["id"]


def get_self_name() -> str:
    with _identity_lock:
        return _self_identity["name"]


# ---------- 清单 ----------
class WeChatManifest:
    def __init__(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            import shutil
            if os.path.exists(CONFIG_EXAMPLE_PATH):
                shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"whitelist_conversations": [], "conversation_names": {}, "name_mapping": {}}, f,
                          ensure_ascii=False, indent=2)
            data = {"whitelist_conversations": [], "conversation_names": {}, "name_mapping": {}}
        except json.JSONDecodeError as e:
            logger.error("微信清单解析失败：%s", e)
            data = {}
        self.whitelist = {str(x) for x in (data.get("whitelist_conversations") or [])}
        self.conversation_names = {
            str(k): str(v) for k, v in (data.get("conversation_names") or {}).items()
        }
        self.name_map = data.get("name_mapping") or {}


manifest = WeChatManifest()


# ---------- 软件状态 ----------
_app_lock = threading.RLock()
_app_open = False
_app_current = ""
_app_unread = {}
_app_buffer = {}
_app_transcript = {}
_app_read_start = {}
_app_seen_transcript = set()
_app_seen_order = deque(maxlen=500)
_app_flush_tasks = set()
_app_at_bottom = {}
_app_archive_cursor = {}
_chat_store = ChatStore("wechat_chat_history")
_APP_TRANSCRIPT_MAX = 120


def _app_context():
    from core.apps import APP_CONTEXT
    return APP_CONTEXT


def _conversation_ids():
    with _app_lock:
        ids = {str(x) for x in manifest.whitelist}
        ids.update(str(x) for x in _app_unread)
        ids.update(str(x) for x in _app_buffer)
        ids.update(str(x) for x in _app_transcript)
        return sorted(ids)


def _conversation_label(conversation_id: str) -> str:
    conversation_id = str(conversation_id or "")
    name = manifest.conversation_names.get(conversation_id)
    if name:
        return name
    ids = _conversation_ids()
    try:
        return f"对话{ids.index(conversation_id) + 1}"
    except ValueError:
        return "一个对话"


def _app_conversation_list():
    conversations = []
    for index, conversation_id in enumerate(_conversation_ids(), start=1):
        with _app_lock:
            unread = int(_app_unread.get(conversation_id, 0))
        conversations.append({
            "id": conversation_id,
            "label": manifest.conversation_names.get(conversation_id) or f"对话{index}",
            "unread": unread,
        })
    return conversations


def is_app_open() -> bool:
    with _app_lock:
        return bool(_app_open)


def get_current_conversation() -> str:
    with _app_lock:
        return str(_app_current or "")


def app_can_read(conversation_id: str) -> bool:
    with _app_lock:
        return bool(_app_open and str(_app_current) == str(conversation_id))


def app_status() -> dict:
    conversations = _app_conversation_list()
    with _app_lock:
        return {
            "id": "wechat",
            "label": "微信",
            "open": bool(_app_open),
            "unread": sum(int(v) for v in _app_unread.values()),
            "current_conversation": str(_app_current or ""),
            "at_bottom": bool(_app_at_bottom.get(str(_app_current or ""), True)),
            "conversations": conversations,
            "identity": get_self_identity(),
        }


def _app_mark_seen(message_id: str) -> bool:
    message_id = str(message_id or "")
    if not message_id:
        return False
    with _app_lock:
        if message_id in _app_seen_transcript:
            return True
        _app_seen_transcript.add(message_id)
        _app_seen_order.append(message_id)
        if len(_app_seen_transcript) > 600:
            _app_seen_transcript.clear()
            _app_seen_transcript.update(_app_seen_order)
        return False


def _app_record_message(conversation_id: str, message_id: str, line: str, push: bool = True):
    conversation_id = str(conversation_id or "")
    line = str(line or "").strip()
    if not conversation_id or not line:
        return
    if _app_mark_seen(message_id):
        return
    with _app_lock:
        transcript = _app_transcript.setdefault(conversation_id, deque(maxlen=_APP_TRANSCRIPT_MAX))
        at_bottom = bool(_app_at_bottom.get(conversation_id, True))
        overflow = transcript.maxlen is not None and len(transcript) >= transcript.maxlen
        transcript.append(line)
        if at_bottom:
            _app_read_start[conversation_id] = max(0, len(transcript) - 6)
        else:
            # 正在往上翻时来了新消息：视图停在原处，不把阅读位置拽回底部。
            start = int(_app_read_start.get(conversation_id, max(0, len(transcript) - 6)))
            if overflow:
                start = max(0, start - 1)
            _app_read_start[conversation_id] = min(start, len(transcript))
        _app_at_bottom[conversation_id] = at_bottom
        can_push = bool(_app_open and str(_app_current) == conversation_id)
    if push and can_push:
        _app_context().remember(line)


def _media_text(media) -> str:
    parts = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "image").lower()
        label = {
            "image": "图片", "sticker": "表情包", "record": "语音", "video": "视频",
            "file": "文件", "link": "链接",
        }.get(kind, kind)
        desc = str(item.get("description") or "").strip()
        parts.append(f"{label}，内容是“{desc}”" if desc else f"一个{label}")
    return "、".join(parts)


def _message_line(data: dict, sender_name: str) -> str:
    text = str(data.get("text") or "").strip()
    media = data.get("media") or []
    media_text = _media_text(media)
    if media_text:
        text = f"{text}（发送了{media_text}）" if text else f"（发送了{media_text}）"
    if not text:
        text = "（没有说话）"
    return f"{sender_name}说：{text}"


def _preview(data: dict) -> str:
    sender = str(data.get("sender_name") or data.get("sender_id") or "某人")
    return _message_line(data, sender)


def _message_real_time(data: dict) -> float:
    raw = data.get("timestamp") or data.get("time") or time.time()
    try:
        value = float(raw)
        if value > 1e12:
            value /= 1000.0
        return value
    except (TypeError, ValueError):
        return time.time()


def _archive_preview(data: dict):
    conversation_id = str(data.get("conversation_id") or "")
    message_id = str(data.get("message_id") or "")
    sender = str(data.get("sender_name") or data.get("sender_id") or "某人")
    text = str(data.get("text") or "")
    if len(text) > 300:
        text = text[:300] + "…"
    _chat_store.append({
        "conversation_id": conversation_id,
        "message_id": message_id,
        "ts": _message_real_time(data),
        "sender": sender,
        "text": text,
        "media_text": _media_text(data.get("media") or []),
        "line": _preview(data),
    })


def _archive_enriched(conversation_id: str, message_id: str, sender: str,
                      text: str, media_text: str, line: str, ts: float):
    _chat_store.append({
        "conversation_id": str(conversation_id or ""),
        "message_id": str(message_id or ""),
        "ts": ts,
        "sender": str(sender or ""),
        "text": str(text or "")[:300],
        "media_text": str(media_text or ""),
        "line": str(line or ""),
    })


def app_buffer_message(data: dict) -> bool:
    conversation_id = str(data.get("conversation_id") or "")
    message_id = str(data.get("message_id") or "")
    if _is_duplicate(message_id):
        return False
    _archive_preview(data)
    try:
        limit = max(10, int(config.get("wechat_unread_buffer_max", 200)))
    except (TypeError, ValueError):
        limit = 200
    with _app_lock:
        buffer = _app_buffer.setdefault(conversation_id, deque(maxlen=limit))
        buffer.append((message_id, data, _preview(data)))
        _app_unread[conversation_id] = int(_app_unread.get(conversation_id, 0)) + 1
        unread_now = _app_unread[conversation_id]
    try:
        BIORHYTHM.note_activity()
    except Exception:
        pass
    logger.info("[微信] 未读+1（%s，%s 条）", _conversation_label(conversation_id), unread_now)
    return True


async def app_open() -> dict:
    global _app_open
    with _app_lock:
        was_open = _app_open
        _app_open = True
        total = sum(int(v) for v in _app_unread.values())
    conversations = _app_conversation_list()
    detail = "微信已经打开了" if was_open else (
        f"我打开了微信，有 {total} 条未读消息" if total else "我打开了微信，没有未读消息"
    )
    lines = [f"{c['label']}有 {c['unread']} 条未读" for c in conversations if c["unread"] > 0]
    return {"ok": True, "detail": detail, "content_lines": lines, "conversations": conversations}


async def app_close() -> dict:
    global _app_open, _app_current
    with _app_lock:
        _app_open = False
        _app_current = ""
    return {"ok": True, "detail": "我关掉了微信"}


async def app_close_conversation() -> dict:
    global _app_current
    with _app_lock:
        if not _app_open:
            return {"ok": False, "detail": "微信还没有打开"}
        if not _app_current:
            return {"ok": True, "detail": "已经在微信首页了"}
        _app_current = ""
    return {"ok": True, "detail": "我退出了这个对话，回到了微信首页"}


async def app_open_conversation(conversation_id: str) -> dict:
    global _app_current
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return {"ok": False, "detail": "没有指定要打开哪个对话"}
    if not is_app_open():
        await app_open()
    if conversation_id not in _conversation_ids():
        return {"ok": False, "detail": f"找不到对话 {conversation_id}"}

    with _app_lock:
        _app_current = conversation_id
        pending = list(_app_buffer.pop(conversation_id, []))
        _app_unread[conversation_id] = 0
        transcript = _app_transcript.setdefault(conversation_id, deque(maxlen=_APP_TRANSCRIPT_MAX))
        previews = []
        archived = _chat_store.tail(conversation_id, max(20, len(pending) + 6)) if not transcript else []
        if archived:
            for record in archived:
                record_id = str(record.get("message_id") or "")
                if record_id:
                    _app_mark_seen(record_id)
                line = str(record.get("line") or "").strip()
                if line:
                    transcript.append(line)
        for message_id, data, preview in pending:
            if _app_mark_seen(message_id):
                previews.append(preview)
                continue
            transcript.append(preview)
            previews.append(preview)
        _app_read_start[conversation_id] = max(0, len(transcript) - 6)
        _app_archive_cursor[conversation_id] = (
            _chat_store.cursor(archived[0]) if archived else None
        )
        _app_at_bottom[conversation_id] = True
        recent = list(transcript)[-6:]

    for _mid, data, _preview_text in pending:
        task = asyncio.create_task(handle_message(data, from_flush=True))
        _app_flush_tasks.add(task)
        task.add_done_callback(_app_flush_tasks.discard)

    label = _conversation_label(conversation_id)
    detail = f"我点开了{label}的对话" + (f"，看到 {len(previews)} 条未读" if previews else "")
    content_lines = list(recent)
    transcript_set = set(transcript)
    for line in previews[-8:]:
        if line not in transcript_set and line not in content_lines:
            content_lines.append(line)
    return {
        "ok": True,
        "detail": detail,
        "conversation": conversation_id,
        "content_lines": content_lines[-12:],
    }


async def app_read_earlier(conversation_id: str = "") -> dict:
    conversation_id = str(conversation_id or get_current_conversation() or "").strip()
    if not is_app_open() or not conversation_id:
        return {"ok": False, "detail": "还没打开任何对话"}
    with _app_lock:
        transcript = _app_transcript.get(conversation_id)
        items = list(transcript) if transcript else []
        start = int(_app_read_start.get(conversation_id, max(0, len(items) - 6)))
        start = max(0, min(start, len(items)))
        new_start = max(0, start - 6)
        chunk = items[new_start:start]
        if chunk:
            _app_read_start[conversation_id] = new_start
            _app_at_bottom[conversation_id] = False
    if chunk:
        return {
            "ok": True, "detail": "我往上翻了翻，看到更早的对话",
            "conversation": conversation_id, "content_lines": chunk,
        }

    # 内存到头：先读本地持久化聊天记录，再向微信桥要更早历史。
    with _app_lock:
        cursor = _app_archive_cursor.get(conversation_id)
        known_lines = {}
        for line in items:
            known_lines[line] = known_lines.get(line, 0) + 1
        known_ids = set(_app_seen_transcript)
    if cursor:
        if isinstance(cursor, (tuple, list)) and len(cursor) >= 2:
            cursor_ts, cursor_seq = float(cursor[0]), int(cursor[1])
        else:
            cursor_ts, cursor_seq = float(cursor), None
    else:
        cursor_ts, cursor_seq = _chat_store.latest_ts(conversation_id) + 1.0, None
    archived_lines = []
    for _ in range(3):
        batch = _chat_store.earlier(
            conversation_id, cursor_ts, 30, before_seq=cursor_seq,
        )
        if not batch:
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
            archived_lines = [
                str(record.get("line") or "").strip() for record in chunk_records
            ]
            cursor_ts, cursor_seq = _chat_store.cursor(chunk_records[0])
            break
    with _app_lock:
        _app_archive_cursor[conversation_id] = (
            (cursor_ts, cursor_seq) if cursor_seq is not None else cursor_ts
        )
        if archived_lines:
            _app_at_bottom[conversation_id] = False
    if archived_lines:
        return {
            "ok": True, "detail": "我往上翻了翻，从聊天记录里看到更早的对话",
            "conversation": conversation_id, "content_lines": archived_lines,
        }

    # 本地记录到头：尝试向微信桥要更早的历史
    data = await _call_bridge("get_history", {
        "conversation_id": conversation_id,
        "limit": 10,
    }, timeout=10.0)
    items = data.get("items") if isinstance(data, dict) else None
    lines = []
    for item in items or []:
        if isinstance(item, dict):
            sender = str(item.get("sender_name") or item.get("sender_id") or "某人")
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"{sender}说：{text}")
    if lines:
        with _app_lock:
            _app_at_bottom[conversation_id] = False
        return {
            "ok": True, "detail": "我从微信桥取回了更早的对话",
            "conversation": conversation_id, "content_lines": lines,
        }
    return {"ok": False, "detail": "再往前就没有更早的对话了"}


async def app_scroll_to_bottom() -> dict:
    conversation_id = str(get_current_conversation() or "").strip()
    if not is_app_open() or not conversation_id:
        return {"ok": False, "detail": "还没打开任何对话"}
    with _app_lock:
        transcript = _app_transcript.get(conversation_id)
        items = list(transcript) if transcript else []
        _app_read_start[conversation_id] = max(0, len(items) - 6)
        _app_at_bottom[conversation_id] = True
    if not items:
        items = [str(r.get("line") or "") for r in _chat_store.tail(conversation_id, 6)]
    return {
        "ok": True,
        "detail": "我回到了最新消息",
        "conversation": conversation_id,
        "content_lines": [line for line in items[-6:] if line],
    }


# ---------- 桥接发送 ----------
async def _call_bridge(action: str, params: dict, timeout: float = 10.0):
    global _action_counter
    websocket = _napcat_like_ws
    if websocket is None:
        return None
    future = asyncio.get_running_loop().create_future()
    _action_counter += 1
    echo = f"nascence-wechat-{action}-{_action_counter}"
    try:
        async with _action_lock:
            _pending_actions[echo] = future
            await websocket.send(json.dumps({
                "action": action, "params": params or {}, "echo": echo,
            }, ensure_ascii=False))
        response = await asyncio.wait_for(future, timeout=timeout)
        if str(response.get("status") or "").lower() != "ok":
            logger.warning("微信桥动作失败（%s）：%s", action, response)
            return None
        data = response.get("data")
        return data if isinstance(data, dict) else {}
    except asyncio.TimeoutError:
        logger.warning("微信桥动作超时（%s，%s 秒）", action, timeout)
        return None
    except Exception as e:
        logger.warning("微信桥动作异常（%s）：%s", action, e)
        return None
    finally:
        _pending_actions.pop(echo, None)


async def send_text(conversation_id: str, text: str) -> bool:
    if not is_app_open() or str(get_current_conversation()) != str(conversation_id):
        return False
    data = await _call_bridge("send_text", {
        "conversation_id": str(conversation_id), "text": str(text),
    }, timeout=10.0)
    ok = data is not None and bool(data.get("ok", True))
    if ok:
        _app_record_message(conversation_id, None, f"我说：{text}")
        _chat_store.append({
            "conversation_id": str(conversation_id), "message_id": "",
            "ts": time.time(), "sender": get_self_name() or "微信",
            "text": str(text)[:300], "line": f"我说：{text}",
        })
        try:
            from core.memory_engine import _bump_message_sent
            _bump_message_sent()
        except Exception:
            pass
    return ok


async def send_media(conversation_id: str, file_path: str, as_sticker: bool = False) -> bool:
    if not is_app_open() or str(get_current_conversation()) != str(conversation_id):
        return False
    if not file_path or not os.path.isfile(str(file_path)):
        return False
    data = await _call_bridge("send_media", {
        "conversation_id": str(conversation_id),
        "path": str(file_path),
        "as_sticker": bool(as_sticker),
    }, timeout=30.0)
    ok = data is not None and bool(data.get("ok", True))
    if ok:
        tag = "表情包" if as_sticker else "图片"
        _app_record_message(conversation_id, None, f"我发了一个{tag}")
        _chat_store.append({
            "conversation_id": str(conversation_id), "message_id": "",
            "ts": time.time(), "sender": get_self_name() or "微信",
            "text": tag, "line": f"我发了一个{tag}",
        })
    return ok


# ---------- 消息处理 ----------
def _is_duplicate(message_id: str) -> bool:
    message_id = str(message_id or "").strip()
    if not message_id:
        return False
    now = time.time()
    while _seen_ids:
        old = _seen_ids[0]
        if now - _seen_ts.get(old, 0) <= 600:
            break
        _seen_ids.popleft()
        _seen_id_set.discard(old)
        _seen_ts.pop(old, None)
    if message_id in _seen_id_set:
        return True
    _seen_ids.append(message_id)
    _seen_id_set.add(message_id)
    _seen_ts[message_id] = now
    return False


async def handle_message(data: dict, from_flush: bool = False):
    conversation_id = str(data.get("conversation_id") or "")
    if conversation_id not in manifest.whitelist:
        return
    message_id = str(data.get("message_id") or "")
    if not from_flush and _is_duplicate(message_id):
        return

    sender_id = str(data.get("sender_id") or "")
    if sender_id and sender_id == get_self_id():
        return
    if data.get("self_id") and not get_self_id():
        set_self_identity(str(data.get("self_id")), str(data.get("self_name") or ""))

    sender_name = (
        manifest.name_map.get(conversation_id, {}).get(sender_id)
        or str(data.get("sender_name") or "")
        or sender_id
        or "某人"
    )
    text = str(data.get("text") or "").strip()
    media = data.get("media") or []
    media_text = _media_text(media)
    _archive_preview(data)
    msg_ts = _message_real_time(data)

    if app_can_read(conversation_id):
        line = _message_line(data, sender_name)
        _archive_enriched(
            conversation_id, message_id, sender_name, text, media_text, line, msg_ts,
        )
        _app_record_message(conversation_id, message_id, line)
        try:
            if text:
                await asyncio.to_thread(
                    create_memory, f"{sender_name}说：{text}", occurred_at=msg_ts
                )
            if media_text:
                await asyncio.to_thread(
                    create_memory, f"{sender_name}发送了{media_text}", occurred_at=msg_ts
                )
            from core.cognition import inject_keywords, extract_keywords_jieba
            keywords = extract_keywords_jieba(text + " " + media_text)
            if keywords:
                inject_keywords(keywords, "wechat")
            history_text = text or f"（发送了{media_text}）"
            add_to_history(sender_name, history_text, None, "WeChat")
            BUS.message.emit(sender_name, history_text, "WeChat")
        except Exception as e:
            logger.warning("微信消息写入失败：%s", e)
    else:
        # 理论上 ws_handler 已经缓冲；这里兜底，避免直接调用时丢内容。
        app_buffer_message(data)


# ---------- WebSocket 服务 ----------
def _start_cognitive_loop():
    global _cognitive_task
    if _cognitive_task is not None and not _cognitive_task.done():
        return
    try:
        import core.cognition as C
        if getattr(C, "_cognitive_running", False):
            logger.warning("[认知循环] 已有认知循环在运行，微信暂不启动第二个循环")
            return
    except Exception:
        pass
    from core.cognition import request_graceful_stop, cognitive_loop
    from plugins.wechat.plugin import get_channel
    request_graceful_stop()
    _cognitive_task = asyncio.create_task(cognitive_loop(channel=get_channel()))
    logger.info("[认知循环] 已随微信桥连接启动")


async def ws_handler(websocket):
    request = getattr(websocket, "request", None)
    path = getattr(request, "path", None) or getattr(websocket, "path", "/")
    headers = getattr(request, "headers", {}) if request else getattr(websocket, "request_headers", {})
    auth = headers.get("Authorization", "") if headers else ""
    token_query = ""
    if "?" in path:
        path, query = path.split("?", 1)
        token_query = parse_qs(query).get("access_token", [""])[0]
    header_token = auth.removeprefix("Bearer ").strip()
    if path != "/ws":
        await websocket.close(code=1008, reason="invalid path")
        return
    token = get_token()
    if token and header_token != token and token_query != token:
        await websocket.close(code=1008, reason="invalid token")
        return

    global _napcat_like_ws, _cognitive_task
    _napcat_like_ws = websocket
    logger.info("微信桥已连接")
    _start_cognitive_loop()

    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            echo = data.get("echo")
            if echo in _pending_actions:
                future = _pending_actions.pop(echo)
                if not future.done():
                    future.set_result(data)
                continue
            post_type = data.get("post_type")
            if post_type == "self_info":
                set_self_identity(str(data.get("self_id") or ""), str(data.get("name") or data.get("nickname") or ""))
                logger.info("微信身份：%s（%s）", get_self_name(), get_self_id())
                continue
            if post_type == "message":
                conversation_id = str(data.get("conversation_id") or "")
                if conversation_id not in manifest.whitelist:
                    continue
                if app_can_read(conversation_id):
                    asyncio.create_task(handle_message(data))
                else:
                    app_buffer_message(data)
    except websockets.exceptions.ConnectionClosed:
        logger.warning("微信桥连接已断开")
    except Exception as e:
        logger.error("微信桥处理异常：%s", e)
    finally:
        if _napcat_like_ws is websocket:
            _napcat_like_ws = None
        if _cognitive_task and not _cognitive_task.done():
            from core.cognition import request_graceful_stop
            request_graceful_stop()
            try:
                await asyncio.wait_for(_cognitive_task, timeout=20)
            except Exception:
                _cognitive_task.cancel()
        _cognitive_task = None
        try:
            if is_app_open():
                await app_close()
        except Exception:
            pass
        for future in list(_pending_actions.values()):
            if not future.done():
                future.set_exception(ConnectionError("微信桥已断开"))
        _pending_actions.clear()
        logger.info("微信桥已释放")


def request_shutdown():
    if _shutdown_event is not None:
        _shutdown_event.set()


async def start_server():
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    host, port = get_ws_host(), get_ws_port()
    logger.info("启动微信桥 WebSocket 服务：ws://%s:%s/ws", host, port)
    server = await websockets.serve(ws_handler, host, port, max_size=32 * 1024 * 1024)
    try:
        await _shutdown_event.wait()
    finally:
        server.close()
        await server.wait_closed()
        if _napcat_like_ws is not None:
            try:
                await _napcat_like_ws.close(code=1000, reason="service stopping")
            except Exception:
                pass
