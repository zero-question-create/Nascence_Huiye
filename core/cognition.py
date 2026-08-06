# core/cognition.py
import json
import os
import re
import datetime
import random
from collections import deque

import jieba

from .memory_engine import (
    create_memory, retrieve_similar, add_link, semantic_dedup, pathfind_activation, retrieve_by_exact_keywords, _load_memory_from_db, 
    DEFAULT_HALF_LIFE
)
from .llm_interface import decompose_input, verbalize
from .virtual_clock import clock
from config.constants import BOT_NAME
from utils.dialogue_state import set_state, reset_state, get_state
from utils.persistence import save_state
from utils.monitor import append_log

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

# ========== 反刍抑制常量 ==========
RUMINATION_THRESHOLD = 2                            # 关键词连续出现 >= 此值，触发抑制
SEED_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3      # 种子抑制轮数
EDGE_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3      # 路径抑制轮数
KEYWORD_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3   # 关键词抑制轮数，触发的关键词在此轮数内跳过检索

# ========== 永续认知循环全局 ==========
_keyword_queue = deque(maxlen=100)           # 关键词消费队列（每轮取空处理）
_shallow_pool = deque(maxlen=20)             # 浅层意识池
_cognitive_running = False                   # 循环运行状态
_graceful_stop = False                       # 优雅停止标志（完成当前轮，不复搜）

# ========== 关键词抽取（jieba 搜索引擎模式）==========
_KEYWORD_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这", "那", "这个", "那个", "这样", "那样", "什么", "怎么", "为什么", "没有",
    "可以", "知道", "觉得", "然后", "一个", "一种", "有点", "有些", "就是", "不是",
    "不会", "不要", "都会", "真的", "但是", "因为", "所以", "如果", "虽然", "而且",
    "其实", "还是", "已经", "现在", "刚才", "之后", "以前", "时候", "一次", "一下",
    "哈哈", "哈哈哈", "嘿嘿", "嘻嘻", "啊啊", "嗯嗯", "哦哦", "好的", "知道", "好吗",
}

def extract_keywords_jieba(text: str, max_keywords: int = 8) -> list:
    """用 jieba 搜索引擎模式分词，过滤停用词/单字/纯标点，返回检索关键词。

    关键词来源：上一轮的回复（心理活动或发送消息）与接收到的消息。
    """
    if not text:
        return []
    result = []
    seen = set()
    for w in jieba.cut_for_search(str(text)):
        w = w.strip()
        if len(w) < 2 or w in _KEYWORD_STOPWORDS:
            continue
        if not re.search(r"[0-9a-zA-Z\u4e00-\u9fff]", w):
            continue
        if w not in seen:
            seen.add(w)
            result.append(w)
        if len(result) >= max_keywords:
            break
    return result

def inject_message_keywords(keywords: list):
    """由消息处理层调用，将消息 jieba 分词后的关键词加入消费队列"""
    if keywords:
        _keyword_queue.append(keywords)

# ========== 反刍抑制状态 ==========
_keyword_continuity: dict = {}                      # {关键词: 连续出现次数}
_inhibited_seeds: dict = {}                         # {种子节点ID: 剩余抑制轮数}
_inhibited_edges: dict = {}                         # {(src_id, tgt_id): 剩余抑制轮数}
_inhibited_keywords: dict = {}                      # {关键词: 剩余抑制轮数}

# 每轮扩散记录（注入抑制时记录当前轮使用的种子和边）
_current_round_seeds: list = []
_current_round_edges: list = []

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
    current_min = now.hour * 60 + now.minute
    start_min = SLEEP_START_HOUR * 60
    end_min = SLEEP_END_HOUR * 60

    # 距离最近一次睡眠开始的分种数（恰在睡眠开始时视为已入睡，不触发困倦）
    minutes_to_sleep = (start_min - current_min) % (24 * 60)
    if minutes_to_sleep == 0:
        minutes_to_sleep = 24 * 60

    # 距上次睡眠结束的分种数（醒来后经过多久）
    minutes_since_wake = (current_min - end_min) % (24 * 60)

    if minutes_to_sleep <= DROWSY_MARGIN:
        return "[现在] 我现在有点困，想睡觉了"
    if minutes_since_wake <= DROWSY_MARGIN:
        return "[现在] 我刚睡醒，还有点迷糊"
    return None

