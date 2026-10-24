# core/llm_interface.py
import json
import base64
import hashlib
import os
import re
import aiofiles
import asyncio
import subprocess
import tempfile
from collections import OrderedDict
#import requests    # 使用网络访问链接LLM（已弃用）
from openai import OpenAI
import datetime
from utils.monitor import append_log
from utils.metrics_probe import PROBE
from utils.dialogue_state import get_state, set_state

from config.api_config import config
from config.constants import BOT_NAME

client = OpenAI(
    api_key=config["primary_api_key"],
    base_url=config["primary_base_url"],
)

client_ = OpenAI(
    api_key=config["secondary_api_key"],
    base_url=config["secondary_base_url"],
)

MODEL = config["primary_model"]
MODEL_ = config["secondary_model"]


def reload_clients():
    """按当前全局 config 就地重建 API 客户端与模型名（配置热更新）。

    开放给面板在保存配置后调用，使新的 base_url / api_key / model 立即生效，
    无需重启进程。正在飞行中的请求不受影响，后续调用使用新客户端；
    模块级名字在函数内是调用时解析的，因此重绑定后各处引用自动生效。
    """
    global client, client_, MODEL, MODEL_
    client = OpenAI(
        api_key=config.get("primary_api_key"),
        base_url=config.get("primary_base_url"),
    )
    client_ = OpenAI(
        api_key=config.get("secondary_api_key"),
        base_url=config.get("secondary_base_url"),
    )
    MODEL = config.get("primary_model")
    MODEL_ = config.get("secondary_model")
    append_log(f"[API] 配置已热重载：主模型={MODEL}，副模型={MODEL_}")


def add_to_history(sender_name: str, user_text: str, bot_reply: str, source="QQ", quote=None, target=None):
    """写入对话历史，返回最后写入条目的序号（无写入时返回 None）。

    返回 seq 是为了让调用方能在发送失败时回填"（消息发送失败）"标记（F20）。
    target: 可选的说话/回应对象（如 "@辉夜" 时为 BOT_NAME，"@张三" 时为 "张三"）。
    quote: 可选引用信息 {"sender": 被引用者, "text": 被引用的原话}，
    只随用户消息记录，供短期上下文还原"这句话在回应什么"。
    """
    from utils.message_history import add_message
    seq = None
    if user_text:
        seq = add_message(sender_name, user_text, source, quote=quote, target=target)
    if bot_reply:
        seq = add_message(BOT_NAME, bot_reply, source)
    return seq


def load_dialogue_history():
    from utils.message_history import load_state
    load_state()

def get_history_context() -> str:
    """生成格式化的对话历史（xx说 / 我说）"""
    from utils.message_history import get_recent
    text = get_recent(10)
    if not text:
        return ""
    result = "【近期对话】\n" + text
    append_log(result)
    return result

def _record_usage(kind: str, response):
    """记录一次 LLM 调用的 token 用量（字段存在时）。"""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        PROBE.bump(f"tokens.{kind}.prompt", int(getattr(usage, "prompt_tokens", 0) or 0))
        PROBE.bump(f"tokens.{kind}.completion", int(getattr(usage, "completion_tokens", 0) or 0))
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        if hit is not None:
            PROBE.bump(f"tokens.{kind}.cache_hit", int(hit or 0))
        if miss is not None:
            PROBE.bump(f"tokens.{kind}.cache_miss", int(miss or 0))
    except Exception:
        pass


def fingerprint_prompt(kind: str, system_prompt: str) -> str:
    """记录静态 system prompt 的指纹，用于确认缓存前缀是否稳定。

    前缀缓存的前提是“每次请求的静态段逐字节相同”。这里只统计，不修改内容；
    指纹变化说明提示词被改过（应显式提升版本号，而不是悄悄漂移）。
    """
    digest = hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest()[:16]
    PROBE.gauge(f"prompt.{kind}.fingerprint", digest)
    PROBE.bump(f"prompt.{kind}.calls")
    return digest


