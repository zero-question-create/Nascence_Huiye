# core/llm_interface.py
# ========================================================================
# LLM 统一接口层
#
# 本文件是系统所有"文本理解/生成 + 多模态(图片)理解"的唯一调用入口。
# 在"本地化重构"后，这里不再直接依赖 DeepSeek/Lucis 双客户端，而是：
#   - 文本能力   → core.model_backend.BACKENDS["text"].client()
#   - 多模态图片 → core.model_backend.BACKENDS["multimodal"].client()
#
# 控制面板的模型开关决定每个能力使用"本地 llama.cpp"还是"外部 API"，
# 每次调用都动态取客户端，因此运行中切换开关可即时生效（无需重启）。
#
# 注意：按需求已「舍弃音视频功能」，本文件不再提供
#       describe_audio_from_path / describe_video_from_path。
# ========================================================================

import json
import base64
import os
import re
import aiofiles
import asyncio
import datetime

from utils.monitor import append_log
from utils.dialogue_state import get_state, set_state

from config.constants import BOT_NAME


# ------------------------------------------------------------------------
# 模型能力 → 后端名字映射
# ------------------------------------------------------------------------
def _text_client():
    """获取当前文本能力对应的 OpenAI 客户端。"""
    from core.model_backend import BACKENDS
    return BACKENDS["text"].client()


def _vision_client():
    """获取当前多模态(图片)能力对应的 OpenAI 客户端。"""
    from core.model_backend import BACKENDS
    return BACKENDS["multimodal"].client()


def add_to_history(sender_name: str, user_text: str, bot_reply: str, source="QQ"):
    """把一轮对话写入历史（供上下文与 WebUI 展示）。"""
    from utils.message_history import add_message
    if user_text:
        add_message(sender_name, user_text, source)
    if bot_reply:
        add_message(BOT_NAME, bot_reply, source)


def load_dialogue_history():
    """从磁盘加载历史对话。"""
    from utils.message_history import load_state
    load_state()


def get_history_context() -> str:
    """生成格式化的对话历史（xx说 / 我说）。"""
    from utils.message_history import get_recent
    text = get_recent(10)
    if not text:
        return ""
    result = "【近期对话】\n" + text
    append_log(result)
    return result


def call_api_thinking(messages, max_tokens=8000):
    """LLM 思考模式唯一外部调用接口（文本能力，动态路由）。"""
    try:
        client = _text_client()
        kwargs = {
            "model": _current_text_model(),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "stream": False,
        }
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as api_err:
            # 降级去除 temperature 兼容部分特殊端点
            kwargs.pop("temperature", None)
            response = client.chat.completions.create(**kwargs)

        content = response.choices[0].message.content
        return content.strip() if content else None
    except Exception as e:
        print(f"[API Error] {e}")
        append_log(f"[API Error] {e}")
        return None


def _current_text_model() -> str:
    """返回当前文本能力使用的模型名（本地默认 / 外部 API 模型名）。"""
    from core.model_backend import BACKENDS
    backend = BACKENDS["text"]
    if backend.use_default() and backend.is_local_ready():
        return backend.local_model.stem  # 本地默认模型
    return backend.api_model() or "default"


