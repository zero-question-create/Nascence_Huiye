# plugins/qq_napcat/rich_message.py
# ========================================================================
# QQ 富消息解析：小程序卡片 / 合并转发聊天记录。
#
# 与 runtime 的分工：
#   - 本模块只做“解析 + 渲染 + 摘要提示词构造”，不发起网络或 LLM 调用；
#   - runtime 负责取聊天记录（get_forward_msg）并调用插件 LLM 生成摘要。
#
# 约束（与产品需求一致）：
#   1. 小程序只读卡片自带的标题/简介，不打开小程序，也不访问其链接；
#   2. 聊天记录只向内展开一层：内嵌的聊天记录只留占位符，绝不递归；
#   3. 送进摘要模型的条数与字符数有上限，摘要本身也有长度上限，
#      避免把整段聊天记录灌进短期上下文。
# ========================================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, List, Optional, Tuple

MINI_APP_PREFIX = "com.tencent.miniapp"
MULTIMSG_APP_PREFIXES = ("com.tencent.multimsg", "com.tencent.forward")

# 卡片里常见的、本身没有信息量的描述词
_GENERIC_DESCS = {
    "qq小程序", "小程序", "聊天记录", "群聊的聊天记录", "合并转发",
    "查看聊天记录", "卡片消息", "分享",
}

# 单条转发节点正文的硬上限；真正的总量由调用方的 max_chars 控制
_NODE_TEXT_MAX_CHARS = 300


@dataclass
class MiniProgramInfo:
    """小程序卡片里能直接读到的元信息。"""

    app: str = ""
    title: str = ""
    description: str = ""
    source: str = "QQ小程序"
    url: str = ""


@dataclass
class ForwardDigest:
    """展开后的聊天记录摘要素材。

    lines: 已经渲染好、准备交给摘要模型的消息行（不含原始 JSON）
    total: 记录里一共有多少条（NapCat 返回的节点数）
    shown: 实际放进 lines 的条数
    truncated: 是否因为条数/字符上限而截断
    """

    lines: List[str] = field(default_factory=list)
    total: int = 0
    shown: int = 0
    truncated: bool = False


@dataclass
class RichExtras:
    """一条消息里所有富内容的解析结果。"""

    mini_programs: List[MiniProgramInfo] = field(default_factory=list)
    chat_summary: str = ""
    chat_total: int = 0
    chat_truncated: bool = False
    has_chat_record: bool = False


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------
def clip_text(text: Any, max_chars: int) -> str:
    """压成单行并按字符数截断（末尾加省略号）。"""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return ""
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "…"


