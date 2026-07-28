# core/cognition.py
import json
import os
import datetime
import random
from collections import deque

from .memory_engine import (
    create_memory, retrieve_similar, add_link, semantic_dedup, pathfind_activation, retrieve_by_exact_keywords, _load_memory_from_db, 
    DEFAULT_HALF_LIFE
)
from .llm_interface import decompose_input, verbalize
from .virtual_clock import clock
from utils.dialogue_state import set_state, reset_state, get_state
from utils.persistence import save_state
from utils.monitor import append_log, clear_log # 清空外部监视器，测试时不用

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

# ========== 永续认知循环全局 ==========
_new_message_keywords_deque = deque(maxlen=100)   # 新消息关键词队列
_shallow_pool = deque(maxlen=20)                  # 浅层意识池
_cognitive_running = False                        # 循环运行状态
_graceful_stop = False                            # 优雅停止标志（完成当前轮，不复搜）

def inject_message_keywords(keywords: list):
    """由消息处理层调用，将新消息的关键词注入认知循环"""
    if keywords:
        _new_message_keywords_deque.append(keywords)

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

def retrieve_and_diffuse(keywords: list, max_memories: int = 10) -> list:
    """
    关键词检索 + 受限BFS扩散，返回带时间标记的记忆片段列表。
    供 cognitive_loop 和 generate_response 共用。
    """
    if not keywords:
        return []

    # 语义检索（faiss）
    seed_ids = []
    faiss_results = []
    for kw in keywords:
        similar = retrieve_similar(kw, k=5)
        for score, mem in similar:
            if mem["id"] not in seed_ids:
                seed_ids.append(mem["id"])
            faiss_results.append((score, mem))

    # 精确关键词检索（词网）
    exact_results = retrieve_by_exact_keywords(keywords, k=5)
    for score, mem in exact_results:
        if mem["id"] not in seed_ids:
            seed_ids.append(mem["id"])

    # BFS扩散
    activated_memories = []
    if seed_ids:
        activated = pathfind_activation(seed_ids, max_stamina=3, top_k=8)
        activated_memories = [(mem, score) for mem, score in activated]

    # 合并扩散结果 + 检索结果
    combined = {}
    for mem, score in activated_memories:
        mem_id = mem["id"]
        content = mem["content"]
        if mem_id not in combined or score > combined[mem_id][0]:
            combined[mem_id] = (score, content, mem)

    for score, mem in faiss_results + exact_results:
        mem_id = mem["id"]
        content = mem["content"]
        if mem_id not in combined or score > combined[mem_id][0]:
            combined[mem_id] = (score, content, mem)

    # 排序、去重、截断
    sorted_mems = sorted(combined.values(), key=lambda x: x[0], reverse=True)
    seen_texts = set()
    final_mem_objects = []
    for score, content, mem in sorted_mems:
        if content not in seen_texts:
            seen_texts.add(content)
            final_mem_objects.append(mem)
        if len(final_mem_objects) >= max_memories:
            break

    # 添加时间标记
    from utils.time_phrases import get_relative_time_phrase

    timed_memories = []
    for mem in final_mem_objects:
        virtual_ts = mem.get("creation_time", 0)
        real_ts = clock.to_real_time(virtual_ts)
        phrase = get_relative_time_phrase(real_ts)
        timed_memories.append(f"[{phrase}] {mem['content']}")

    return timed_memories


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
    result = verbalize(timed_memories, keywords, new_state, user_input)
    reply = ""
    if isinstance(result, dict):
        reply = result.get("text", "")
    elif result:
        reply = str(result)

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


def request_graceful_stop():
    """
    请求认知循环在完成当前轮次后优雅停止。
    当前轮会正常输出回复，但跳过复搜步骤，然后退出。
    """
    global _graceful_stop, _cognitive_running
    _graceful_stop = True
    _cognitive_running = False  # 阻止在空闲时启动新的一轮


