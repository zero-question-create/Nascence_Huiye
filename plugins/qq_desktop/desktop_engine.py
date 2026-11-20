# plugins/qq_desktop/desktop_engine.py
# ========================================================================
# 直接读 QQ 窗口（QQNT）的未读与会话内容，不登录协议、不使用 NapCat。
#
# - 未读：QQ 的无障碍树里，会话项下有 Group "N条未读"，直接解析；
# - 会话列表：Pane "会话列表" 下的 Group 项（名字/时间/预览/未读）；
# - 打开会话：按名字找到会话项并 UIA 激活；
# - 读消息：优先 UIA 文本，读不到就用截图 + 视觉模型兜底。
# ========================================================================

from __future__ import annotations

import os
import re
import time
from typing import List, Optional

from plugins.desktop import uia

_UNREAD_RE = re.compile(r"(\d+)\s*条未读")
_UNREAD_BADGE_RE = re.compile(r"^\d{1,4}\+?$")
# QQ 切到资料卡/空间等页面时，会话列表不在树里，但左侧导航仍带着
# “消息 90条未读”这个全局角标。没有它就会误报“没有未读消息”。
_GLOBAL_UNREAD_RE = re.compile(r'Button\s+"消息\s*(\d+)\+?\s*条未读"')
_ITEM_RE = re.compile(r"^(\S+)\s+Group\s+\"\"\s+actions=invoke")
_TEXT_RE = re.compile(r'^(\S+)\s+Text\s+"([^"]*)"')
_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}|昨天|前天|星期[一二三四五六日天]|\d{1,2}/\d{1,2}|[A-Za-z]{3}\s+\d{1,2})$")


def format_unread(count: int) -> str:
    """未读显示上限：不把 53754 这种原始计数直接灌给语言层。"""
    try:
        value = max(0, int(count))
    except (TypeError, ValueError):
        value = 0
    if value >= 1000:
        return "999+"
    if value >= 100:
        return "99+"
    return str(value)


_cached_main_hwnd = 0


def _looks_like_main(hwnd: int) -> bool:
    try:
        return 'Pane "会话列表"' in snapshot_text(hwnd)
    except Exception:
        return False


def find_window() -> int:
    """找到 QQNT 主窗口；没有返回 0。

    QQ 最小化到托盘后主窗口是隐藏的（IsWindowVisible=False），但 UIA 仍然
    能读；这里把隐藏窗口也枚举进来，避免一收进托盘就完全看不到新消息。
    """
    global _cached_main_hwnd
    if _cached_main_hwnd and uia.window_exists(_cached_main_hwnd):
        try:
            if _looks_like_main(_cached_main_hwnd):
                return int(_cached_main_hwnd)
        except Exception:
            pass

    windows = uia.list_windows(only_titled=True, include_hidden=True)
    candidates = []
    for item in windows:
        title = str(item.get("title") or "")
        if title == "QQ" or title.startswith("QQ "):
            candidates.append(item)
    if not candidates:
        for item in windows:
            image = uia.process_image(int(item["pid"]))
            if image and os.path.basename(image).lower() in ("qq.exe", "qqnt.exe"):
                candidates.append(item)
    if not candidates:
        _cached_main_hwnd = 0
        return 0

    # 主窗口有“会话列表”；聊天小窗/资料卡没有。优先选它，不能只看标题。
    for item in candidates:
        hwnd = int(item["hwnd"])
        if _looks_like_main(hwnd):
            _cached_main_hwnd = hwnd
            return hwnd
    # 加载中/暂时读不到时，先返回可见窗口，没有可见再用第一个候选。
    for item in candidates:
        if item.get("visible"):
            _cached_main_hwnd = int(item["hwnd"])
            return int(item["hwnd"])
    _cached_main_hwnd = int(candidates[0]["hwnd"])
    return int(candidates[0]["hwnd"])


def snapshot_text(hwnd: int) -> str:
    result = uia.backend().snapshot(hwnd, max_nodes=1200, depth=28, chrome=True)
    return str(result.get("text") or "")