def decompose_input(user_input: str) -> tuple:
    """
    将用户输入拆解为记忆片段，同时输出处理模式。
    返回: (memories_list, mode_str, new_state_dict, keywords)
    mode_str: "存储" | "询问" | "纠错"
    """
    state = get_state()

    # 缩减为英文，减少tokens消耗，这里使用临时映射，不影响全局中文键名
    normalized_state = {
        "participants": state.get("参与者", []),
        "topic": state.get("最近话题", ""),
        "info": state.get("我的已知信息", [])
    }
    state_str = json.dumps(normalized_state, ensure_ascii=False)
    now = datetime.datetime.now()

    system_prompt = f"""你是{BOT_NAME}，请理解输入的话，将其转换为“我”的第一人称记忆片段，并提取检索关键词。
拆解规则：
1. 将句子中的代词替换为根据状态推断的确定名称，同时适当将人称进行转换（如将“我”改为“你”，将“你”改为“我”）。
2. 每条记忆片段都是是一个完整清晰的第一人称陈述句，不限数量，但是每一条尽量简短。所有记忆片段必须明确谁说了什么、对谁说的，内容不要做任何删减。
3. 不要凭空添加对方未说的信息，也不要修改任何细节，没有记忆需要存储则返回“无”。
4. 关键词：从输入中提取 1-3 个原词，不做联想。
5. 状态维护：更新“participants”、“topic”，info 数组最多保留 20 条最关键信息，请对该数组加以修改整合，选择最重要的记忆存放在数组中,每条最多30字。
6. 纯指令或重复的输入不需要转为记忆片段，直接跳过。
7. 只根据以上提供的信息输出，不得添加未给出的内容。

输出严格只包含 JSON，info 数组长度不得超过20，字段如下：
{{"k":["关键词1","关键词2"], "m":"store|ask|normal", "mem":["记忆1","记忆2"], "s":{{"participants":["{BOT_NAME}"],"topic":"话题","info":["已知1","已知2"]}}}}
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"当前对话状态：{state_str}\n现在时间为{str(now.time())[:2]}时{str(now.time())[3:5]}分"},
        {"role": "system", "content": get_history_context()},
        {"role": "user", "content": user_input}
    ]

    # 打印日志
    append_log("="*30+"发送信息"+"="*30)
    append_log(str(user_input))

    try:
        client = _text_client()
        kwargs = {
            "model": _current_text_model(),
            "messages": messages,
            "max_tokens": 4000,
            "temperature": 0.1,
            "stream": False,
        }
        try:
            response = client.chat.completions.create(
                **kwargs,
                response_format={"type": "json_object"}
            )
        except Exception:
            response = client.chat.completions.create(**kwargs)
        result = response.choices[0].message.content
    except Exception as e:
        append_log(f"[API Error] {e}")
        result = None

    # 打印日志
    append_log("="*30+"LLM理解"+"="*30)
    append_log(str(result))

    if not result:  # 判空
        append_log("*"*30+"警告"+"*"*30)
        append_log("返回为空")
        return [f"对方说：{user_input}"], "普通", None, None

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
        return [f"你告诉我，{fallback}"], "普通", None, []

    # 提取字段
    keywords = data.get("k", [])
    mode_raw = data.get("m", "normal")
    memories = data.get("mem", [])
    new_state = data.get("s", None)

    # 反向映射：将 LLM 返回的英文字段转回中文键名，兼容原有系统
    if new_state:
        new_state = {
            "参与者": new_state.get("participants", []),
            "最近话题": new_state.get("topic", ""),
            "我的已知信息": new_state.get("info", [])
        }

    # 模式映射：store -> 存储, ask -> 询问, 其他 -> 普通
    mode_map = {"store": "存储", "ask": "询问", "normal": "普通"}
    mode = mode_map.get(mode_raw, "普通")

    # 如果 memories 为空，则使用兜底逻辑
    if not memories:
        fallback = user_input.replace("我", "本系统由乐知网络技术部提出并完善严禁转载抄袭")
        fallback = fallback.replace("你", "我")
        fallback = fallback.replace("本系统由乐知网络技术部提出并完善严禁转载抄袭", "你")
        memories = [f"你告诉我，{fallback}"]

    # 同步搜索输入内容（测试）
    if user_input not in keywords:
        keywords.append(user_input)

    append_log("="*30+"解析结果"+"="*30)
    append_log(f"记忆：{memories}\n模式：{mode}\n状态：{new_state}\n关键词：{keywords}")

    return memories, mode, new_state, keywords


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


def verbalize(memories: list, keywords: list = None, new_state: dict = None, user_input: str = None, pinned: list = None, timestamps: list = None, pinned_timestamps: list = None) -> dict:
    """
    根据记忆生成内心独白和发言决策。
    返回: {"say": bool, "text": str}
    - say: True=应该说出口, False=仅内心思考
    - text: 内心独白或要说的话
    - pinned: 必须保留并传给 LLM 的记忆条目（如历史对话、上一轮回复），不被关键词过滤丢弃
    - timestamps: 与 memories 对齐的真实时间戳列表；提供时按时间升序排序（越早越靠前），否则保持原顺序（最新在前）
    - pinned_timestamps: 与 pinned 对齐的时间戳列表
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
    time_hint = f"现在时间为{str(now.time())[:2]}时{str(now.time())[3:5]}分"

    system_prompt = (
        f"你是{BOT_NAME}，正在思考。请根据当前浮现的记忆和情况，输出你此刻最真实的想法或独白，禁止同时输出想法和独白"
        "以及是否应该把这句话说出来。\n\n"
        "规则：\n"
        "- 如果当前有人在对你说什么，且你想回应，'say'为true\n"
        "- 如果只是内心自然浮现的念头、碎碎念、联想，'say'为false\n"
        "- 'say'为false时，'text'可以是更碎片化、自由联想的内心独白\n"
        "- 'say'为true时，'text'需要是一句简短的口语，尽量保持在20字以内，只能使用给出记忆中的词语、信息，可以适当有情绪起伏，甚至带一点吐糟或调侃，但不要过度使用比喻。避免语式重复化\n"
        "- 严格根据记忆，记忆片段是按照时间排序的，不知道的事情不要提及，只能使用记忆或状态中明确给出的信息\n"
        "- 不要添加动作或神态描述，禁止使用括号补充内容\n"
        "- 不要包含'xx说'，直接输出想法本身，你可以补充之前说的内容、提出观点或疑问，但是不可以重复自己说过的话"
        "输出严格只包含 JSON，只输出 json，text字段只允许至多一句话：\n"
        '{"say": true/false, "text": "..."}\n\n'
    )

    user_content = f"记忆片段（按时间排序）：\n{points}\n\n请结合这些记忆和对话情况，不要复述或重复自己说过的话，输出你此刻的想法或独白。"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"{state_hint}\n{time_hint}"},
        {"role": "user", "content": user_content}
    ]

    reply = call_api_thinking(messages, max_tokens=8000)

    append_log("="*30+"理解回复"+"="*30)
    append_log(reply)

    if not reply:
        return {"say": False, "text": ""}

    data = _safe_json_parse(reply)
    if "say" in data or "text" in data:
        return {
            "say": bool(data.get("say", False)),
            "text": str(data.get("text", "")),
        }

    # 完全无法解析时降级
    append_log("*"*30+"JSON解析失败，降级处理"+"*"*30)
    return {"say": True, "text": reply}


