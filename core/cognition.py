# core/cognition.py
import json
import os
import datetime

from .memory_engine import (
    create_memory, retrieve_similar, add_link, semantic_dedup, pathfind_activation, retrieve_by_exact_keywords, _load_memory_from_db, 
    DEFAULT_HALF_LIFE
)
from .llm_interface import decompose_input, verbalize
from .virtual_clock import clock
from utils.dialogue_state import set_state, reset_state
from utils.persistence import save_state
from utils.monitor import clear_log # 清空外部监视器，测试时不用

# 不同模式的半衰期配置
MODE_HALF_LIFE = {
    "存储": 7 * 24 * 3600,   # 7天
    "询问": DEFAULT_HALF_LIFE,  # 2天
    "普通": DEFAULT_HALF_LIFE,  # 2天
}

# 不同模式的检索数量
MODE_RETRIEVAL_K = {
    "存储": 5,
    "询问": 8,
    "普通": 5,
}

# undo快照路径
UNDO_FILE = "data/test/undo_snapshot.json"

current_speaker: str = None

# ========== 睡眠配置 ==========
SLEEP_START_HOUR = 23           # 开始困倦/准备睡觉的小时
SLEEP_END_HOUR = 6              # 完全醒来的小时
DROWSY_MARGIN = 15              # 睡前/醒后迷糊的分钟数

def _get_drowsy_memory() -> str:
    """
    根据当前真实时间，返回应注入的困倦/刚醒虚拟记忆。
    若不在迷糊时段，返回 None。
    """
    now = datetime.datetime.now()
    hour = now.hour
    minute = now.minute

    # 睡前困倦：SLEEP_START_HOUR 前一小时的最后 DROWSY_MARGIN 分钟内
    if hour == SLEEP_START_HOUR - 1 and minute >= 60 - DROWSY_MARGIN:
        return "[现在] 我现在有点困，想睡觉了"
    # 刚醒迷糊
    elif hour == SLEEP_END_HOUR and minute < DROWSY_MARGIN:
        return "[现在] 我刚睡醒，还有点迷糊"
    else:
        return None

def mask_brackets(text: str) -> str:
    """
    移除 text 中所有成对括号及其内部内容。
    支持：()、（）、[]、【】——中英文半角全角。
    未匹配的括号保留原样。
    """
    # 定义括号对（左->右）
    PAIRS = {
        '(': ')', '（': '）',
        '[': ']', '【': '】',
    }
    RIGHT_TO_LEFT = {v: k for k, v in PAIRS.items()}  # 右括号反查左括号

    stack = []          # 栈，记录每个左括号在结果串中的位置
    output = []         # 输出字符列表
    removal_ranges = [] # 待删除区间 [start, end]（含两端）

    for i, ch in enumerate(text):
        if ch in PAIRS:                     # 左括号
            stack.append(len(output))       # 记下当前位置（在 output 中的索引）
            output.append(ch)               # 暂时保留
        elif ch in RIGHT_TO_LEFT:           # 右括号
            if stack:                       # 有匹配的左括号
                left_pos = stack.pop()
                removal_ranges.append((left_pos, len(output)))
                output.append(ch)           # 暂时保留
            else:
                output.append(ch)           # 多余的右括号，保留
        else:
            output.append(ch)               # 普通字符

    # 栈中剩余未匹配的左括号位置（保留不删）
    unmatched = set(stack)

    # 构建最终结果
    result = []
    skip_until = -1
    for idx, ch in enumerate(output):
        if idx <= skip_until:
            continue
        # 检查是否在某个要删除的区间内
        removed = False
        for start, end in removal_ranges:
            if start <= idx <= end:
                skip_until = end
                removed = True
                break
        if not removed:
            result.append(ch)

    return ''.join(result)