def parse_conversations(text: str) -> List[dict]:
    """从树文本里解析会话列表：名字、时间、预览、未读数、引用。"""
    lines = text.splitlines()
    start = None
    base_indent = 0
    for index, line in enumerate(lines):
        if 'Pane "会话列表"' in line:
            start = index
            base_indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return []

    items: List[dict] = []
    current = None
    item_indent = base_indent + 2
    for raw in lines[start + 1:]:
        indent = len(raw) - len(raw.lstrip())
        if indent <= base_indent:
            break
        stripped = raw.strip()
        match = _ITEM_RE.match(stripped)
        if match and indent == item_indent:
            current = {
                "ref": match.group(1),
                "name": "",
                "time": "",
                "preview": [],
                "unread": 0,
            }
            items.append(current)
            continue
        if current is None:
            continue
        unread = _UNREAD_RE.search(stripped)
        if unread:
            current["unread"] = int(unread.group(1))
        text_match = _TEXT_RE.match(stripped)
        if text_match:
            value = text_match.group(2).strip()
            if not value:
                continue
            if not current["name"]:
                current["name"] = value
            elif not current["time"] and _TIME_RE.match(value):
                current["time"] = value
            elif _UNREAD_BADGE_RE.match(value):
                # 会话项里的纯数字/“99+”通常是未读角标，不是消息预览。
                # 之前把“3”“99+”拼进预览，会变成“快加入这个群 3”。
                continue
            elif value in ("以下为新消息", "新消息", "查看更多消息"):
                continue
            else:
                current["preview"].append(value)

    result = []
    seen = {}
    for item in items:
        name = item["name"].strip()
        if not name:
            continue
        key = name
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            key = f"{name} #{seen[name]}"
        result.append({
            "id": key,
            "label": name,
            "name": name,
            "time": item["time"],
            "preview": " ".join(item["preview"])[:120],
            "unread": int(item["unread"] or 0),
            "ref": item["ref"],
        })
    return result


def total_unread(conversations: List[dict]) -> int:
    return sum(int(item.get("unread") or 0) for item in conversations)


def parse_global_unread(text: str) -> int:
    """读取左侧“消息”导航上的全局未读角标。

    会话列表只有在消息页才存在；QQ 停在资料卡、空间或频道页时，
    parse_conversations() 会返回空列表，但全局角标仍然可读。
    """
    best = 0
    for line in str(text or "").splitlines():
        match = _GLOBAL_UNREAD_RE.search(line)
        if match:
            best = max(best, int(match.group(1)))
    return best


def effective_unread(text: str, conversations: List[dict] = None) -> int:
    """会话项未读之和与全局角标取较大值，避免页面切换时误报 0。"""
    if conversations is None:
        conversations = parse_conversations(text)
    return max(total_unread(conversations), parse_global_unread(text))


def extract_message_lines(text: str) -> List[str]:
    """从树文本里取消息区的 Text 行（不调用视觉）。

    这个函数只做纯文本兜底；判断消息是谁说的必须用 extract_messages()，
    因为树文本本身不带方向，辉夜自己发出去的话也会出现在这里。
    """
    lines = []
    in_area = False
    for line in text.splitlines():
        if "消息列表" in line or "聊天记录" in line:
            in_area = True
        if not in_area:
            continue
        match = _TEXT_RE.match(line.strip())
        if match and match.group(2).strip():
            value = match.group(2).strip()
            if len(value) > 1:
                lines.append(value)
    return lines[:60]


# ------------------------------------------------------------------------
# 结构化消息读取
#
# QQNT 的消息列表在 UIA 里是一个 Pane "消息列表"，每个消息行是一个 Group：
#   - 自己发的消息：行里有 Group "<自己的昵称>"，气泡靠右；
#   - 对方发的消息：一对一里没有昵称 Group，气泡靠左；
#   - 群聊里对方消息可能带 Group "<群成员昵称>"。
#
# 只按文本行做“前缀 diff”会把滚动后重新出现的旧消息、以及自己刚发出去
# 的消息再次当成“对方的新消息”，所以这里直接按消息行解析方向。
# ------------------------------------------------------------------------

_SYSTEM_GROUPS = {"消息", "图片", "文件", "表情", "语音", "视频", "卡片"}