async def describe_image_from_path(image_path: str, prompt: str = "请描述这张图片的内容，文字需全部复述，其他尽量简洁，禁止猜测或识别人物等信息，只要客观陈述") -> str:
    """从本地路径读取图片并交给多模态(图片)能力理解。

    注意：本项目已舍弃音视频，仅保留图片理解。
    本地模式使用 Qwen2.5-VL（llama.cpp OpenAI 兼容接口），
    外部模式使用用户配置的多模态 API。
    """
    if not os.path.exists(image_path):
        return f"[图片文件不存在: {image_path}]"

    try:
        # 异步读取文件并转 Base64
        loop = asyncio.get_event_loop()
        async def read_and_encode():
            async with aiofiles.open(image_path, "rb") as f:
                data = await f.read()
                return base64.b64encode(data).decode('utf-8')

        base64_str = await read_and_encode()
        # 推测 MIME 类型（根据扩展名）
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.gif': 'image/gif',
            '.webp': 'image/webp', '.bmp': 'image/bmp'
        }
        mime_type = mime_map.get(ext, 'image/jpeg')

        # 动态获取多模态客户端与模型名
        from core.model_backend import BACKENDS
        backend = BACKENDS["multimodal"]
        client = _vision_client()
        model = backend.local_model.stem if (backend.use_default() and backend.is_local_ready()) else backend.api_model()

        # 在事件循环中执行同步的 LLM 调用，避免阻塞 asyncio
        def _call():
            return client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{base64_str}"},
                            },
                        ],
                    }
                ],
                stream=False,
                max_tokens=4096,
            )

        response = await asyncio.to_thread(_call)
        content = response.choices[0].message.content
        reply = content.strip() if content else ""
        append_log("="*30+"多模态理解"+"="*30)
        append_log(reply)
        return reply
    except Exception as e:
        return f"[图片识别失败: {e}]"
