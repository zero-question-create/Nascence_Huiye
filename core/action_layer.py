# core/action_layer.py
# ========================================================================
# 动作抉择层：每轮认知循环结束后的"本能冲动"。
#
# 位置：core/cognition.py 的 cognitive_loop 在发言决策之后调用，作为该轮的最后一步。
# 语义：本轮必须等抉择返回才继续（阻塞该轮），但实现上走线程池执行，
#       以免堵住 asyncio 事件循环导致 NapCat 收消息与生物钟 tick 停摆。
#
# 动作一览（LLM 输出 JSON，用关键字表示）：
#   none / image / sticker / image_next_page / sticker_next_page
#   save_image / save_sticker（收藏本轮收到的媒体）
#   write_txt（只允许写 txt 笔记）
#
# 精力等内部数值不进入提示词：模型的判断依据只有记忆、思维链与候选描述。
# 不设冷却，改为去重：候选池已排除上一条发送记录（见 core/asset_library）。
# ========================================================================

import json
import os
import re

from . import asset_library as ASSETS
from .llm_interface import call_api_thinking
from config.api_config import config
from config.constants import BOT_NAME
from utils.monitor import append_log

DEFAULT_MAX_PAGES = 4    # 单个动作类型的翻页上限，超过强制结束抉择
DEFAULT_PAGE_SIZE = 8    # 每页候选数量
APP_ACTIONS = {"open_conversation", "read_earlier", "scroll_to_bottom", "close_conversation", "close_app"}
VALID_ACTIONS = {
    "none", "image", "sticker",
    "image_next_page", "sticker_next_page",
    "save_image", "save_sticker", "write_txt", "edit_txt", "delete_txt", "read_txt",
} | APP_ACTIONS

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _max_pages() -> int:
    try:
        return max(1, int(config.get("action_max_pages", DEFAULT_MAX_PAGES)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PAGES


def _page_size() -> int:
    try:
        return max(1, int(config.get("action_page_size", DEFAULT_PAGE_SIZE)))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE


def action_enabled() -> bool:
    return bool(config.get("action_enabled", True))


def note_enabled() -> bool:
    return bool(config.get("action_note_enabled", True))


def _parse_action(raw: str) -> dict:
    """宽松解析模型输出：允许 ```json 包裹与前后缀说明文字。"""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```").removeprefix("json").removesuffix("```").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # 兜底：仅在整段输出就是一个裸动作词时才认，避免误抓正文里的词
    token = text.strip().strip('"\'').lower()
    if token in VALID_ACTIONS:
        return {"action": token}
    return {}


def _page_text(kind: str, entries: list, page: int) -> str:
    """渲染某一页候选，编号供模型指代。描述截断到 40 字，控制提示词长度。"""
    page_size = _page_size()
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    start = page * page_size
    chunk = entries[start:start + page_size]
    label = "图片" if kind == ASSETS.IMAGE else "表情包"
    lines = [f"【我的收藏·{label} 第{page + 1}/{total_pages}页，共{len(entries)}个】"]
    for e in chunk:
        desc = str(e.get("desc") or "")
        if len(desc) > 40:
            desc = desc[:40] + "…"
        lines.append(f"- {e['id']}｜{desc}")
    if not chunk:
        lines.append("（本页没有内容）")
    return "\n".join(lines)


def _pending_text() -> str:
    """本轮收到的媒体，供"收藏"动作指代。"""
    pending = ASSETS.list_pending()
    if not pending:
        return ""
    lines = ["【本轮收到的媒体】"]
    for item in pending:
        label = "图片" if item["kind"] == ASSETS.IMAGE else "表情包"
        lines.append(f"- {item['ref']}｜{label}，内容是“{item['desc']}”")
    return "\n".join(lines)


def _system_prompt(has_apps: bool = False) -> str:
    parts = [
        f"你是{BOT_NAME}的冲动决策层，只决定此刻要不要做一个小动作。\n\n",
        "可用动作（每次只输出一个 JSON）：\n"
        "- {\"action\":\"none\"}\n"
        "- {\"action\":\"image\",\"id\":\"编号\"} / {\"action\":\"sticker\",\"id\":\"编号\"}\n"
        "- {\"action\":\"image_next_page\"} / {\"action\":\"sticker_next_page\"}\n"
        "- {\"action\":\"save_image\",\"ref\":\"m1\"} / {\"action\":\"save_sticker\",\"ref\":\"m1\"}\n"
        "- {\"action\":\"read_txt\",\"name\":\"文件名\"}\n"
        "- {\"action\":\"write_txt\",\"name\":\"文件名\",\"text\":\"一条内容\"}\n"
        "- {\"action\":\"edit_txt\",\"name\":\"文件名\",\"old_text\":\"原来那条\",\"new_text\":\"改成什么\"}\n"
        "- {\"action\":\"delete_txt\",\"name\":\"文件名\",\"old_text\":\"要划掉的那条\"}\n",
    ]
    if has_apps:
        parts.append(
            "\n软件内动作（软件已经打开时）：\n"
            "- {\"action\":\"open_conversation\",\"id\":\"对话id\"}\n"
            "- {\"action\":\"read_earlier\"}\n"
            "- {\"action\":\"scroll_to_bottom\"}\n"
            "- {\"action\":\"close_conversation\"}\n"
            "- {\"action\":\"close_app\"}\n"
        )
    parts.append(
        "\n规则：\n"
        "1. 只输出 JSON，不加解释、不加多余字段。\n"
        "2. 编号必须来自给出的清单或本轮媒体，禁止编造。\n"
        "3. 大多数时候选 none；只有当前念头明确需要时才动手，不要为了发而发。\n"
        "4. 一次只做一个动作，翻页时不要同时发送。\n"
        "5. 笔记一次只记一件事；修改/删除前先 read_txt，old_text 用看到的那条原句。\n"
    )
    if has_apps:
        parts.append(
            "6. 只有软件已经打开时才会询问软件内动作；打开软件由本地层负责，不要输出 open_app。\n"
            "7. 翻看更早用 read_earlier；不确定是否到底部时看【我的软件】里的状态，"
            "不在底部可以用 scroll_to_bottom 回到最新消息。\n"
            "8. 看完/不想看时先 close_conversation 回软件首页；长时间不用再 close_app 退出软件。\n"
        )
    return "".join(parts)


def _notes_brief(notes: list, max_chars: int = None) -> str:
    """把记事本现状摘要给模型看：文件名 + 最近几条（带行号）。

    行号来自总条目数倒推，模型可以直接用 line 精确修改；
    old_text 仍要求唯一命中，行号只是辅助。
    """
    if max_chars is None:
        try:
            max_chars = max(80, int(config.get("note_brief_max_chars", 400)))
        except (TypeError, ValueError):
            max_chars = 400
    try:
        recent_limit = max(1, int(config.get("note_brief_recent_lines", 5)))
    except (TypeError, ValueError):
        recent_limit = 5

    lines = ["【我的记事本】（写之前先看，已经记过的事不必再记一遍）"]
    for name in notes[:5]:
        existing = ASSETS.recent_note_lines(name, limit=recent_limit)
        total = ASSETS.note_line_count(name)
        stats = ASSETS.note_stats(name)
        lines.append(f"- {name}（共 {total} 条，{stats['chars']} 字）")
        if existing:
            start = max(1, total - len(existing) + 1)
            for offset, item in enumerate(existing):
                lines.append(f"    {start + offset}| {item[:60]}")
            if total > len(existing):
                lines.append(f"    （更早的 {total - len(existing)} 条已略）")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "…（已截断）"
    return text


def _apps_text(apps: list) -> str:
    """把软件状态渲染成动作层可读的清单；未打开时不暴露对话内容与列表。"""
    if not apps:
        return ""
    lines = ["【我的软件】"]
    for app in apps[:6]:
        app_id = str(app.get("id") or "")
        label = str(app.get("label") or app_id)
        opened = bool(app.get("open"))
        unread = int(app.get("unread") or 0)
        head = f"- {label}（{app_id}）：{'已打开' if opened else '未打开'}"
        if not opened and unread:
            head += f"，{unread} 条未读"
        lines.append(head)
        if opened:
            conversations = app.get("conversations") or []
            if not conversations:
                lines.append("    - （没有可打开的对话）")
            for conv in conversations[:8]:
                conv_id = str(conv.get("id") or "")
                conv_label = str(conv.get("label") or conv_id)
                current = "（当前）" if str(app.get("current_conversation") or "") == conv_id else ""
                conv_unread = int(conv.get("unread") or 0)
                tail = f"，{conv_unread} 条未读" if conv_unread else ""
                lines.append(f"    - 对话 {conv_label}（{conv_id}）{current}{tail}")
            if str(app.get("current_conversation") or ""):
                at_bottom = app.get("at_bottom")
                if at_bottom is not None:
                    status = (
                        "当前在群聊消息底部"
                        if bool(at_bottom)
                        else "当前不在群聊消息底部（可用 scroll_to_bottom 回到底部）"
                    )
                    lines.append(f"    当前状态：{status}")
    return "\n".join(lines)


def _user_prompt(thought_text: str, should_speak: bool, said_text: str,
                 keywords: list, context_hint: str,
                 images: list = None, stickers: list = None, apps: list = None,
                 language_trace: str = "") -> str:
    parts = []
    if context_hint:
        parts.append(f"【此刻的情况】{context_hint}")
    if thought_text:
        think_line = f"【我此刻的念头】{thought_text}"
        parts.append(think_line)
    if language_trace:
        parts.append(f"【语言层的思考过程】{str(language_trace)[:1200]}")
    if should_speak and said_text:
        parts.append(f"【我刚说出口的话】{said_text}")
    elif should_speak:
        parts.append("【我刚说出口的话】说了一句，但没能发出去")
    else:
        parts.append("【我刚说出口的话】我没有说话，只是在心里想")
    if keywords:
        kws = "、".join([str(k) for k in keywords[:8] if k])
        if kws:
            parts.append(f"【此刻萦绕的词】{kws}")

    apps_text = _apps_text(apps)
    if apps_text:
        parts.append(apps_text)

    # 候选列表由调用方传入并**固定**，翻页复用同一份，避免重复或跳过。
    if images is None:
        images = ASSETS.ordered_pool(ASSETS.IMAGE, keywords)
    if stickers is None:
        stickers = ASSETS.ordered_pool(ASSETS.STICKER, keywords)

    # 收藏提示必须紧跟在“本轮收到的媒体”之后：先看到图，再看到能留下它。
    pending = ASSETS.list_pending()
    if pending:
        parts.append(_pending_text())
        parts.append("如果想留下上面的某一条，用 save_image / save_sticker 引用它的编号。")

    if images:
        parts.append(_page_text(ASSETS.IMAGE, images, 0))
    if stickers:
        parts.append(_page_text(ASSETS.STICKER, stickers, 0))
    if note_enabled():
        notes = ASSETS.list_notes()
        if notes:
            parts.append(_notes_brief(notes))
    if not images and not stickers and not pending:
        # 区分"真的没收藏"与"收藏暂时没有合适的"
        has_any = ASSETS.count(ASSETS.IMAGE) + ASSETS.count(ASSETS.STICKER)
        if has_any:
            parts.append("【我的收藏】（有几张，但此刻没有特别想发的）")
        else:
            parts.append("【我的收藏】（空，还没收下过任何图片或表情包）")

    # 最近发过什么：只提供信息，不做硬性拦截。
    sent_lines = []
    for kind in (ASSETS.IMAGE, ASSETS.STICKER):
        for desc in ASSETS.recent_sent(kind)[:3]:
            label = "图片" if kind == ASSETS.IMAGE else "表情包"
            sent_lines.append(f"{label}「{desc}」")
    if sent_lines:
        parts.append("【我最近发过的】" + "；".join(sent_lines) + "（刚发过的别再连发，除非此刻确实需要）")

    parts.append("请输出此刻的动作 JSON。")
    return "\n\n".join(parts)


def decide_action(thought_text: str, should_speak: bool, said_text: str = "",
                  keywords: list = None, context_hint: str = "", profile=None,
                  apps: list = None, language_trace: str = "") -> dict:
    """执行一次动作抉择（含翻页子对话）。阻塞式，请在线程池中调用。

    profile: 插件配置的动作 LLM（core.llm_profile.LLMProfile）；None 走全局 primary。
    apps: 当前已打开软件的状态与对话列表。
    language_trace: 插件设置要求时，语言层的思考过程（不强制传入）。
    提示词只使用“念头、刚说出口的话、记忆关键词、自己的收藏/记事本、软件状态”，
    不包含平台协议细节（群号只在对话 id 里作为界面标识出现）。
    返回 {"action": <动作>, "detail": <执行结果描述>}；
    未通过校验或执行失败时统一回落为 {"action": "none", "detail": ""}。
    """
    max_pages = _max_pages()
    page_size = _page_size()
    images = ASSETS.ordered_pool(ASSETS.IMAGE, keywords)
    stickers = ASSETS.ordered_pool(ASSETS.STICKER, keywords)
    pages = {ASSETS.IMAGE: 1, ASSETS.STICKER: 1}
    counts = {ASSETS.IMAGE: len(images), ASSETS.STICKER: len(stickers)}
    pending = ASSETS.list_pending()
    # 记事本允许从零开始写，所以"全空"不算无事可做；
    # 只有连记事本都关掉、且收藏与暂存皆空时，才没必要询问。
    if not images and not stickers and not pending and not note_enabled() and not apps:
        return {"action": "none", "detail": ""}

    messages = [
        {"role": "system", "content": _system_prompt(bool(apps))},
        {"role": "user", "content": _user_prompt(thought_text, should_speak, said_text, keywords,
                                                 context_hint, images=images, stickers=stickers,
                                                 apps=apps, language_trace=language_trace)},
    ]
    from .llm_interface import fingerprint_prompt
    fingerprint_prompt("action", messages[0]["content"], profile=profile)

    for turn in range(max_pages + 1):
        raw = call_api_thinking(messages, max_tokens=800, thinking=False, profile=profile)
        data = _parse_action(raw)
        action = str(data.get("action") or "").strip().lower()
        append_log(f"[动作抉择] 第{turn + 1}次询问返回: {raw}")

        if action in ("image_next_page", "sticker_next_page"):
            kind = ASSETS.IMAGE if action.startswith("image") else ASSETS.STICKER
            total_pages = max(1, (counts[kind] + page_size - 1) // page_size)
            next_page = pages[kind] + 1
            # 越界判断只在**确实超过**总页数时成立。
            # 此前写成 pages[kind] >= total_pages（先自增再比较），于是
            # 只有 2 页时翻到第 2 页就被判定"已到末页"，最后一页永远看不到（F18）。
            if next_page > total_pages or turn >= max_pages - 1:
                messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
                messages.append({"role": "user", "content": "已经翻到最后一页了，请现在做决定，不要再翻页。"})
                continue
            pages[kind] = next_page
            messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
            messages.append({"role": "user", "content": _page_text(
                kind, images if kind == ASSETS.IMAGE else stickers, next_page - 1) + "\n\n请继续决定动作。"})
            continue

        if action in VALID_ACTIONS:
            return _execute(action, data, images, stickers)

        # 无法识别的输出：给一次纠正机会，再失败就当作不动作
        if turn == 0 and raw:
            messages.append({"role": "assistant", "content": str(raw)[:500]})
            messages.append({"role": "user", "content": "格式不对。只输出一个动作 JSON，例如 {\"action\":\"none\"}。"})
            continue
        append_log(f"[动作抉择] 输出无法识别，按不动作处理: {raw}")
        return {"action": "none", "detail": ""}

    return {"action": "none", "detail": ""}


def _execute(action: str, data: dict, images: list, stickers: list) -> dict:
    """校验并执行动作。发送类由调用方注入的发送函数完成（见 cognitive_loop）。"""
    if action == "none":
        return {"action": "none", "detail": ""}

    if action in APP_ACTIONS:
        # 软件内动作由认知循环异步执行（对话/翻读可能涉及平台 IO）。
        app_id = str(data.get("app_id") or "")
        conversation = str(data.get("conversation") or data.get("conversation_id") or "")
        raw_id = str(data.get("id") or "")
        if action == "open_conversation":
            conversation = conversation or raw_id
        return {
            "action": action,
            "detail": "",
            "app_id": app_id,
            "conversation": conversation,
        }

    if action in ("image", "sticker"):
        kind = ASSETS.IMAGE if action == "image" else ASSETS.STICKER
        token = data.get("id") or data.get("desc") or ""
        entry = ASSETS.resolve_token(kind, token)
        if not entry:
            append_log(f"[动作抉择] 编号无法对应到收藏（{kind}: {token!r}），放弃发送")
            return {"action": "none", "detail": ""}
        # 不做硬性拦截：把"最近发过什么"告诉模型，由它自己判断是否重复。
        return {"action": action, "detail": entry["desc"], "entry": entry}

    if action in ("save_image", "save_sticker"):
        ref = str(data.get("ref") or "").strip()
        item = ASSETS.get_pending(ref)
        if not item:
            append_log(f"[动作抉择] 找不到对应的暂存媒体（ref={ref!r}），放弃收藏")
            return {"action": "none", "detail": ""}
        # 以抉择的意图为准：模型判断是表情包就存成表情包（会转 GIF）。
        if action == "save_sticker":
            kind = ASSETS.STICKER
        else:
            kind = item["kind"]
        saved = ASSETS.add_asset(kind, item["path"], item["desc"])
        if not saved:
            append_log("[动作抉择] 收藏失败")
            return {"action": "none", "detail": ""}
        word = "收进了收藏" if saved.get("added") else "早就已经在收藏里了"
        return {"action": action, "detail": item["desc"], "saved": saved, "added": saved.get("added", False),
                "reply": f"（{word}：{item['desc']}）",
                "memory_ids": item.get("memory_ids") or []}

    if action == "write_txt":
        if not note_enabled():
            append_log("[动作抉择] 写笔记已在配置中关闭，忽略")
            return {"action": "none", "detail": ""}
        name = str(data.get("name") or "").strip()
        text = str(data.get("text") or "").strip()
        result = ASSETS.write_note_ex(name, text)
        if result.get("error"):
            append_log(f"[动作抉择] 写笔记被拒绝（{result['error']}，name={name!r}）")
            return {
                "action": "none", "detail": "",
                "note_error": result["error"],
                "note_used_chars": result.get("used_chars", 0),
                "note_used_lines": result.get("used_lines", 0),
                "written": result.get("written", ""),
            }
        # 正在写这个文件时，顺手读回全文，交给下一轮以特供记忆方式回看（不入库）
        note = ASSETS.read_note(name)
        return {
            "action": action, "detail": result["path"], "path": result["path"],
            "note_name": result["name"],
            "written": result["written"],
            "truncated": bool(result.get("truncated")) or bool((note or {}).get("truncated")),
            "note_text": (note or {}).get("text", result["written"]),
        }

    if action == "edit_txt":
        if not note_enabled():
            append_log("[动作抉择] 改笔记已在配置中关闭，忽略")
            return {"action": "none", "detail": ""}
        name = str(data.get("name") or "").strip()
        old_text = str(data.get("old_text") or "").strip()
        new_text = data.get("new_text")
        line = data.get("line")
        if new_text is None:
            append_log(f"[动作抉择] edit_txt 缺少 new_text，拒绝隐式删除（name={name!r}）")
            return {"action": "none", "detail": "", "note_error": "missing_new_text"}
        result = ASSETS.edit_note_ex(name, old_text, str(new_text).strip(), line=line)
        if result.get("error"):
            append_log(f"[动作抉择] 改笔记未生效（{result['error']}，name={name!r}）")
            return {
                "action": "none", "detail": "",
                "note_error": result["error"],
                "candidates": result.get("candidates") or [],
            }
        note = ASSETS.read_note(name)
        return {
            "action": action, "detail": result["path"], "path": result["path"],
            "note_name": result["name"],
            "old_text": result["old_text"], "new_text": result["new_text"],
            "deleted": result["deleted"],
            "note_text": (note or {}).get("text", ""),
            "truncated": bool((note or {}).get("truncated")),
            "line": result.get("line"),
        }

    if action == "delete_txt":
        if not note_enabled():
            append_log("[动作抉择] 改笔记已在配置中关闭，忽略")
            return {"action": "none", "detail": ""}
        name = str(data.get("name") or "").strip()
        old_text = str(data.get("old_text") or "").strip()
        line = data.get("line")
        result = ASSETS.delete_note_line(name, old_text=old_text or None, line=line)
        if result.get("error"):
            append_log(f"[动作抉择] 删除笔记行未生效（{result['error']}，name={name!r}）")
            return {
                "action": "none", "detail": "",
                "note_error": result["error"],
                "candidates": result.get("candidates") or [],
            }
        note = ASSETS.read_note(name)
        return {
            "action": action, "detail": result["path"], "path": result["path"],
            "note_name": result["name"],
            "old_text": result["old_text"], "new_text": "",
            "deleted": True,
            "note_text": (note or {}).get("text", ""),
            "truncated": bool((note or {}).get("truncated")),
            "line": result.get("line"),
        }

    if action == "read_txt":
        if not note_enabled():
            append_log("[动作抉择] 读笔记已在配置中关闭，忽略")
            return {"action": "none", "detail": ""}
        name = str(data.get("name") or "").strip()
        note = ASSETS.read_note(name)
        if not note:
            append_log(f"[动作抉择] 读不到笔记（name={name!r}）")
            return {"action": "none", "detail": ""}
        return {
            "action": action, "detail": note["name"], "path": note["path"],
            "note_name": note["name"], "note_text": note["text"],
            "truncated": note["truncated"],
            "total_chars": note.get("total_chars"),
            "total_lines": note.get("total_lines"),
            "start_line": note.get("start_line"),
            "end_line": note.get("end_line"),
            "read_only": True,
        }

    return {"action": "none", "detail": ""}