async def cognitive_loop(send_func=None, target_group_id: str = None):
    """
    永续认知循环 —— 辉夜的"默认模式网络"永远在线。
    无论是否有用户输入，循环始终运行：
      合并关键词 → 检索扩散 → 拼接层(生成内心独白+发言决策)
      → 存入记忆 → 复搜层(提取新关键词) → 检查新消息 → 循环

    在 NapCat 连接后启动，断开时 request_graceful_stop 触发：
      当前轮继续执行到回复输出，跳过复搜后退出。
    """
    global _new_message_keywords_deque, _shallow_pool, _cognitive_running, _graceful_stop

    from .llm_interface import extract_curiosity_keywords, add_to_history, get_history_context
    from .memory_engine import memories, access_memory

    # 重置停止标志，允许新会话启动
    _graceful_stop = False
    _cognitive_running = True
    current_keywords = []
    loop_count = 0

    append_log("="*40)
    append_log("[认知循环] 永续认知循环启动")
    append_log("="*40)

    while not _graceful_stop:
        try:
            loop = asyncio.get_event_loop()
            loop_count += 1

            # ============================
            # Step 1: 收集本轮输入
            # ============================
            new_kws_batch = []
            while _new_message_keywords_deque:
                kws = _new_message_keywords_deque.popleft()
                new_kws_batch.extend(kws)

            if new_kws_batch:
                current_keywords = list(set(current_keywords + new_kws_batch))
                append_log(f"[认知循环] 合并新消息关键词: {new_kws_batch}")

            # ============================
            # Step 2: 若无关键词，从记忆库取种子
            # ============================
            if not current_keywords:
                if memories:
                    seed_text = None
                    if _shallow_pool:
                        mem_id = random.choice(list(_shallow_pool))
                        mem = memories.get(mem_id)
                        if mem:
                            seed_text = mem["content"][:50]
                    else:
                        mem_id = random.choice(list(memories.keys()))
                        mem = memories.get(mem_id)
                        if mem:
                            seed_text = mem["content"][:50]
                            access_memory(mem_id)

                    if seed_text:
                        current_keywords = [seed_text]
                        append_log(f"[认知循环] 无新消息，随机种子: {seed_text[:30]}...")
                    else:
                        await asyncio.sleep(10)
                        continue
                else:
                    await asyncio.sleep(10)
                    continue

            # ============================
            # Step 3: 检索与扩散
            # ============================
            related_memories = retrieve_and_diffuse(current_keywords, max_memories=10)
            if not related_memories:
                append_log("[认知循环] 无相关记忆，跳过本轮")
                current_keywords = []
                await asyncio.sleep(3)
                continue

            # ============================
            # Step 4: 拼接层处理
            # ============================
            state = get_state()
            dialogue_history = get_history_context()

            result = await loop.run_in_executor(
                None, lambda: verbalize(
                    related_memories, current_keywords, state,
                    dialogue_history=dialogue_history
                )
            )

            if not result or not isinstance(result, dict):
                current_keywords = []
                await asyncio.sleep(3)
                continue

            thought_text = result.get("text", "").strip()
            should_speak = result.get("say", False)

            if not thought_text:
                append_log("[认知循环] verbalize 返回空，跳过")
                await asyncio.sleep(3)
                continue

            # ============================
            # Step 5: 存入记忆库
            # ============================
            if should_speak:
                memory_text = f"我说：{thought_text}"
            else:
                memory_text = f"我想：{thought_text}"

            dedup_id = semantic_dedup(memory_text)
            if not dedup_id:
                mem_id = create_memory(memory_text)
                _shallow_pool.append(mem_id)

            append_log(f"[认知循环] {'【发言】' if should_speak else '【内心】'}: {thought_text}")

            # ============================
            # Step 6: 发送消息（若应发言）
            # ============================
            if should_speak and thought_text and send_func and target_group_id:
                await send_func(target_group_id, thought_text)
                add_to_history(None, None, thought_text)
                from utils.event_bus import BUS
                BUS.message.emit("辉夜", thought_text, "QQ")

            # ============================
            # Step 7: 优雅停止检查（不复搜）
            # ============================
            if _graceful_stop:
                append_log("[认知循环] 收到停止信号，完成本轮回复，跳过复搜")
                break

            # ============================
            # Step 7b: 复搜层 —— 提取新关键词
            # ============================
            kw_context = related_memories + [thought_text]
            new_keywords = await loop.run_in_executor(
                None, extract_curiosity_keywords, kw_context
            )

            if new_keywords:
                current_keywords = new_keywords
                append_log(f"[认知循环] 复搜关键词: {new_keywords}")
            else:
                if loop_count % 5 == 0:
                    current_keywords = []
                else:
                    current_keywords = current_keywords[:2]

            # ============================
            # Step 8: 休眠
            # ============================
            await asyncio.sleep(2)

        except asyncio.CancelledError:
            append_log("[认知循环] 已停止")
            break
        except Exception as e:
            append_log(f"[认知循环] 异常: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(10)

    _cognitive_running = False
    append_log("[认知循环] 已退出")