def call_api_thinking(messages, max_tokens=8000, thinking=True, timeout=60.0):
    """
    LLM思考模式唯一外部调用接口。

    thinking=False 时关闭推理（不发送 reasoning_effort / thinking 字段），
    用于“输出一句 JSON 短句”这类不需要推理的调用，可大幅降低输出 token。
    timeout: 单次请求超时（秒）。默认 60 秒，避免网络挂起把认知循环与关停一起拖住。
    """
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": False,
        "timeout": timeout,
    }
    if thinking:
        kwargs["reasoning_effort"] = "high"
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    PROBE.bump("llm.calls")
    try:
        with PROBE.timed("llm.call_ms"):
            response = client.chat.completions.create(**kwargs)
        _record_usage("thinking", response)
        content = response.choices[0].message.content
        return content.strip() if content else None
    except Exception as e:
        PROBE.bump("llm.errors")
        print(f"[API Error] {e}")
        return None

def decompose_input(user_input: str) -> tuple:
    """
    将用户输入拆解为记忆片段，同时输出处理模式、概念与时间指代。
    返回: (memories_list, mode_str, new_state_dict, keywords, concept, time_intent)
    mode_str: "存储" | "询问" | "纠错"
    concept: 概念名，空字符串表示无明确事件（不入概念层）
    time_intent: 时间指代枚举 none/latest/recent/earlier/any，系统据此做数值换算
    """
    state = get_state()

    # 缩减为英文，减少tokens消耗，这里使用临时映射，不影响全局中文键名
    normalized_state = {
        "participants": state.get("参与者", []),
        "topic": state.get("最近话题", "")
    }
    state_str = json.dumps(normalized_state, ensure_ascii=False)
    now = datetime.datetime.now()

    system_prompt = f"""任务：把输入转换为“我”的第一人称记忆片段，并提取检索关键词。
规则：
1. 代词按当前状态换成确定名称，人称按对话关系转换（如“我”改为“你”、“你”改为“我”）。
2. 每条记忆是完整清晰的第一人称陈述，明确谁说了什么、对谁说，不删减、不凭空添加。
3. 纯指令、重复输入、无实质内容时不生成记忆。
4. 关键词：从输入提取 1-3 个原词，不做联想。
5. 概念：用 6~20 字概括“发生了什么”，包含人物或对象；寒暄无实质事件时返回空字符串。
6. 时间指代只从 none/latest/recent/earlier/any 中选一个，不计算或输出日期。
7. 只使用上述给出信息。

输出严格只包含 JSON：
{{"k":["关键词1","关键词2"], "m":"store|ask|normal", "mem":["记忆1"], "s":{{"participants":["{BOT_NAME}"],"topic":"话题"}}, "c":"概念名或空字符串", "t":"none"}}
"""

    history = get_history_context()
    user_parts = [
        f"当前状态：{state_str}",
        f"现在时间为{str(now.time())[:2]}时{str(now.time())[3:5]}分",
    ]
    if history:
        user_parts.append(history)
    user_parts.append(f"【输入】\n{user_input}")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    fingerprint_prompt("decompose", system_prompt)

    # 打印日志
    append_log("="*30+"发送信息"+"="*30)
    append_log(str(user_input))

    PROBE.bump("llm.decompose.calls")
    try:
        with PROBE.timed("llm.decompose_ms"):
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=800,
                temperature=0.1,
                stream=False,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                timeout=30.0,
            )
        _record_usage("decompose", response)
        result = response.choices[0].message.content
    except Exception as e:
        PROBE.bump("llm.decompose.errors")
        append_log(f"[API Error] {e}")
        result = None

    # 打印日志
    append_log("="*30+"LLM理解"+"="*30)
    append_log(str(result))

    if not result:  # 判空
        append_log("*"*30+"警告"+"*"*30)
        append_log("返回为空")
        return [f"对方说：{user_input}"], "普通", None, None, "", "none"

    # 尝试解析 JSON
    try:
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```").removeprefix("json").removesuffix("```").strip()
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        append_log("*"*30+"警告"+"*"*30)
        append_log("JSON解析失败，降级处理")
        append_log(f"内容：{result}")
        fallback = user_input.replace("我", "本系统由乐知网络技术部提出并完善严禁转载抄袭")
        fallback = fallback.replace("你", "我")
        fallback = fallback.replace("本系统由乐知网络技术部提出并完善严禁转载抄袭", "你")
        return [f"你告诉我，{fallback}"], "普通", None, [], "", "none"

    # 类型校验：模型可能返回 null / 字符串 / 数组等畸形结构，
    # 此前直接 data.get 后按假设使用，会 AttributeError 或在字符串上按字符迭代
    # 产生垃圾记忆（F19）。这里逐字段归一化，非法值一律退回安全默认。
    if not isinstance(data, dict):
        append_log("*"*30+"警告：LLM 返回的顶层不是对象，降级处理"+"*"*30)
        fallback = user_input.replace("我", "本系统由乐知网络技术部提出并完善严禁转载抄袭")
        fallback = fallback.replace("你", "我")
        fallback = fallback.replace("本系统由乐知网络技术部提出并完善严禁转载抄袭", "你")
        return [f"你告诉我，{fallback}"], "普通", None, [], "", "none"

    def _as_str_list(value):
        """只接受 list[str]；其余（None/str/dict/数字）一律丢弃。"""
        if not isinstance(value, list):
            return []
        return [str(v) for v in value if isinstance(v, str) and v.strip()]

    # 提取字段
    keywords = _as_str_list(data.get("k", []))
    mode_raw = data.get("m", "normal")
    memories = _as_str_list(data.get("mem", []))
    new_state = data.get("s", None)
    # 概念层：概念名与时间指代枚举（旧模型不返回时给安全默认值）
    concept = str(data.get("c") or "").strip() if isinstance(data.get("c"), str) else ""
    try:
        from .concept_store import VALID_TIME_INTENTS
    except Exception:
        VALID_TIME_INTENTS = {"none", "latest", "recent", "earlier", "any"}
    time_intent = data.get("t")
    time_intent = time_intent.strip().lower() if isinstance(time_intent, str) else "none"
    if time_intent not in VALID_TIME_INTENTS:
        time_intent = "none"

    # 反向映射：将 LLM 返回的英文字段转回中文键名，兼容原有系统
    if isinstance(new_state, dict):
        participants = new_state.get("participants", [])
        topic = new_state.get("topic", "")
        new_state = {
            "参与者": _as_str_list(participants),
            "最近话题": topic if isinstance(topic, str) else "",
        }
    else:
        # 不是 dict（None/str/list）时忽略状态更新，避免 AttributeError
        new_state = None

    # 模式映射：store -> 存储, ask -> 询问, 其他 -> 普通
    # mode_raw 可能是 list/dict 等不可哈希类型，用类型检查兜住（原实现会 TypeError）
    mode_map = {"store": "存储", "ask": "询问", "normal": "普通"}
    mode = mode_map.get(mode_raw, "普通") if isinstance(mode_raw, str) else "普通"

    # 如果 memories 为空，则使用兜底逻辑
    if not memories:
        fallback = user_input.replace("我", "本系统由乐知网络技术部提出并完善严禁转载抄袭")
        fallback = fallback.replace("你", "我")
        fallback = fallback.replace("本系统由乐知网络技术部提出并完善严禁转载抄袭", "你")
        memories = [f"你告诉我，{fallback}"]

    # 同步搜索输入内容（测试）
    # 注意：不再把整句 user_input 追加进关键词。整句会污染检索和反刍判定，
    # 也会让缓存前缀失去稳定性；全文只作为输入文本使用。
    if not keywords:
        keywords = []

    append_log("="*30+"解析结果"+"="*30)
    append_log(f"记忆：{memories}\n模式：{mode}\n状态：{new_state}\n关键词：{keywords}\n"
               f"概念：{concept or '（无）'}\n时间指代：{time_intent}")

    return memories, mode, new_state, keywords, concept, time_intent