def decode_json_payload(raw: Any) -> Optional[dict]:
    """把 json 消息段里的 data 解析成 dict。

    NapCat 各版本给的类型并不统一：有的直接给 dict，有的给 JSON 字符串，
    还有的把卡片再套一层 {"data": "{...}"}。这里都做兼容；解析失败返回 None。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8-sig", errors="ignore")
        except Exception:
            return None
    if not isinstance(raw, str):
        return None

    text = raw.strip().lstrip("\ufeff")
    for _ in range(2):
        if not text or text[0] not in "{[":
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, dict):
            inner = parsed.get("data")
            if isinstance(inner, str) and inner.strip().startswith("{"):
                decoded = decode_json_payload(inner)
                if decoded:
                    # 外层的 prompt/desc 常常比内层完整，补进去
                    for key in ("prompt", "desc"):
                        if key in parsed and key not in decoded:
                            decoded[key] = parsed[key]
                    return decoded
            return parsed
        if isinstance(parsed, str):
            text = parsed.strip()
            continue
        return None
    return None


def _iter_nested_dicts(value: Any, depth: int = 0) -> Iterator[dict]:
    """遍历 meta 里任意层级的 dict（只看 dict/list，深度有限）。"""
    if depth > 5:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _iter_nested_dicts(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from _iter_nested_dicts(item, depth + 1)


def _pick_str(mappings: Iterable[dict], keys: Tuple[str, ...]) -> str:
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _strip_card_prefix(text: Any) -> str:
    """去掉卡片摘要前的 “[QQ小程序]”“[聊天记录]” 等方括号前缀。"""
    cleaned = str(text or "").strip()
    return re.sub(r"^\[[^\]]{0,20}\]\s*", "", cleaned).strip()


def _unescape_cq_text(text: str) -> str:
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _clean_cq(text: str) -> str:
    """移除 CQ 码、还原转义字符并压成单行。

    纯文本 CQ 码（图片/表情/语音等）保留一个占位符，
    这样 raw_message 兜底时不会把整段媒体提示变成空白。
    """
    placeholders = {
        "image": "[图片]", "mface": "[表情]", "face": "[表情]",
        "bface": "[表情]", "sface": "[表情]", "record": "[语音]",
        "video": "[视频]", "file": "[文件]", "forward": "[内嵌聊天记录]",
        "node": "[内嵌聊天记录]", "json": "[卡片]", "reply": "",
    }

    def _replace(match):
        code = match.group(1)
        name = code.split(",", 1)[0].strip().lower()
        if name == "at":
            qq = re.search(r"(?:^|,)qq=([^,]+)", code)
            return f"@{qq.group(1)}" if qq else "@某人"
        return placeholders.get(name, "")

    text = re.sub(r"\[CQ:([^\]]+)\]", _replace, str(text or ""), flags=re.IGNORECASE)
    return clip_text(_unescape_cq_text(text), _NODE_TEXT_MAX_CHARS * 10)


# ----------------------------------------------------------------------
# 分类与元信息提取
# ----------------------------------------------------------------------
def classify_json_payload(payload: dict) -> str:
    """判断一张卡片是聊天记录、小程序还是普通卡片。"""
    if not isinstance(payload, dict):
        return "other"
    app = str(payload.get("app") or "").strip().lower()
    prompt = str(payload.get("prompt") or "")
    desc = str(payload.get("desc") or "")
    blob = f"{prompt} {desc}"

    if app.startswith(MULTIMSG_APP_PREFIXES):
        return "chat_record"
    if app.startswith(MINI_APP_PREFIX):
        return "miniapp"

    # app 字段缺失/改名时，用文案和 resid 兜底
    if "聊天记录" in blob or "合并转发" in blob or "转发消息" in blob:
        return "chat_record"
    if "小程序" in blob:
        return "miniapp"

    meta = payload.get("meta")
    if _find_meta_value(meta, ("resid", "m_resid")):
        return "chat_record"
    if app == "com.tencent.template" and _find_meta_value(meta, ("qqdocurl", "preview")):
        return "miniapp"
    return "other"


def _find_meta_value(meta: Any, keys: Tuple[str, ...]) -> str:
    for item in _iter_nested_dicts(meta):
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def extract_forward_resid(payload: dict, seg_data: Optional[dict] = None) -> str:
    """提取 get_forward_msg 需要的 resid/id。

    不同 NapCat 版本把它放在消息段 data、卡片顶层或 meta.detail 里；
    这里按可信度依次尝试：resid 系字段优先，其次才是 forward 段的 id，
    最后才把 file/filename 当兜底，避免误把文件名当成资源 ID。
    """
    candidates: List[str] = []

    def add(value: Any):
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    if isinstance(seg_data, dict):
        for key in ("resid", "m_resid"):
            add(seg_data.get(key))

    if isinstance(payload, dict):
        for key in ("resid", "m_resid"):
            add(payload.get(key))
        for item in _iter_nested_dicts(payload.get("meta")):
            for key in ("resid", "m_resid"):
                add(item.get(key))

    if isinstance(seg_data, dict):
        add(seg_data.get("id"))
    if isinstance(payload, dict):
        add(payload.get("id"))

    if isinstance(seg_data, dict):
        for key in ("file", "filename"):
            add(seg_data.get(key))

    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def chat_record_hint(payload: dict) -> str:
    """聊天记录卡片上“查看 N 条转发消息”之类的提示，取不到时返回空串。"""
    if not isinstance(payload, dict):
        return ""
    meta = payload.get("meta")
    detail = {}
    if isinstance(meta, dict):
        value = meta.get("detail")
        if isinstance(value, dict):
            detail = value

    candidate = _pick_str((detail,), ("summary", "desc", "title", "wording"))
    if not candidate:
        news = detail.get("news") if isinstance(detail, dict) else None
        if isinstance(news, list):
            texts = []
            for item in news:
                if isinstance(item, dict):
                    text = str(item.get("text") or "").strip()
                    if text:
                        texts.append(text)
            if texts:
                candidate = "；".join(texts[:4])
    if not candidate:
        candidate = _strip_card_prefix(payload.get("prompt"))
    if not candidate:
        candidate = str(payload.get("desc") or "").strip()

    candidate = _strip_card_prefix(candidate)
    if not candidate or candidate.lower() in _GENERIC_DESCS:
        return "一段聊天记录"
    return clip_text(candidate, 60)


def extract_mini_program(payload: dict) -> MiniProgramInfo:
    """从小程序卡片里取标题、简介、来源；不做任何网络访问。"""
    info = MiniProgramInfo(app=str(payload.get("app") or "").strip() if isinstance(payload, dict) else "")

    meta = payload.get("meta") if isinstance(payload, dict) else None
    details: List[dict] = []
    if isinstance(meta, dict):
        # detail_1 / detail 优先，其余按出现顺序补位
        for key in ("detail_1", "detail", "detail_2", "detail_3", "mini_app"):
            value = meta.get(key)
            if isinstance(value, dict):
                details.append(value)
        for value in meta.values():
            if isinstance(value, dict) and value not in details:
                details.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item not in details:
                        details.append(item)
    elif isinstance(meta, list):
        details.extend(item for item in meta if isinstance(item, dict))

    info.title = _pick_str(details, ("title", "name", "nickname"))
    info.description = _pick_str(
        details, ("desc", "description", "summary", "abstract", "content")
    )
    info.url = _pick_str(details, ("qqdocurl", "url", "detail_url", "jump_url"))

    prompt = _strip_card_prefix(payload.get("prompt") if isinstance(payload, dict) else "")
    if not info.title and prompt:
        info.title = prompt
    if not info.description and prompt and prompt != info.title:
        # prompt 常是“标题 - 简介”，标题已用时把剩余部分当简介
        extra = prompt.replace(info.title, "", 1).strip(" -—|·")
        if extra and extra != info.title:
            info.description = extra

    source = str(payload.get("desc") or "").strip() if isinstance(payload, dict) else ""
    if source and "小程序" in source and len(source) <= 16:
        info.source = source
    tag = _pick_str(details, ("tag", "source", "host_name", "app_name"))
    if tag and "小程序" not in tag and len(tag) <= 12 and tag not in info.title:
        info.source = f"{tag}小程序"

    info.title = clip_text(info.title, 80)
    info.description = clip_text(info.description, 160)
    return info


def format_mini_program(info: MiniProgramInfo, max_chars: int = 200) -> str:
    """渲染成一句自然语言；标题/简介都没有时返回空串。"""
    if not info.title and not info.description:
        return ""
    title = info.title or "一个未命名小程序"
    label = info.source or "QQ小程序"
    text = f"一个{label}“{title}”"
    desc = info.description.strip()
    if desc and desc != info.title:
        text += f"，简介：“{desc}”"
    return clip_text(text, max_chars)


# ----------------------------------------------------------------------
# 消息段遍历
# ----------------------------------------------------------------------
def iter_rich_segments(message_segments: Any) -> Iterator[Tuple[str, dict, dict]]:
    """遍历消息段，产出 (kind, payload, seg_data)。

    kind 只会是 "miniapp" 或 "chat_record"；普通文本/图片/卡片不产出。
    """
    if not isinstance(message_segments, list):
        return
    for seg in message_segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "").strip().lower()
        seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}

        if seg_type == "forward":
            # OneBot 11 的合并转发段：{"type":"forward","data":{"id":"..."}}
            payload = decode_json_payload(seg_data.get("data")) or {}
            yield "chat_record", payload, seg_data
            continue

        if seg_type not in ("json", "miniapp", "card"):
            continue

        raw = seg_data.get("data")
        if raw is None:
            raw = seg_data.get("json")
        payload = decode_json_payload(raw)
        if payload is None:
            # 少数实现把 app/title 直接展开在 seg_data 上
            payload = decode_json_payload(seg_data)
        if not payload:
            continue

        kind = classify_json_payload(payload)
        if seg_type == "miniapp" and kind == "other":
            kind = "miniapp"
        if kind in ("miniapp", "chat_record"):
            yield kind, payload, seg_data


def has_rich_segments(message_segments: Any) -> bool:
    """这条消息里是否有小程序或聊天记录。"""
    return any(True for _ in iter_rich_segments(message_segments))


def preview_rich_segments(message_segments: Any, max_items: int = 3, max_chars: int = 120) -> str:
    """不取正文、不调 LLM 的轻量预览，供未读缓冲与事件面板显示。"""
    parts: List[str] = []
    for kind, payload, _seg_data in iter_rich_segments(message_segments):
        if kind == "miniapp":
            info = extract_mini_program(payload)
            label = f"[小程序] {info.title}" if info.title else "[小程序]"
        else:
            hint = chat_record_hint(payload)
            label = "[聊天记录]" if not hint or hint == "一段聊天记录" else f"[聊天记录] {hint}"
        if label not in parts:
            parts.append(label)
        if len(parts) >= max_items:
            break
    return clip_text("；".join(parts), max_chars)


# ----------------------------------------------------------------------
# 合并转发展开（只向下走一层）
# ----------------------------------------------------------------------
def _looks_like_node_list(items: Any) -> bool:
    if not isinstance(items, list) or not items:
        return False
    if all(isinstance(item, str) for item in items[:3]):
        # 少数版本直接返回字符串消息（可能是原始文本或序列化节点）
        return True
    for item in items[:3]:
        if not isinstance(item, dict):
            return False
        if item.get("type") == "node":
            return True
        if "sender" in item or "nickname" in item or "user_id" in item:
            return True
        if ("content" in item or "message" in item) and "type" not in item:
            return True
        data = item.get("data")
        if isinstance(data, dict) and ("message" in data or "content" in data):
            return True
    return False


def _coerce_node_items(items: Any) -> List[dict]:
    """把字符串节点/序列化节点统一成 dict 列表。"""
    nodes: List[dict] = []
    for item in items:
        if isinstance(item, dict):
            nodes.append(item)
            continue
        if isinstance(item, str):
            parsed = decode_json_payload(item)
            if isinstance(parsed, dict) and (
                "sender" in parsed or "message" in parsed
                or "content" in parsed or "nickname" in parsed
            ):
                nodes.append(parsed)
            else:
                nodes.append({"raw_message": item})
    return nodes


def _looks_like_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("type") == "node":
        return True
    if "sender" in node:
        return True
    return "nickname" in node and ("content" in node or "message" in node)


def _extract_forward_nodes(data: Any, depth: int = 0) -> List[dict]:
    """兼容 NapCat 各版本的 get_forward_msg 返回结构。"""
    if depth > 2:
        return []
    if isinstance(data, list):
        return _coerce_node_items(data) if _looks_like_node_list(data) else []
    if not isinstance(data, dict):
        return []

    for key in ("messages", "message", "msg", "content"):
        value = data.get(key)
        if isinstance(value, list) and _looks_like_node_list(value):
            return _coerce_node_items(value)
        if isinstance(value, dict):
            nodes = _extract_forward_nodes(value, depth + 1)
            if nodes:
                return nodes

    inner = data.get("data")
    if isinstance(inner, (dict, list)):
        nodes = _extract_forward_nodes(inner, depth + 1)
        if nodes:
            return nodes

    if _looks_like_node(data):
        return [data]
    return []


def _node_sender_and_content(node: dict) -> Tuple[str, Any]:
    """从节点里取发送者名字与消息内容。"""
    if not isinstance(node, dict):
        return "", ""

    if node.get("type") == "node":
        info = node.get("data") if isinstance(node.get("data"), dict) else node
        name = (
            info.get("nickname")
            or info.get("card")
            or info.get("name")
            or info.get("user_id")
            or ""
        )
        content = (
            info.get("message")
            or info.get("content")
            or info.get("raw_message")
            or ""
        )
        return str(name), content

    sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
    name = (
        sender.get("card")
        or sender.get("nickname")
        or node.get("nickname")
        or sender.get("user_id")
        or node.get("user_id")
        or ""
    )
    content = node.get("message") or node.get("content") or node.get("raw_message") or ""
    return str(name), content


def _render_segment(seg: dict) -> str:
    """把一个消息段渲染成短文本；不认识的类型只留类型占位符。"""
    if not isinstance(seg, dict):
        return ""
    seg_type = str(seg.get("type") or "").strip().lower()
    data = seg.get("data") if isinstance(seg.get("data"), dict) else {}

    if seg_type == "text":
        return _clean_cq(str(data.get("text") or ""))
    if seg_type == "image":
        return "[图片]"
    if seg_type in ("mface", "face", "bface", "sface"):
        return "[表情]"
    if seg_type == "record":
        return "[语音]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "file":
        return "[文件]"
    if seg_type == "reply":
        return ""
    if seg_type in ("forward", "node"):
        # 只展开一层：内嵌聊天记录不再进入
        return "[内嵌聊天记录]"
    if seg_type == "at":
        name = str(data.get("name") or data.get("qq") or "").strip()
        if name == "all":
            return "@全体成员"
        return f"@{name}" if name else "@某人"
    if seg_type == "poke":
        return "[戳一戳]"
    if seg_type == "shake":
        return "[窗口抖动]"
    if seg_type in ("json", "miniapp", "card"):
        raw = data.get("data")
        if raw is None:
            raw = data.get("json")
        payload = decode_json_payload(raw) or decode_json_payload(data)
        if payload:
            kind = classify_json_payload(payload)
            if kind == "chat_record":
                return "[内嵌聊天记录]"
            if kind == "miniapp" or seg_type == "miniapp":
                info = extract_mini_program(payload)
                return f"[小程序] {info.title}" if info.title else "[小程序]"
        return "[卡片]"
    return f"[{seg_type}]" if seg_type else ""


def _render_node_content(content: Any, depth: int = 0) -> str:
    """把节点的 message/content 渲染成正文；depth 只用于防御异常嵌套。"""
    if depth > 2:
        return "[内嵌内容]"
    if content is None:
        return ""
    if isinstance(content, str):
        text = content.strip()
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return _render_node_content(parsed, depth + 1)
        # raw_message 里的嵌套转发：保留占位符，不请求内容
        if re.search(r"\[CQ:forward,", text, re.IGNORECASE):
            return "[内嵌聊天记录]"
        if re.search(r"\[CQ:json,", text, re.IGNORECASE) and (
            "multimsg" in text or "聊天记录" in text
        ):
            return "[内嵌聊天记录]"
        return _clean_cq(text)
    if isinstance(content, dict):
        if content.get("type"):
            return _render_segment(content)
        for key in ("message", "content", "raw_message", "data"):
            value = content.get(key)
            if value:
                return _render_node_content(value, depth + 1)
        return ""
    if isinstance(content, (list, tuple)):
        parts = []
        for seg in content:
            if isinstance(seg, dict):
                parts.append(_render_segment(seg))
            elif isinstance(seg, str):
                parts.append(_clean_cq(seg))
        return "".join(parts).strip()
    return str(content)


def flatten_forward_messages(
    forward_data: Any,
    max_messages: int = 80,
    max_chars: int = 4000,
) -> ForwardDigest:
    """把 get_forward_msg 的返回展开成可读行，最多 max_messages/max_chars。

    这里只处理当前这一层节点的内容；节点里若还有聊天记录，只渲染成
    “[内嵌聊天记录]”，不会再调用 get_forward_msg。
    """
    nodes = _extract_forward_nodes(forward_data)
    digest = ForwardDigest(total=len(nodes))
    if not nodes:
        return digest

    max_messages = max(1, int(max_messages))
    max_chars = max(100, int(max_chars))
    used_chars = 0

    for node in nodes:
        if not isinstance(node, dict):
            continue
        name, content = _node_sender_and_content(node)
        text = _render_node_content(content)
        if not text:
            continue
        line = f"{name or '某人'}：{clip_text(text, _NODE_TEXT_MAX_CHARS)}"

        if digest.shown >= max_messages or (
            digest.lines and used_chars + len(line) > max_chars
        ):
            digest.truncated = True
            break
        digest.lines.append(line)
        digest.shown += 1
        used_chars += len(line)

    if digest.shown < len(nodes):
        digest.truncated = True
    return digest


# ----------------------------------------------------------------------
# 摘要提示词与兜底
# ----------------------------------------------------------------------
def build_chat_record_summary_messages(
    sender_name: str,
    digest: ForwardDigest,
    max_chars: int = 160,
) -> List[dict]:
    """构造“概括聊天记录”的 LLM 消息。"""
    max_chars = max(40, int(max_chars))
    system_prompt = (
        "任务：把一段转发的聊天记录概括成 1~3 句中文摘要。\n"
        "规则：\n"
        "1. 只陈述记录里明确出现的事实：谁说了什么、关键结论或安排；\n"
        f"2. 最多 {max_chars} 字，不要逐条复述，不要评论，不要编造；\n"
        "3. 不要输出“摘要：”“聊天记录：”之类的前缀，只输出摘要正文。\n"
    )
    lines = "\n".join(digest.lines) if digest.lines else "（没有可读内容）"
    if digest.truncated:
        remaining = max(0, digest.total - digest.shown)
        lines += f"\n（后面还有 {remaining} 条未展开）"
    user_content = (
        f"发送者：{sender_name or '某人'}\n"
        f"共 {digest.total or digest.shown} 条消息：\n{lines}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def normalize_chat_record_summary(text: Any, max_chars: int = 160) -> str:
    """清理模型输出：去代码块/前缀/引号，压成单行并截断。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").removeprefix("json").removeprefix("JSON")
        cleaned = cleaned.removesuffix("```").strip()
    cleaned = re.sub(r"^(摘要|概括|总结)\s*[:：]\s*", "", cleaned)
    cleaned = cleaned.strip("“”\"'` ")
    return clip_text(cleaned, max(40, int(max_chars)))


def fallback_chat_record_summary(digest: ForwardDigest, max_chars: int = 160) -> str:
    """模型不可用时的兜底：取前两条，带上总条数。"""
    if not digest.lines:
        return ""
    head = "；".join(digest.lines[:2])
    if len(digest.lines) > 2 or digest.truncated:
        head += f"（共 {digest.total or len(digest.lines)} 条）"
    return clip_text(head, max(40, int(max_chars)))


def format_rich_parts(extras: RichExtras, mini_max_chars: int = 200) -> List[str]:
    """把解析结果渲染成可拼进正文的名词短语列表。"""
    parts: List[str] = []
    for info in extras.mini_programs:
        text = format_mini_program(info, max_chars=mini_max_chars)
        if text and text not in parts:
            parts.append(text)
    if extras.chat_summary:
        parts.append(f"一段聊天记录，大意是：“{extras.chat_summary}”")
    elif extras.has_chat_record:
        parts.append("一段聊天记录（内容没能展开）")
    return parts
