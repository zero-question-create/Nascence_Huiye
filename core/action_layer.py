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
DEFAULT_PAGE_SIZE = 12   # 每页候选数量
VALID_ACTIONS = {
    "none", "image", "sticker",
    "image_next_page", "sticker_next_page",
    "save_image", "save_sticker", "write_txt", "read_txt",
}

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
    """渲染某一页候选，编号供模型指代。"""
    page_size = _page_size()
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    start = page * page_size
    chunk = entries[start:start + page_size]
    label = "图片" if kind == ASSETS.IMAGE else "表情包"
    lines = [f"【我的收藏·{label} 第{page + 1}/{total_pages}页，共{len(entries)}个】"]
    for e in chunk:
        lines.append(f"- {e['id']}｜{e['desc']}")
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


def _system_prompt() -> str:
    return (
        f"你是{BOT_NAME}的冲动决策层，只决定此刻要不要做一个小动作。\n\n"
        "可用动作（每次只输出一个）：\n"
        "- {\"action\":\"none\"} 什么都不做\n"
        "- {\"action\":\"image\",\"id\":\"编号\"} 发一张自己收藏的图片\n"
        "- {\"action\":\"sticker\",\"id\":\"编号\"} 发一个自己收藏的表情包\n"
        "- {\"action\":\"image_next_page\"} 翻看图片收藏的下一页\n"
        "- {\"action\":\"sticker_next_page\"} 翻看表情包收藏的下一页\n"
        "- {\"action\":\"save_image\",\"ref\":\"m1\"} 把本轮收到的一张图收进自己的收藏\n"
        "- {\"action\":\"save_sticker\",\"ref\":\"m1\"} 把本轮收到的一个表情包收进自己的收藏\n"
        "- {\"action\":\"read_txt\",\"name\":\"文件名\"} 翻开自己以前写的记事本看看\n"
        "- {\"action\":\"write_txt\",\"name\":\"文件名\",\"text\":\"要写的内容\"} 写进自己的记事本\n\n"
        "规则：\n"
        "1. 输出严格只包含 JSON，不要任何解释、不要多余字段。\n"
        "2. 编号必须来自上面给出的收藏清单或本轮收到的媒体，禁止自己编造。\n"
        "3. 大多数时候应该是什么都不做；只有此刻确实想发、或确实觉得值得收下时才动手。\n"
        "4. 不要因为看到收藏清单就随便挑一个；发出的东西要和此刻在想的事情有关系。\n"
        "5. 一次只做一个动作。翻页时不要同时发送。\n"
        "6. 是否发出去要符合你此刻的心情和场合，别打断自己刚说的话。\n"
        "7. 记事本只能写 txt，文件名用中文或字母数字，不要带路径和扩展名。\n"
    )


def _user_prompt(thought_text: str, should_speak: bool, said_text: str,
                 keywords: list, context_hint: str) -> str:
    parts = []
    if context_hint:
        parts.append(f"【此刻的情况】{context_hint}")
    if thought_text:
        think_line = f"【我此刻的念头】{thought_text}"
        parts.append(think_line)
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

    images = ASSETS.ordered_pool(ASSETS.IMAGE, keywords)
    stickers = ASSETS.ordered_pool(ASSETS.STICKER, keywords)
    if images:
        parts.append(_page_text(ASSETS.IMAGE, images, 0))
    if stickers:
        parts.append(_page_text(ASSETS.STICKER, stickers, 0))
    pending = _pending_text()
    if pending:
        parts.append(pending)
    if note_enabled():
        notes = ASSETS.list_notes()
        if notes:
            parts.append("【我的记事本】" + "、".join(notes) + "（想看就 read_txt）")
    if not images and not stickers and not pending:
        parts.append("【我的收藏】（空，还没收下过任何图片或表情包）")
    parts.append("请输出此刻的动作 JSON。")
    return "\n\n".join(parts)