def _coerce_bool(value) -> bool:
    """把模型给出的松散值严格转成布尔。

    不能直接用 bool()——字符串 "false" 是非空字符串，bool("false") 会得到 True，
    于是模型明确表示"不要说出口"时反而被当成要发言（F19）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0", ""):
            return False
        return False      # 无法识别时保守取 False（不发言更安全）
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _safe_json_parse(text: str) -> dict:
    """
    容错解析 LLM 返回的简单 JSON 片段。
    处理 code block 包裹、text 字段中未转义半角引号等问题。
    返回提取到的字段 dict（不保证包含所有字段）。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").removeprefix("json").removesuffix("```").strip()

    # 优先标准 JSON 解析
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    result = {}

    # say → bool
    m = re.search(r'"say"\s*:\s*(true|false)', cleaned, re.IGNORECASE)
    if m:
        result["say"] = m.group(1).lower() == "true"

    # text → string（值中可能含未转义引号，使用贪婪匹配到最后的 " 前）
    m = re.search(r'"text"\s*:\s*"(.+)"\s*\}', cleaned, re.DOTALL)
    if m:
        raw = m.group(1)
        raw = raw.replace('\\"', "\u201c").replace('"', "\u201d").replace("'", "\u2018").replace("'", "\u2019")
        result["text"] = raw

    # keywords → list
    m = re.search(r'"keywords"\s*:\s*(\[[\s\S]*?\])\s*\}', cleaned, re.DOTALL)
    if m:
        try:
            result["keywords"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            items = re.findall(r'"([^"]*)"', m.group(1))
            if items:
                result["keywords"] = list(items)

    return result


def verbalize(memories: list, keywords: list = None, new_state: dict = None, user_input: str = None, pinned: list = None, timestamps: list = None, pinned_timestamps: list = None, body_state: str = None) -> dict:
    """
    根据记忆生成内心独白和发言决策。
    返回: {"say": bool, "text": str}
    - say: True=应该说出口, False=仅内心思考
    - text: 内心独白或要说的话
    - pinned: 必须保留并传给 LLM 的记忆条目（如历史对话、上一轮回复），不被关键词过滤丢弃
    - timestamps: 与 memories 对齐的真实时间戳列表；提供时按时间升序排序（越早越靠前），否则保持原顺序（最新在前）
    - pinned_timestamps: 与 pinned 对齐的时间戳列表
    - body_state: 身体状态文本（睡前/刚醒），作为独立提示行，不写成记忆
    """
    if not memories and not pinned:
        return {"say": False, "text": ""}

    if keywords is None:
        keywords = []
    if user_input:
        keywords.append(user_input)

    ts = list(timestamps) if timestamps is not None else [None] * len(memories)

    pinned = list(pinned or [])
    pinned_set = set(pinned)
    if pinned_timestamps is None:
        pinned_ts_list = [None] * len(pinned)
    else:
        pinned_ts_list = list(pinned_timestamps) + [None] * max(0, len(pinned) - len(pinned_timestamps))

    # 普通记忆：剔除 pinned 条目（pinned 后续无条件保留）
    normal = []
    normal_ts = []
    for mem, t in zip(memories, ts):
        if mem not in pinned_set:
            normal.append(mem)
            normal_ts.append(t)

    # 关键词过滤：只作用于普通记忆
    if keywords:
        kw_hits = []
        kw_hits_ts = []
        for mem, t in zip(normal, normal_ts):
            if any(kw in mem for kw in keywords):
                kw_hits.append(mem)
                kw_hits_ts.append(t)
        if kw_hits:
            normal = kw_hits
            normal_ts = kw_hits_ts
    normal = normal[:10]
    normal_ts = normal_ts[:10]

    # 合并：pinned（历史对话、上一轮回复）无条件保留，后接普通记忆
    memories = [m for m in pinned] + normal
    ts = [t for t in pinned_ts_list] + normal_ts

    # 排序：越早越靠前（升序）；无时间戳（None）的条目排最后
    if timestamps is not None:
        indexed = sorted(
            range(len(memories)),
            key=lambda i: (ts[i] is None, ts[i] if ts[i] is not None else 0)
        )
        memories = [memories[i] for i in indexed]
    else:
        memories.reverse()

    # 全局内容查重：pinned 条目优先保留；重复内容只保留一条
    def _content_key(m):
        return re.sub(r"^\[[^\]]*\]\s*", "", m)

    seen_content = set()
    deduped_memories = []
    for m in memories:
        key = _content_key(m)
        if key in seen_content:
            if m in pinned_set:
                for i, existing in enumerate(deduped_memories):
                    if _content_key(existing) == key:
                        deduped_memories[i] = m
                        break
            continue
        seen_content.add(key)
        deduped_memories.append(m)
    memories = deduped_memories

    points = "\n".join([f"- {m}" for m in memories])

    append_log("="*30+"LLM特供记忆"+"="*30)
    append_log(points)

    now = datetime.datetime.now()
    state_hint = f"当前状态：{new_state}" if new_state else ""
    # 回复层要能自己判断"今天几号/星期几"，只给时分会让它把日期记错
    time_hint = f"现在时间为{now:%Y年%m月%d日 %H时%M分}"

    system_prompt = (
        f"你是{BOT_NAME}，正在思考。根据下面给出的记忆和情况，输出此刻的想法，并判断是否说出口。\n\n"
        "规则：\n"
        "- 如果有人在对你说什么，且你想回应，say 为 true；只是自然浮现的念头、联想，say 为 false\n"
        "- say 为 true 时，text 是一句简短口语，尽量 20 字以内，只能使用给定记忆中的信息\n"
        "- 不知道的事情不要提及，只能使用记忆或状态里明确给出的信息\n"
        "- 不要复述记忆里已有的同一内容，也不要重复自己刚才说过的话\n"
        "- 每条记忆开头的[刚刚]/[N天前]只是时间标签，表示这件事发生在何时，不是一条新消息\n"
        "- 记忆按时间排序，可以直接引用、补充观点或提问，但不要添加动作、神态或括号补充\n"
        "输出严格只包含 JSON：\n"
        '{"say": true/false, "text": "..."}\n'
    )

    body_hint = f"【身体状态】{body_state}\n\n" if body_state else ""
    user_content = (
        f"{state_hint}\n{time_hint}\n\n"
        f"{body_hint}"
        f"记忆片段（按时间排序）：\n{points}\n\n"
        "请结合这些记忆和对话情况，输出你此刻的想法。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    fingerprint_prompt("verbalize", system_prompt)

    # 认知循环高频调用：只需输出一句 JSON 短句，关闭推理以大幅降低输出 token
    PROBE.bump("llm.verbalize.calls")
    with PROBE.timed("llm.verbalize_ms"):
        reply = call_api_thinking(messages, max_tokens=300, thinking=False)

    append_log("="*30+"理解回复"+"="*30)
    append_log(reply)

    if not reply:
        return {"say": False, "text": ""}

    data = _safe_json_parse(reply)
    if "say" in data or "text" in data:
        return {
            "say": _coerce_bool(data.get("say", False)),
            "text": str(data.get("text", "")),
        }

    # 完全无法解析时降级
    append_log("*"*30+"JSON解析失败，降级处理"+"*"*30)
    return {"say": True, "text": reply}


DEFAULT_IMAGE_PROMPT = (
    "客观描述这张图片：主体是什么、关键可见文字有哪些，最多40字。"
    "文字只摘录关键片段，不要逐字复述；不要猜测人物身份、关系、情绪或意图。"
)
DEFAULT_STICKER_PROMPT = (
    "客观描述这个表情包：画面主体和关键文字，最多30字。"
    "不要猜测人物身份、关系、情绪或意图。"
)

_DESC_CACHE_FILE = "data/test/media_desc_cache.json"
_DESC_CACHE_VERSION = "v2"
_desc_cache = OrderedDict()
_desc_cache_lock = __import__("threading").RLock()


def _load_desc_cache():
    global _desc_cache
    if _desc_cache:
        return
    try:
        if os.path.exists(_DESC_CACHE_FILE):
            with open(_DESC_CACHE_FILE, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if isinstance(value, str):
                        _desc_cache[key] = value
    except Exception:
        _desc_cache = OrderedDict()


def _save_desc_cache():
    try:
        os.makedirs(os.path.dirname(_DESC_CACHE_FILE), exist_ok=True)
        tmp = _DESC_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(_desc_cache), f, ensure_ascii=False)
        os.replace(tmp, _DESC_CACHE_FILE)
    except Exception:
        pass


def _clip_desc(text: str, max_chars: int = None) -> str:
    """清理并截断描述：去换行/引号/多余空白，在标点处截断。"""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip().strip("“”\"'` ")
    if not max_chars or len(cleaned) <= max_chars:
        return cleaned
    head = cleaned[:max_chars]
    cut = max(head.rfind(p) for p in "。！？；，、,.!?;")
    if cut >= max_chars // 2:
        return head[:cut].rstrip("，、,;；")
    return head.rstrip()


