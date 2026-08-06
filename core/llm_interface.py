# core/llm_interface.py
import json
import base64
import os
import re
import aiofiles
import asyncio
import subprocess
import tempfile
#import requests    # 使用网络访问链接LLM（已弃用）
from openai import OpenAI
import datetime
from utils.monitor import append_log
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


def add_to_history(sender_name: str, user_text: str, bot_reply: str, source="QQ"):
    from utils.message_history import add_message
    if user_text:
        add_message(sender_name, user_text, source)
    if bot_reply:
        add_message(BOT_NAME, bot_reply, source)


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

def call_api_thinking(messages, max_tokens=8000):
    """
    LLM思考模式唯一外部调用接口
    """
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
            stream=False,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        content = response.choices[0].message.content
        return content.strip() if content else None
    except Exception as e:
        print(f"[API Error] {e}")
        return None

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
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=8000,
            temperature=0.1,
            stream=False,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
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
        "- 'say'为true时，'text'需要是一句简短的口语，尽量保持在20字以内\n"
        "- 严格根据记忆，不知道的事情不要提及，只能使用记忆或状态中明确给出的信息\n"
        "- 不要添加动作或神态描述，禁止使用括号补充内容\n"
        "- 不要包含'xx说'，直接输出想法本身"
        "输出严格只包含 JSON：\n"
        '{"say": true/false, "text": "..."}\n\n'
    )

    user_content = f"记忆片段：\n{points}\n\n请结合这些记忆和对话情况，不要复述或重复自己说过的话，输出你此刻的想法。"

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


async def describe_image_from_path(image_path: str, prompt: str = "请描述这张图片的内容，文字需全部复述，其他尽量简洁") -> str:
    """
    从本地路径读取图片
    """
    # 检查文件是否存在
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
        
        # 调用 API
        response = client_.chat.completions.create(
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
            max_tokens=10000,
        )
        content = response.choices[0].message.content
        reply = content.strip() if content else ""
        append_log("="*30+"多模态理解"+"="*30)
        append_log(reply)
        return reply
    except Exception as e:
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

        response = client_.chat.completions.create(
            model=MODEL_,
            messages=[{"role": "user", "content": content}],
            stream=False,
            max_tokens=10000,
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