def retrieve_and_diffuse(keywords: list, max_memories: int = 10,
                         inhibited_seeds: set = None,
                         inhibited_edges: set = None) -> list:
    """
    关键词检索 + 受限BFS扩散，返回带时间标记的记忆片段列表。
    每个元素为 (real_timestamp, "[时间短语] 内容")。
    供 cognitive_loop 调用。
    inhibited_seeds: 被抑制的种子ID集合，检索后从候选种子中移除
    inhibited_edges: 被抑制的边集合 (src_id, tgt_id)，扩散时跳过
    """
    if not keywords:
        return []

    if inhibited_seeds is None:
        inhibited_seeds = set()
    if inhibited_edges is None:
        inhibited_edges = set()

    global _current_round_seeds, _current_round_edges

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

    # 应用种子抑制过滤
    seed_ids = [sid for sid in seed_ids if sid not in inhibited_seeds]

    # BFS扩散（同时记录本轮遍历的边）
    _current_round_edges = []
    activated_memories = []
    if seed_ids:
        activated = pathfind_activation(seed_ids, max_stamina=3, top_k=8,
                                        inhibited_edges=inhibited_edges,
                                        output_visited_edges=_current_round_edges)
        activated_memories = [(mem, score) for mem, score in activated]

    # 记录本轮实际使用的种子
    _current_round_seeds = list(seed_ids)

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
        timed_memories.append((real_ts, f"[{phrase}] {mem['content']}"))

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
    timestamps = []
    for mem in final_mem_objects:
        virtual_ts = mem.get("creation_time", 0)
        real_ts = clock.to_real_time(virtual_ts)
        phrase = get_relative_time_phrase(real_ts)
        timed_memories.append(f"[{phrase}] {mem['content']}")
        timestamps.append(real_ts)

    drowsy = _get_drowsy_memory()
    if drowsy:
        timed_memories.insert(0, drowsy)
        timestamps.insert(0, None)

    # 阶段D：LLM 拼接回复（传入关键词作为指引）
    result = verbalize(timed_memories, keywords, new_state, user_input, timestamps=timestamps)
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
      消费队列关键词 → 检索扩散 → 拼接层(生成内心独白+发言决策)
      → 存入记忆 → 回复jieba分词入队 → 检查新消息 → 循环

    关键词来源：上一轮的回复（心理活动或发送消息）与接收到的消息，
    均以 jieba 搜索引擎模式分词后进入消费队列，每轮取空队列处理全部关键词。

    在 NapCat 连接后启动，断开时 request_graceful_stop 触发：
      当前轮继续执行到回复输出，跳过复搜后退出。
    """
    global _keyword_queue, _shallow_pool, _cognitive_running, _graceful_stop
    global _keyword_continuity, _inhibited_seeds, _inhibited_edges, _inhibited_keywords

    from .llm_interface import add_to_history
    from .memory_engine import memories, access_memory

    # 重置停止标志和抑制状态，允许新会话启动
    _graceful_stop = False
    _cognitive_running = True
    _keyword_continuity.clear()
    _inhibited_seeds.clear()
    _inhibited_edges.clear()
    _inhibited_keywords.clear()
    _keyword_queue.clear()
    current_keywords = []
    prev_thought_text = None
    prev_should_speak = False

    append_log("="*40)
    append_log("[认知循环] 永续认知循环启动")
    append_log("="*40)

    while not _graceful_stop:
        try:
            loop = asyncio.get_event_loop()

            # ============================
            # Step 1: 消费队列中的全部关键词（取空即清空队列）
            # ============================
            new_kws_batch = []
            while _keyword_queue:
                kws = _keyword_queue.popleft()
                new_kws_batch.extend(kws)

            if new_kws_batch:
                current_keywords = list(dict.fromkeys(new_kws_batch))
                append_log(f"[认知循环] 消费队列关键词: {current_keywords}")

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
            # 阶段0：过滤被抑制的关键词
            # ============================
            if current_keywords and _inhibited_keywords:
                old_len = len(current_keywords)
                current_keywords = [kw for kw in current_keywords if kw not in _inhibited_keywords]
                if len(current_keywords) < old_len:
                    append_log(f"[反刍抑制] 过滤 {old_len - len(current_keywords)} 个被抑制关键词，剩余: {current_keywords}")

            # ============================
            # 阶段1：更新关键词计数器，检测反刍
            # ============================
            if current_keywords:
                # 添加上轮不存在的关键词
                for kw in current_keywords:
                    _keyword_continuity[kw] = _keyword_continuity.get(kw, 0) + 1
                # 删除本轮不存在的旧关键词
                for kw in list(_keyword_continuity):
                    if kw not in current_keywords:
                        del _keyword_continuity[kw]

                # 检测反刍
                is_ruminating = any(
                    count >= RUMINATION_THRESHOLD
                    for kw, count in _keyword_continuity.items()
                )
            else:
                is_ruminating = False

            # ============================
            # 阶段2：抑制触发（强制转向）
            # ============================
            if is_ruminating:
                triggered_kws = [kw for kw, count in _keyword_continuity.items()
                                 if count >= RUMINATION_THRESHOLD]
                for kw in triggered_kws:
                    append_log(f"[反刍抑制] 检测到关键词重复: '{kw}' 连续 {_keyword_continuity[kw]} 次")

                # 抑制本轮使用的种子和边
                seed_count = 0
                for sid in _current_round_seeds:
                    if sid not in _inhibited_seeds:
                        _inhibited_seeds[sid] = SEED_INHIBIT_ROUNDS
                        seed_count += 1
                edge_count = 0
                for edge in _current_round_edges:
                    if edge not in _inhibited_edges:
                        _inhibited_edges[edge] = EDGE_INHIBIT_ROUNDS
                        edge_count += 1

                # 抑制触发了反刍的关键词本身
                kw_inhibit_count = 0
                for kw in triggered_kws:
                    if kw not in _inhibited_keywords:
                        _inhibited_keywords[kw] = KEYWORD_INHIBIT_ROUNDS
                        kw_inhibit_count += 1

                append_log(f"[反刍抑制] 触发：抑制 {seed_count} 个种子节点（{SEED_INHIBIT_ROUNDS}轮），{edge_count} 条边（{EDGE_INHIBIT_ROUNDS}轮），{kw_inhibit_count} 个关键词（{KEYWORD_INHIBIT_ROUNDS}轮）")

                # 强制转向：从浅层意识池随机抽取新种子
                forced_seed_id = None
                if _shallow_pool:
                    forced_seed_id = random.choice(list(_shallow_pool))
                    forced_mem = memories.get(forced_seed_id)
                    if forced_mem:
                        current_keywords = [forced_mem["content"][:50]]
                        append_log(f"[反刍抑制] 强制转向：从浅层池抽取新种子 {forced_seed_id[:8]}...")
                    else:
                        forced_seed_id = None

                if not forced_seed_id:
                    # 浅层池不可用，清空关键词触发重选
                    current_keywords = []
                    append_log("[反刍抑制] 强制转向：浅层池为空，清空关键词")

                # 清空触发了反刍的关键词计数器
                for kw in triggered_kws:
                    if kw in _keyword_continuity:
                        del _keyword_continuity[kw]

            # ============================
            # Step 3: 检索与扩散（应用抑制过滤）
            # ============================
            if not current_keywords:
                append_log("[认知循环] 当前无关键词，跳过本轮")
                await asyncio.sleep(3)
                continue

            related_pairs = retrieve_and_diffuse(
                current_keywords, max_memories=10,
                inhibited_seeds=set(_inhibited_seeds.keys()),
                inhibited_edges=set(_inhibited_edges.keys())
            )
            if not related_pairs:
                append_log("[认知循环] 无相关记忆，跳过本轮")
                current_keywords = []
                await asyncio.sleep(3)
                continue

            related_memories = [text for _, text in related_pairs]
            related_ts = [ts for ts, _ in related_pairs]

            # ============================
            # Step 3b: 历史对话作为记忆并入（滑动窗口 4~6，保留最近 10 条取最后 4~6 条）
            # ============================
            from utils.message_history import get_all
            from utils.time_phrases import get_relative_time_phrase

            dialogue_count = random.randint(4, 6)
            recent_msgs = get_all()[-10:]
            selected_msgs = recent_msgs[-dialogue_count:]
            dialogue_memories = []
            dialogue_ts = []
            for msg in selected_msgs:
                if msg["sender"] == BOT_NAME:
                    d_content = f"我说：{msg['content']}"
                else:
                    d_content = f"{msg['sender']}说：{msg['content']}"
                d_msg_time = msg.get("time")
                if d_msg_time is None:
                    d_msg_time = clock.now()
                d_mem_id = semantic_dedup(d_content, now=d_msg_time) or create_memory(d_content)
                d_mem = memories.get(d_mem_id)
                if d_mem:
                    d_mem["creation_time"] = d_msg_time
                    d_real_ts = clock.to_real_time(d_msg_time)
                    d_phrase = get_relative_time_phrase(d_real_ts)
                    dialogue_memories.append(f"[{d_phrase}] {d_content}")
                    dialogue_ts.append(d_real_ts)

            if dialogue_memories:
                related_memories = related_memories + dialogue_memories
                related_ts = related_ts + dialogue_ts
                append_log(f"[认知循环] 历史对话并入 {len(dialogue_memories)} 条（窗口={dialogue_count}）: {dialogue_memories}")

            # ============================
            # Step 3c: 上一轮回复并入（无论是否发出消息），带时间标签
            # ============================
            prev_mem = None
            prev_ts = None
            if prev_thought_text:
                prev_real_ts = clock.to_real_time(clock.now())
                prev_phrase = get_relative_time_phrase(prev_real_ts)
                prev_mem = f"[{prev_phrase}] {'我说' if prev_should_speak else '我想'}：{prev_thought_text}"
                prev_ts = prev_real_ts
                related_memories.append(prev_mem)
                related_ts.append(prev_ts)
                append_log(f"[认知循环] 上一轮回复并入: {prev_mem}")

            # ============================
            # Step 3d: 困倦/刚醒记忆附加（即将睡觉 / 刚睡醒）
            # ============================
            drowsy_mem = None
            drowsy_ts = None
            drowsy = _get_drowsy_memory()
            if drowsy:
                drowsy_ts = clock.to_real_time(clock.now())
                drowsy_mem = drowsy
                related_memories.append(drowsy)
                related_ts.append(drowsy_ts)
                append_log(f"[认知循环] 附加困倦/刚醒记忆: {drowsy}")

            # ============================
            # Step 4: 拼接层处理
            # ============================
            state = get_state()

            pinned_memories = dialogue_memories + ([prev_mem] if prev_mem else []) + ([drowsy_mem] if drowsy_mem else [])
            pinned_ts = dialogue_ts + ([prev_ts] if prev_ts is not None else []) + ([drowsy_ts] if drowsy_ts is not None else [])

            result = await loop.run_in_executor(
                None, lambda: verbalize(
                    related_memories, current_keywords, state,
                    pinned=pinned_memories,
                    timestamps=related_ts,
                    pinned_timestamps=pinned_ts
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

            prev_thought_text = thought_text
            prev_should_speak = should_speak

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
                BUS.message.emit(BOT_NAME, thought_text, "QQ")

            # ============================
            # Step 7: 优雅停止检查（不复搜）
            # ============================
            if _graceful_stop:
                append_log("[认知循环] 收到停止信号，完成本轮回复，跳过复搜")
                break

            # ============================
            # Step 7b: 关键词队列 —— 将本轮回复 jieba 分词后入队（下一轮消费）
            # ============================
            reply_keywords = extract_keywords_jieba(thought_text)
            if reply_keywords:
                _keyword_queue.append(reply_keywords)
                append_log(f"[认知循环] 回复分词入队: {reply_keywords}")

            # 本轮队列关键词已消费完毕，清空本轮的 current_keywords
            current_keywords = []

            # ============================
            # Step 8: 休眠
            # ============================
            await asyncio.sleep(2)

            # ============================
            # 阶段5：抑制计数器衰减与清理
            # ============================
            expired_seeds = 0
            for sid in list(_inhibited_seeds):
                _inhibited_seeds[sid] -= 1
                if _inhibited_seeds[sid] <= 0:
                    del _inhibited_seeds[sid]
                    expired_seeds += 1
            expired_edges = 0
            for edge in list(_inhibited_edges):
                _inhibited_edges[edge] -= 1
                if _inhibited_edges[edge] <= 0:
                    del _inhibited_edges[edge]
                    expired_edges += 1
            expired_keywords = 0
            for kw in list(_inhibited_keywords):
                _inhibited_keywords[kw] -= 1
                if _inhibited_keywords[kw] <= 0:
                    del _inhibited_keywords[kw]
                    expired_keywords += 1
            if expired_seeds > 0 or expired_edges > 0 or expired_keywords > 0:
                append_log(f"[反刍抑制] 解除：{expired_seeds} 个种子节点，{expired_edges} 条边，{expired_keywords} 个关键词已恢复")

            await asyncio.sleep(1)

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