async def describe_image_from_path(image_path: str, prompt: str = None, max_chars: int = None) -> str:
    """从本地路径读取图片，返回简短客观描述。

    - 默认使用短描述提示词，max_tokens=160，temperature=0；
    - 相同文件 + 相同提示词命中内容哈希缓存，不再调用视觉模型；
    - max_chars 控制最终长度（图片 60、表情包 30），在标点处截断。
    """
    if not os.path.exists(image_path):
        return f"[图片文件不存在: {image_path}]"

    if prompt is None:
        prompt = DEFAULT_IMAGE_PROMPT
    if max_chars is None:
        try:
            max_chars = int(config.get("image_desc_max_chars", 60))
        except (TypeError, ValueError):
            max_chars = 60

    try:
        async def read_and_encode():
            async with aiofiles.open(image_path, "rb") as f:
                data = await f.read()
                return data, base64.b64encode(data).decode('utf-8')

        raw_bytes, base64_str = await read_and_encode()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        cache_key = f"{_DESC_CACHE_VERSION}|{digest}|{prompt}|{max_chars}"

        _load_desc_cache()
        with _desc_cache_lock:
            cached = _desc_cache.get(cache_key)
            if cached:
                _desc_cache.move_to_end(cache_key)
                PROBE.bump("vision.cache_hit")
                return cached

        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.gif': 'image/gif',
            '.webp': 'image/webp', '.bmp': 'image/bmp'
        }
        mime_type = mime_map.get(ext, 'image/jpeg')

        PROBE.bump("vision.calls")
        with PROBE.timed("vision.ms"):
            response = await asyncio.to_thread(
                lambda: client_.chat.completions.create(
                    model=MODEL_,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{mime_type};base64,{base64_str}"
                                    }
                                },
                            ],
                        }
                    ],
                    stream=False,
                    max_tokens=160,
                    temperature=0,
                    timeout=30.0,
                )
            )
        _record_usage("vision", response)
        content = response.choices[0].message.content
        reply = _clip_desc(content or "", max_chars)
        append_log("="*30+"多模态理解"+"="*30)
        append_log(reply)
        PROBE.record_ms("vision.desc_chars", len(reply))

        try:
            with _desc_cache_lock:
                _desc_cache[cache_key] = reply
                _desc_cache.move_to_end(cache_key)
                try:
                    limit = max(1, int(config.get("media_desc_cache_size", 500)))
                except (TypeError, ValueError):
                    limit = 500
                while len(_desc_cache) > limit:
                    _desc_cache.popitem(last=False)
                _save_desc_cache()
        except Exception:
            pass
        return reply
    except Exception as e:
        PROBE.bump("vision.errors")
        return f"[图片识别失败: {e}]"