def _children(control) -> list:
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def _name(control) -> str:
    try:
        return str(control.Name or "").strip()
    except Exception:
        return ""


def _role(control) -> str:
    try:
        return uia._role(control)
    except Exception:
        return ""


def _rect(control) -> tuple:
    try:
        rect = control.BoundingRectangle
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return 0, 0, 0, 0


def parse_self_name(text: str) -> str:
    """从 QQ 窗口树里取自己的昵称（左上角“XX的头像”按钮）。"""
    match = re.search(r'Button "([^"]{1,40})的头像"', str(text or ""))
    return match.group(1).strip() if match else ""


_HEADER_BUTTON_SKIP = {
    "在线状态 在线", "在线状态", "语音通话", "视频通话", "屏幕共享", "远程协助",
    "发起群聊", "更多", "关闭", "最小化", "最大化", "恢复", "查看群主页",
    "二维码", "分享", "申请加群", "查看群介绍", "搜索", "迷你窗 Ctrl + Alt + E",
    "切换为经典模式", "消息", "联系人", "空间", "频道", "游戏", "小程序",
    "腾讯文档", "QQ游戏", "邮箱", "我的手机", "截图", "文件", "图片",
    "窗口抖动", "红包", "语音消息", "语音输入文字", "发送", "聊天记录",
}


def _button_names(lines, start: int, stop: int):
    for index in range(start, stop, -1):
        match = re.search(r'\bButton "([^"]{1,60})"', lines[index])
        if match:
            yield index, match.group(1).strip()