def decide_action(thought_text: str, should_speak: bool, said_text: str = "",
                  keywords: list = None, context_hint: str = "") -> dict:
    """执行一次动作抉择（含翻页子对话）。阻塞式，请在线程池中调用。

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
    if not images and not stickers and not pending and not note_enabled():
        return {"action": "none", "detail": ""}

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _user_prompt(thought_text, should_speak, said_text, keywords, context_hint)},
    ]

    for turn in range(max_pages + 1):
        raw = call_api_thinking(messages, max_tokens=4000, thinking=False)
        data = _parse_action(raw)
        action = str(data.get("action") or "").strip().lower()
        append_log(f"[动作抉择] 第{turn + 1}次询问返回: {raw}")

        if action in ("image_next_page", "sticker_next_page"):
            kind = ASSETS.IMAGE if action.startswith("image") else ASSETS.STICKER
            pages[kind] += 1
            total_pages = max(1, (counts[kind] + page_size - 1) // page_size)
            if pages[kind] >= total_pages or turn >= max_pages - 1:
                # 已到末页或翻页次数用尽：把结论直接告知，不再继续翻
                messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
                messages.append({"role": "user", "content": "已经翻到最后一页了，请现在做决定，不要再翻页。"})
                continue
            messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
            messages.append({"role": "user", "content": _page_text(kind, images if kind == ASSETS.IMAGE else stickers, pages[kind] - 1) + "\n\n请继续决定动作。"})
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

    if action in ("image", "sticker"):
        kind = ASSETS.IMAGE if action == "image" else ASSETS.STICKER
        token = data.get("id") or data.get("desc") or ""
        entry = ASSETS.resolve_token(kind, token)
        if not entry:
            append_log(f"[动作抉择] 编号无法对应到收藏（{kind}: {token!r}），放弃发送")
            return {"action": "none", "detail": ""}
        # 去重：与上一条发送内容一致则放弃
        if entry["desc"] == ASSETS.last_sent(kind):
            append_log("[动作抉择] 与上一条发送内容重复，放弃发送")
            return {"action": "none", "detail": ""}
        return {"action": action, "detail": entry["desc"], "entry": entry}

    if action in ("save_image", "save_sticker"):
        kind = ASSETS.IMAGE if action == "save_image" else ASSETS.STICKER
        ref = str(data.get("ref") or "").strip()
        item = ASSETS.get_pending(ref)
        if not item:
            append_log(f"[动作抉择] 找不到对应的暂存媒体（ref={ref!r}），放弃收藏")
            return {"action": "none", "detail": ""}
        if item["kind"] != kind:
            # 类型不符时按实际类型收藏，避免因分类偏差丢掉素材
            kind = item["kind"]
        saved = ASSETS.add_asset(kind, item["path"], item["desc"])
        if not saved:
            append_log("[动作抉择] 收藏失败")
            return {"action": "none", "detail": ""}
        word = "收进了收藏" if saved.get("added") else "早就已经在收藏里了"
        return {"action": action, "detail": item["desc"], "saved": saved, "added": saved.get("added", False),
                "reply": f"（{word}：{item['desc']}）"}

    if action == "write_txt":
        if not note_enabled():
            append_log("[动作抉择] 写笔记已在配置中关闭，忽略")
            return {"action": "none", "detail": ""}
        name = str(data.get("name") or "").strip()
        text = str(data.get("text") or "").strip()
        path = ASSETS.write_note(name, text)
        if not path:
            append_log(f"[动作抉择] 写笔记被拒绝（name={name!r}）")
            return {"action": "none", "detail": ""}
        # 正在写这个文件时，顺手读回全文，交给下一轮以特供记忆方式回看（不入库）
        note = ASSETS.read_note(name)
        return {
            "action": action, "detail": path, "path": path,
            "note_name": os.path.splitext(os.path.basename(path))[0],
            "written": text,
            "note_text": (note or {}).get("text", text),
            "truncated": bool((note or {}).get("truncated")),
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
            "truncated": note["truncated"], "read_only": True,
        }

    return {"action": "none", "detail": ""}