def generate_response(user_input: str, current_speaker: str = None) -> str:
    """
    核心回复逻辑：
    1、预处理输入
    2、储存撤销快照
    3、LLM拆解
    4、语义查找
    5、体力行走扩散
    6、LLM拼接
    """
    # 预处理
    #clear_log()     # 可选择不清空，不清空的话直接把这行注释掉
    user_input = user_input.replace("*","")     # 对输入进行预处理，防止污染，必要时可以注释这一行
    user_input = mask_brackets(user_input)      # 对输入进行预处理，防止污染，必要时可以注释这一行
    # 撤销操作快照
    from core.memory_engine import memories, links
    from core.llm_interface import get_state
    snapshot = {
        "memories": memories,
        "links": {f"{s}||{t}": v for (s, t), v in links.items()},
        "state": get_state()
    }
    os.makedirs("data/test", exist_ok=True)
    with open(UNDO_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False)

    # 阶段A：LLM 拆解输入，拿到关键词
    mem_fragments, mode, new_state, keywords = decompose_input(user_input)

    # 更新对话状态
    if new_state:
        set_state(new_state)
        save_state()

    # 存储记忆（按模式设置半衰期）
    half_life = MODE_HALF_LIFE.get(mode, DEFAULT_HALF_LIFE)
    user_mem_ids = []
    for frag in mem_fragments:
        if not semantic_dedup(frag):
            mid = create_memory(frag, half_life=half_life)
            user_mem_ids.append(mid)

    # 阶段B：关键词驱动的语义检索（faiss）
    seed_ids = []
    faiss_results = []  # 新增：收集 faiss 检索结果
    if keywords:
        for kw in keywords:
            similar = retrieve_similar(kw, k=5)
            for score, mem in similar:
                if mem["id"] not in seed_ids:
                    seed_ids.append(mem["id"])
                faiss_results.append((score, mem))

    # 新增：精确关键词检索（词网）
    exact_results = []
    if keywords:
        exact_results = retrieve_by_exact_keywords(keywords, k=5)
        for score, mem in exact_results:
            if mem["id"] not in seed_ids:
                seed_ids.append(mem["id"])

    # 合并两路检索结果（用于后续兜底）
    all_retrieved = faiss_results + [(score, mem) for score, mem in exact_results]

    # 阶段C：体力行走式扩散（从种子出发，沿有向图走）
    activated_memories = []  # 扩散激活的记忆
    if seed_ids:
        activated = pathfind_activation(seed_ids, max_stamina=3, top_k=8)
        activated_memories = [(mem["content"], score, mem["id"]) for mem, score in activated]

    # 收集检索阶段的所有高分结果（不去重，作为兜底）
    all_retrieved = []
    if keywords:
        for kw in keywords:
            similar = retrieve_similar(kw, k=5)
            for score, mem in similar:
                all_retrieved.append((mem["content"], score, mem["id"]))

    # 合并：扩散结果 + 检索结果
    combined = {}
    for content, score, mem_id in activated_memories:
        mem = memories.get(mem_id) or _load_memory_from_db(mem_id)
        if mem and (mem_id not in combined or score > combined[mem_id][0]):
            combined[mem_id] = (score, content, mem)

    for content, score, mem_id in all_retrieved:
        mem = memories.get(mem_id) or _load_memory_from_db(mem_id)
        if mem and (mem_id not in combined or score > combined[mem_id][0]):
            combined[mem_id] = (score, content, mem)

    # 排序、去重、截断
    sorted_mems = sorted(combined.values(), key=lambda x: x[0], reverse=True)
    seen_texts = set()
    final_mem_objects = []
    for score, content, mem in sorted_mems:
        if content not in seen_texts:
            seen_texts.add(content)
            final_mem_objects.append(mem)
        if len(final_mem_objects) >= 10:
            break

    # 在生成带时间标记的记忆文本处
    from core.virtual_clock import clock
    from utils.time_phrases import get_relative_time_phrase

    timed_memories = []
    for mem in final_mem_objects:
        virtual_ts = mem.get("creation_time", 0)
        real_ts = clock.to_real_time(virtual_ts)
        phrase = get_relative_time_phrase(real_ts)
        timed_memories.append(f"[{phrase}] {mem['content']}")

    drowsy = _get_drowsy_memory()
    if drowsy:
        memories.insert(0, drowsy)

    # 阶段D：LLM 拼接回复（传入关键词作为指引）
    reply = verbalize(timed_memories, keywords, new_state, user_input)
    if reply:
        reply = reply.strip("“")
        reply = reply.strip("”")

        if current_speaker:
            reply_memory = f"我告诉{current_speaker}，{reply}"
        else:
            reply_memory = f"我说，{reply}"

        # 存储回复
        dedup_id = semantic_dedup(reply_memory)
        if dedup_id:
            if user_mem_ids:
                add_link(user_mem_ids[0], dedup_id, 0.8, "causal")
        else:
            bot_mem_id = create_memory(reply_memory)
            if user_mem_ids:
                add_link(user_mem_ids[0], bot_mem_id, 0.8, "causal")
    return reply, user_input

def reset_dialogue():
    reset_state()

# ====== 附加功能：QQ接口 ======

import asyncio
from typing import Tuple, List
import re

async def process_dialogue(augmented_input: str, extra_context: str = "") -> Tuple[str, List[str]]:
    full_input = augmented_input
    if extra_context:
        full_input = f"{extra_context}\n{augmented_input}"

    # 解析当前说话人（从增强输入中提取）
    current_speaker = None
    match = re.match(r"^\[(.+?) 对 .+? 说\]：", augmented_input)
    if match:
        current_speaker = match.group(1)

    loop = asyncio.get_event_loop()
    reply, user_input = await loop.run_in_executor(
        None, generate_response, full_input, current_speaker
    )

    from core.memory_engine import _last_created_ids
    new_mem_ids = _last_created_ids.copy()
    _last_created_ids.clear()

    return reply, new_mem_ids