def parse_current_conversation(text: str, self_name: str = "") -> str:
    """从聊天区上方的标题按钮取当前对话名；聊天区没开时返回空。"""
    lines = str(text or "").splitlines()
    message_index = None
    for index, line in enumerate(lines):
        if 'Pane "消息列表"' in line:
            message_index = index
            break
    if message_index is None:
        return ""

    for index in range(message_index - 1, -1, -1):
        if 'Button "在线状态 在线"' not in lines[index]:
            continue
        for _idx, name in _button_names(lines, index - 1, -1):
            if not name or name == self_name:
                continue
            if name in _HEADER_BUTTON_SKIP or "头像" in name or "在线状态" in name:
                continue
            if any(key in name for key in ("个性签名", "天气", "迷你窗", "经典模式")):
                continue
            if "条未读" in name or "object Object" in name or name.startswith("顶部"):
                continue
            return name
        break

    # 群聊没有“在线状态 在线”：群名 Button 后面通常紧跟 Text "(人数)"。
    for index, line in enumerate(lines[:message_index]):
        match = re.search(r'\bButton "([^"]{1,60})"', line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name or name == self_name:
            continue
        if name in _HEADER_BUTTON_SKIP or "头像" in name or "在线状态" in name:
            continue
        if any(key in name for key in (
            "个性签名", "天气", "迷你窗", "经典模式", "最小化", "最大化",
            "条未读", "object Object",
        )) or name.startswith("顶部"):
            continue
        if any(re.search(r'Text "\(\d+\)"', row) for row in lines[index + 1:index + 6]):
            return name

    for line in lines[:message_index]:
        match = re.search(r'\bButton "([^"]{1,60})"', line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name or name == self_name:
            continue
        if name in _HEADER_BUTTON_SKIP or "头像" in name or "在线状态" in name:
            continue
        if any(key in name for key in (
            "个性签名", "天气", "迷你窗", "经典模式", "最小化", "最大化",
            "条未读", "object Object",
        )) or name.startswith("顶部"):
            continue
        return name
    return ""


def conversation_is_group(text: str, self_name: str = "") -> bool:
    """群聊标题通常带成员数 Text "(N)"；一对一标题带“在线状态 在线”。"""
    lines = str(text or "").splitlines()
    message_index = None
    for index, line in enumerate(lines):
        if 'Pane "消息列表"' in line:
            message_index = index
            break
    if message_index is None:
        return False
    header = parse_current_conversation(text, self_name)
    if not header:
        return False
    for index, line in enumerate(lines[:message_index]):
        if f'Button "{header}"' not in line:
            continue
        window = lines[index + 1:index + 8]
        if any(re.search(r'Text "\(\d+\)"', row) for row in window):
            return True
        if any('Button "在线状态 在线"' in row for row in window):
            return False
        break
    return False


def _find_message_list(root):
    """在 QQ 窗口里找到 Pane "消息列表"。"""
    stack = [(root, 0)]
    visited = 0
    while stack and visited < 5000:
        control, depth = stack.pop()
        if depth > 50:
            continue
        visited += 1
        if _role(control) == "Pane" and _name(control) == "消息列表":
            return control
        for child in reversed(_children(control)):
            stack.append((child, depth + 1))
    return None


def _has_message_content(control, depth: int = 0) -> bool:
    """行里是否有文字或图片（滚动条之类的空 Group 会被排除）。"""
    if depth > 20:
        return False
    role = _role(control)
    if role == "Text" and _name(control):
        return True
    if role == "Image":
        return True
    for child in _children(control):
        if _has_message_content(child, depth + 1):
            return True
    return False


def _message_rows(pane):
    """消息列表里一行一个 Group；这里兼容外层包裹层数量不固定的情况。"""
    node = pane
    for _ in range(8):
        children = _children(node)
        groups = [child for child in children if _role(child) == "Group"]
        rich = [group for group in groups if _has_message_content(group)]
        if len(rich) >= 2:
            return rich
        if len(rich) == 1:
            child = rich[0]
            _left, top, _right, bottom = _rect(node)
            _cl, ctop, _cr, cbottom = _rect(child)
            node_height = bottom - top
            child_height = cbottom - ctop
            # 只有一个消息时，列表里只有一条内容 Group + 一个滚动条；
            # 这一条的高度明显小于列表容器，说明它已经是消息行本身。
            if node_height > 0 and 0 < child_height < node_height * 0.8:
                return [child]
            if child is not node:
                node = child
                continue
        if len(children) == 1:
            node = children[0]
            continue
        if len(groups) == 1 and len(children) <= 2:
            node = groups[0]
            continue
        break
    return [child for child in _children(node)
            if _role(child) == "Group" and _has_message_content(child)]


def _row_message(row, list_center: int, conversation: str, self_name: str):
    texts = []
    groups = []
    has_image = False

    def walk(control, depth: int):
        nonlocal has_image
        if depth > 20:
            return
        role = _role(control)
        name = _name(control)
        if role == "Text" and name:
            texts.append((name, control))
        elif role == "Group" and name:
            groups.append(name)
        elif role == "Image":
            has_image = True
        for child in _children(control):
            walk(child, depth + 1)

    walk(row, 0)
    if not texts and not has_image:
        return None

    time_text = ""
    for value, _control in texts:
        if _TIME_RE.match(value):
            time_text = value
            break

    sender = ""
    for value in groups:
        if value and value not in _SYSTEM_GROUPS:
            sender = value
            break

    # 自己发的消息在树里有自己的昵称，气泡靠右；对方消息没有昵称时靠左。
    xs = []
    for value, control in texts:
        left, _top, right, _bottom = _rect(control)
        if right > left:
            xs.append((left + right) / 2)
    content_center = sum(xs) / len(xs) if xs else 0
    box_self = bool(xs) and content_center > list_center

    if self_name and sender == self_name:
        direction = "self"
    elif self_name and sender and sender != self_name:
        direction = "other"
    else:
        direction = "self" if box_self else "other"

    content = []
    for value, _control in texts:
        if value == time_text or _TIME_RE.match(value):
            continue
        if value == sender or (self_name and value == self_name):
            continue
        if value in ("已读", "未读", "以下为新消息", "新消息", "查看更多消息"):
            continue
        if value not in content:
            content.append(value)

    text = " ".join(content).strip()
    if not text:
        if not has_image:
            return None
        text = "【图片】"

    return {
        "sender": sender or (self_name if direction == "self" else conversation) or "对方",
        "direction": direction,
        "text": text,
        "time": time_text,
        "has_image": bool(has_image),
    }


def extract_messages(hwnd: int, conversation: str = "", self_name: str = "") -> List[dict]:
    """按消息行读取完整消息列表（包含滚动到屏幕外的历史消息）。"""
    try:
        uia.backend()  # 确保当前线程初始化 UIA
        root = uia.auto.ControlFromHandle(int(hwnd))
    except Exception:
        return []
    if root is None:
        return []
    pane = _find_message_list(root)
    if pane is None:
        return []
    left, _top, right, _bottom = _rect(pane)
    list_center = (left + right) / 2 if right > left else 0
    result = []
    for row in _message_rows(pane):
        try:
            message = _row_message(row, list_center, conversation, self_name)
        except Exception:
            message = None
        if message:
            result.append(message)
    return result


def message_key(message: dict) -> str:
    return "|".join((
        str(message.get("direction") or ""),
        str(message.get("sender") or ""),
        str(message.get("text") or ""),
    ))


def new_messages(previous: List[dict], current: List[dict]) -> List[dict]:
    """找 current 里真正新增的消息。

    消息列表只会追加/裁剪，不会重排；所以拿 previous 的尾部去对齐
    current 的开头。滚动导致旧消息重新可见时，不会重复返回。
    """
    if not current:
        return []
    if not previous:
        return list(current)
    old_keys = [message_key(item) for item in previous]
    new_keys = [message_key(item) for item in current]
    max_overlap = min(len(old_keys), len(new_keys))
    for size in range(max_overlap, 0, -1):
        if old_keys[-size:] == new_keys[:size]:
            return list(current[size:])
    # 树里偶尔会插入“查看更多消息”之类的行，按最后一条已知消息再对齐一次。
    for index in range(len(new_keys) - 1, -1, -1):
        if new_keys[index] == old_keys[-1]:
            return list(current[index + 1:])
    return []


def merge_messages(existing: List[dict], current: List[dict]) -> tuple:
    """把当前可见的消息窗口合并进完整记录。

    QQ 的 UIA 消息列表是虚拟列表：往上翻只保留旧的一段，往下翻只保留
    新的一段。如果直接用当前窗口覆盖记录，就会“往上翻丢掉新消息、往下翻
    丢掉旧消息”。这里按序列重叠对齐，返回 (完整记录, 真正新增的尾部消息)。
    """
    if not current:
        return list(existing), []
    if not existing:
        return list(current), []
    old_keys = [message_key(item) for item in existing]
    new_keys = [message_key(item) for item in current]
    max_overlap = min(len(old_keys), len(new_keys))

    # 1) 向下翻/来了新消息：existing 尾部 == current 开头。
    for size in range(max_overlap, 0, -1):
        if old_keys[-size:] == new_keys[:size]:
            appended = list(current[size:])
            return list(existing) + appended, appended

    # 2) 向上翻：current 尾部 == existing 开头，把更早的一段接到前面。
    for size in range(max_overlap, 0, -1):
        if new_keys[-size:] == old_keys[:size]:
            prepended = list(current[:-size])
            return prepended + list(existing), []

    # 3) current 是 existing 中间的一段（滚动位置改变）：保留完整记录。
    if len(new_keys) <= len(old_keys):
        for start in range(0, len(old_keys) - len(new_keys) + 1):
            if old_keys[start:start + len(new_keys)] == new_keys:
                return list(existing), []

    # 4) 中间有系统消息/加载断层：按 key 去重后追加未见过的尾部。
    seen = set(old_keys)
    appended = [item for item in current if message_key(item) not in seen]
    return list(existing) + appended, appended


def format_messages(messages: List[dict], limit: int = 40) -> str:
    lines = []
    for message in messages[-limit:]:
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        who = "我" if message.get("direction") == "self" else str(
            message.get("sender") or "对方"
        ).strip()
        lines.append(f"{who}：{text}")
    return "\n".join(lines)


def has_image_messages(text: str) -> bool:
    """消息区里是否有图片控件（QQ 的图片消息通常叫 Image "图片"）。"""
    in_area = False
    for raw in text.splitlines():
        line = raw.strip()
        if "消息列表" in line or "聊天记录" in line:
            in_area = True
            continue
        if not in_area:
            continue
        if 'Button "表情"' in line or 'Button "发送"' in line or 'Pane "群成员列表"' in line:
            break
        if "Image" in line:
            return True
    return False


async def _vision_describe(hwnd: int, name: str = "") -> str:
    image = uia.capture_window_visible(hwnd)
    if image is None or uia.image_blank(image):
        image = uia.capture_window(hwnd, 2)
    if image is None or uia.image_blank(image):
        image = uia.capture_window(hwnd, 0)
    if image is None or uia.image_blank(image):
        return ""
    try:
        import tempfile
        from pathlib import Path

        from core.llm_interface import describe_image_from_path

        path = Path(tempfile.gettempdir()) / f"nhy_qq_desktop_{os.getpid()}.png"
        image.save(path)
        prompt = (
            f"这是QQ聊天窗口，当前会话是「{name}」。"
            "请用中文按顺序列出这段聊天记录里的消息（发送者：内容）。"
            "如果消息里有图片、表情包、贴纸或截图，请重点描述它们长什么样、"
            "画面里有什么文字或人物；也描述未读提示和群成员等可见信息，只写看得到的。"
        )
        try:
            return await describe_image_from_path(
                str(path), prompt=prompt, max_tokens=3000, timeout=120.0, full=True
            )
        finally:
            try:
                path.unlink()
            except OSError:
                pass
    except Exception as exc:
        try:
            from utils.monitor import append_log

            append_log(f"[QQ] 视觉读取失败：{exc}")
        except Exception:
            pass
        return ""


async def read_current_chat(hwnd: int, name: str = "", use_vision: bool = True) -> str:
    """读取当前会话消息：结构化 UIA 文本 + 有图片时补视觉描述。"""
    text = snapshot_text(hwnd)
    self_name = parse_self_name(text)
    messages = extract_messages(hwnd, conversation=name, self_name=self_name)
    lines = format_messages(messages, limit=40)
    has_image = any(m.get("text") == "【图片】" for m in messages)
    incoming_count = sum(1 for m in messages if m.get("direction") == "other")

    vision = ""
    if use_vision and (has_image or incoming_count < 3):
        vision = await _vision_describe(hwnd, name)

    parts = []
    if lines:
        parts.append(lines)
    if vision:
        parts.append("【图片/画面】\n" + vision)
    return "\n".join(parts)


def scroll_chat(hwnd: int, direction: str = "up", pages: float = 1.0) -> dict:
    """翻消息区：先点一下消息区拿焦点，再用 PageUp/PageDown。

    QQ 的消息区不响应窗口级的滚轮/PageUp，必须先点进列表；之前直接
    focus_window + 滚轮看起来返回成功，实际没有翻页。
    """
    from plugins.desktop import input as desktop_input

    rect = uia.window_rect(hwnd)
    width = max(1, rect[2] - rect[0])
    height = max(1, rect[3] - rect[1])
    x = rect[0] + int(width * 0.55)
    y = rect[1] + int(height * 0.45)
    desktop_input.focus_window(hwnd)
    # 优先把键盘焦点交给“消息列表”Pane；点坐标可能点到群名片/链接，
    # 反而打开资料卡，PageUp 就不再作用在聊天记录上。
    focused = False
    try:
        be = uia.backend()
        snap = be.snapshot(hwnd, max_nodes=1200, depth=28, chrome=True)
        for line in (snap.get("text") or "").splitlines():
            if 'Pane "消息列表"' in line:
                ref = line.strip().split(" ", 1)[0]
                focused = bool(be.focus(ref).get("ok"))
                break
    except Exception:
        focused = False
    if not focused:
        desktop_input.click(x, y)
    time.sleep(0.15)
    key = "pageup" if str(direction).lower() == "up" else "pagedown"
    try:
        count = max(1, int(round(float(pages))))
    except (TypeError, ValueError):
        count = 1
    result = {"ok": True, "x": x, "y": y, "direction": direction, "pages": count}
    for _ in range(count):
        pressed = desktop_input.press_key(key)
        if not pressed.get("ok"):
            result["ok"] = False
            result["error"] = pressed.get("error") or "翻页失败"
            break
        time.sleep(0.25)
    time.sleep(0.3)
    return result


def activate_conversation(hwnd: int, conversation_id: str) -> bool:
    """只点开会话，不读内容；发送前确保落点正确。"""
    text = snapshot_text(hwnd)
    conversations = parse_conversations(text)
    target = None
    for item in conversations:
        if item["id"] == conversation_id or item["name"] == conversation_id:
            target = item
            break
    if target is None:
        for item in conversations:
            if conversation_id and conversation_id in item["name"]:
                target = item
                break
    if target is None:
        return False
    text = snapshot_text(hwnd)
    # 会话已经打开时不要再点一次：QQ 会把已选中的会话当成“收起聊天区”，
    # 点完消息列表反而消失。
    if ('Pane "消息列表"' in text
            and f'Button "{target["name"]}"' in text):
        return True
    clicked = uia.backend().click(target["ref"])
    if not clicked.get("ok"):
        # 会话项有时只有 invoke 标记但 Invoke 失败，退回坐标点击该行。
        from plugins.desktop import input as desktop_input

        rect = uia.backend().element_rect(target["ref"])
        if not rect.get("ok"):
            return False
        desktop_input.focus_window(hwnd)
        desktop_input.click(int(rect["x"]), int(rect["y"]))
    time.sleep(0.6)
    return True


def send_text(hwnd: int, text: str) -> bool:
    """在当前 QQ 会话输入框里键入并发送。"""
    from plugins.desktop import input as desktop_input

    text = str(text or "").strip()
    if not text:
        return False
    rect = uia.window_rect(hwnd)
    width = max(1, rect[2] - rect[0])
    height = max(1, rect[3] - rect[1])
    # 输入框在消息区底部，避开左侧列表和右侧群成员栏。
    x = rect[0] + int(width * 0.46)
    y = rect[1] + int(height * 0.76)
    desktop_input.focus_window(hwnd)
    clicked = desktop_input.click(x, y)
    if not clicked.get("ok"):
        return False
    time.sleep(0.2)
    typed = desktop_input.type_text(text)
    if not typed.get("ok"):
        return False
    time.sleep(0.3)
    # 优先用发送按钮；按钮不可用时回车发送。
    snap = snapshot_text(hwnd)
    send_ref = ""
    for line in snap.splitlines():
        if 'Button "发送"' in line:
            send_ref = line.strip().split(" ", 1)[0]
            break
    sent = False
    if send_ref:
        sent = bool(uia.backend().click(send_ref).get("ok"))
    if not sent:
        desktop_input.press_key("enter")
    time.sleep(0.5)
    return True


async def open_conversation(hwnd: int, conversation_id: str, name: str = "", use_vision: bool = True) -> dict:
    """按会话名找到列表项并激活，然后读取当前聊天内容。"""
    text = snapshot_text(hwnd)
    conversations = parse_conversations(text)
    target = None
    for item in conversations:
        if item["id"] == conversation_id or item["name"] == name or item["id"] == name:
            target = item
            break
    if target is None and name:
        for item in conversations:
            if name in item["name"]:
                target = item
                break
    if target is None:
        return {"ok": False, "detail": f"在QQ会话列表里找不到 {conversation_id or name}"}
    # 当前会话已经展开时，再点一次会把聊天区收起来；直接读。
    if not ('Pane "消息列表"' in text
            and f'Button "{target["name"]}"' in text):
        clicked = uia.backend().click(target["ref"])
        if not clicked.get("ok"):
            # 会话项有时只有 invoke 标记但 Invoke 失败，退回坐标点击该行。
            from plugins.desktop import input as desktop_input

            rect = uia.backend().element_rect(target["ref"])
            if not rect.get("ok"):
                return {"ok": False, "detail": "点不开这个会话", "click": clicked}
            desktop_input.focus_window(hwnd)
            desktop_input.click(int(rect["x"]), int(rect["y"]))
    time.sleep(0.8)
    body = await read_current_chat(hwnd, target["name"], use_vision=use_vision)
    return {
        "ok": True,
        "detail": f"我点开了{target['name']}的对话",
        "name": target["name"],
        "unread": target["unread"],
        "content": body,
    }