async def describe_audio_from_path(audio_path: str, prompt: str = "请描述这段音频内容") -> str:
    return await _describe_media_from_path(audio_path, "audio", prompt)

async def describe_video_from_path(video_path: str, prompt: str = "请描述这段视频内容") -> str:
    return await _describe_media_from_path(video_path, "video", prompt)

async def _describe_media_from_path(file_path: str, media_type: str, prompt: str) -> str:
    """通用媒体描述（供音频/视频调用）"""
    if not os.path.exists(file_path):
        return f"[文件不存在: {file_path}]"
    try:
        source_path = file_path
        converted_path = None
        if media_type == "audio":
            source_path = await _convert_audio_to_wav(file_path)
            converted_path = source_path if source_path != file_path else None

        # 读取并编码
        async with aiofiles.open(source_path, "rb") as f:
            data = await f.read()
            base64_str = base64.b64encode(data).decode('utf-8')
        if media_type == "audio":
            content = [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": base64_str, "format": "wav"}},
            ]
        else:
            ext = os.path.splitext(file_path)[1].lower()
            mime_map = {
                '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.avi': 'video/x-msvideo'
            }
            mime_type = mime_map.get(ext, 'video/mp4')
            content = [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": f"data:{mime_type};base64,{base64_str}"}},
            ]

        response = await asyncio.to_thread(
            lambda: client_.chat.completions.create(
                model=MODEL_,
                messages=[{"role": "user", "content": content}],
                stream=False,
                max_tokens=200,
                timeout=60.0,
            )
        )
        response_content = response.choices[0].message.content
        reply = response_content.strip() if response_content else ""
        append_log("="*30+"多模态理解"+"="*30)
        append_log(reply)
        return reply
    except Exception as e:
        print(f"[API ERROR] 多模态解析：{e}")
        return f"[{media_type}识别失败: {e}]"
    finally:
        if 'converted_path' in locals() and converted_path:
            try:
                os.remove(converted_path)
            except OSError:
                pass


async def _convert_audio_to_wav(audio_path: str) -> str:
    """将 NapCat 常见的 AMR/音频文件转换为 Lucis 可接受的 WAV。"""
    if os.path.splitext(audio_path)[1].lower() == ".wav":
        return audio_path

    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return wav_path
    except Exception:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        raise RuntimeError("语音格式转换失败，请确认 ffmpeg 已安装且支持 NapCat 音频格式")
