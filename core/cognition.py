# core/cognition.py
import re
import datetime
import time
import random
from collections import deque

import jieba

from .memory_engine import (
    create_memory, retrieve_similar, add_link, pathfind_activation, retrieve_by_exact_keywords, _load_memory_from_db, 
    DEFAULT_HALF_LIFE
)
from .llm_interface import decompose_input, verbalize
from .virtual_clock import clock
from .biorhythm import BIORHYTHM
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

current_speaker: str = None

# ========== 反刍抑制常量 ==========
RUMINATION_THRESHOLD = 2                            # 关键词连续出现 >= 此值，触发抑制
SEED_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3      # 种子抑制轮数
EDGE_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3      # 路径抑制轮数
KEYWORD_INHIBIT_ROUNDS = RUMINATION_THRESHOLD * 3   # 关键词抑制轮数，触发的关键词在此轮数内跳过检索

# ========== 永续认知循环全局 ==========
# 关键词消费队列：元素为 (keywords, group_id)。携带来源群是为了让回复
# 能路由回触发它的那个群（F09）——此前是纯关键词列表，无从判断来源。
_keyword_queue = deque(maxlen=100)
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

def inject_message_keywords(keywords: list, group_id: str = None):
    """由消息处理层调用，将消息 jieba 分词后的关键词加入消费队列。

    group_id 用于让后续回复路由回来源群（F09）。不传时从当前上下文取。
    """
    if not keywords:
        return
    if group_id is None:
        try:
            from utils.dialogue_state import current_group
            group_id = current_group()
        except Exception:
            group_id = None
    _keyword_queue.append((keywords, group_id))


# ========== 时间指代通道 ==========
# 理解层给出的时间枚举（latest/recent/earlier/any），供概念路做时间窗筛选。
# 只存最近一次：时间指代描述的是"当前这句话"的意图，跨轮沿用会失真。
_time_intent: str = "none"


def inject_time_intent(intent: str):
    """由消息处理层调用，记录本轮消息的时间指代意图。"""
    global _time_intent
    if intent:
        _time_intent = str(intent).strip().lower()


def consume_time_intent() -> str:
    """取出并重置时间指代（一次性，避免影响后续轮次）。"""
    global _time_intent
    intent = _time_intent
    _time_intent = "none"
    return intent

# ========== 反刍抑制状态 ==========
_keyword_continuity: dict = {}                      # {关键词: 连续出现次数}
_inhibited_seeds: dict = {}                         # {种子节点ID: 剩余抑制轮数}
_inhibited_edges: dict = {}                         # {(src_id, tgt_id): 剩余抑制轮数}
_inhibited_keywords: dict = {}                      # {关键词: 剩余抑制轮数}

# ========== 记事本回看通道（一次性）==========
# 读文件或写文件时，把文件内容留给下一轮作为特供记忆（pinned）呈现。
# 只在这一轮有效，用过即清；内容不进记忆库（不入库）。
_pending_note_memory: str = None

# 每轮扩散记录（注入抑制时记录当前轮使用的种子和边）
_current_round_seeds: list = []
_current_round_edges: list = []

# ========== 睡眠配置 ==========
# 睡眠时机不再由固定钟点决定，改由 core.biorhythm 的睡眠压力涌现。
# 该常量保留仅为兼容旧引用，不再参与睡眠判定。
DROWSY_MARGIN = 15              # 保留字段：兼容旧引用


def _get_drowsy_memory() -> str:
    """
    返回应注入的第一人称身体感受（困倦/疲惫/精神），由生物钟精力分档生成。
    这是"内心感受"通道：让辉夜自己感到累，而不只是内部数值。
    """
    return BIORHYTHM.feeling_text()


def _strip_trailing_period(text: str) -> str:
    """去掉句末的一个句号。

    只处理末尾这一个"。"，句中的标点与其他收尾标点（？！）一律保留；
    放在入库与对话历史之前，保证记忆、历史、发送三者用的是同一份文本。
    """
    if text.endswith("。"):
        return text[:-1]
    return text

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

    # 精力调制思考深度：精力越低，检索面越窄、扩散越浅（想得浅一点）
    depth = BIORHYTHM.depth_factor()
    retr_k = max(2, round(5 * depth))
    max_mem = max(4, round(max_memories * depth))
    bfs_stamina = max(1.5, 3.0 * depth)
    bfs_topk = max(4, round(8 * depth))

    # 语义检索（faiss）
    seed_ids = []
    faiss_results = []
    for kw in keywords:
        similar = retrieve_similar(kw, k=retr_k)
        for score, mem in similar:
            if mem["id"] not in seed_ids:
                seed_ids.append(mem["id"])
            faiss_results.append((score, mem))

    # 精确关键词检索（词网）
    exact_results = retrieve_by_exact_keywords(keywords, k=retr_k)
    for score, mem in exact_results:
        if mem["id"] not in seed_ids:
            seed_ids.append(mem["id"])

    # 应用种子抑制过滤
    seed_ids = [sid for sid in seed_ids if sid not in inhibited_seeds]

    # BFS扩散（同时记录本轮遍历的边）
    _current_round_edges = []
    activated_memories = []
    if seed_ids:
        activated = pathfind_activation(seed_ids, max_stamina=bfs_stamina, top_k=bfs_topk,
                                        inhibited_edges=inhibited_edges,
                                        output_visited_edges=_current_round_edges)
        activated_memories = [(mem, score) for mem, score in activated]

    # 记录本轮实际使用的种子
    _current_round_seeds = list(seed_ids)

    # 合并扩散结果 + 检索结果
    # 抑制过滤必须在这最后一关也生效：此前 inhibited_seeds 只剔除 BFS 种子，
    # 被抑制的记忆仍能经 faiss / 精确关键词两路回到结果里（F25）。
    combined = {}
    for mem, score in activated_memories:
        mem_id = mem["id"]
        if mem_id in inhibited_seeds:
            continue
        content = mem["content"]
        if mem_id not in combined or score > combined[mem_id][0]:
            combined[mem_id] = (score, content, mem)

    for score, mem in faiss_results + exact_results:
        mem_id = mem["id"]
        if mem_id in inhibited_seeds:
            continue
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
        if len(final_mem_objects) >= max_mem:
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


def retrieve_by_concept(keywords: list, time_intent: str = "none") -> tuple:
    """概念定向检索：关键词命中概念 → 取若干事件段 → 段内按时间倒序直读。

    与向量检索的区别在于全程不做相似度排序：概念已经把话题定死，
    段内记忆又是同一次交互里的连续内容，直接按时间读即可。

    返回 (timed_memories, concept_name, degraded)：
      timed_memories 形如 [(real_ts, "[时间短语] 内容"), ...]
      concept_name 为命中的概念名；未命中时为 None
      degraded 表示时间窗内无记录、已降级为"最近若干段"
    """
    if not keywords:
        return [], None, False

    from .concept_store import match_concept, select_episodes, fetch_members
    from utils.time_phrases import get_relative_time_phrase

    # 长关键词更具体，优先用它匹配（如"物理作业"优于"作业"）
    candidates = sorted({str(k) for k in keywords if k}, key=len, reverse=True)
    cid = cname = None
    for kw in candidates:
        cid, cname, how = match_concept(kw)
        if cid:
            append_log(f"[概念路] 关键词「{kw}」命中概念「{cname}」（{how}）")
            break
    if not cid:
        return [], None, False

    try:
        episodes, degraded = select_episodes(cid, time_intent)
    except Exception as e:
        append_log(f"[概念路] 事件段选择失败，跳过: {e}")
        return [], None, False
    if not episodes:
        append_log(f"[概念路] 概念「{cname}」下没有事件段，跳过")
        return [], None, False
    if degraded:
        append_log(f"[概念路] 时间窗内无记录（意图={time_intent}），降级为最近 {len(episodes)} 段")

    try:
        members = fetch_members(episodes)
    except Exception as e:
        append_log(f"[概念路] 成员展开失败，跳过: {e}")
        return [], None, False

    timed = []
    for mem, real_ts in members:
        phrase = get_relative_time_phrase(real_ts)
        timed.append((real_ts, f"[{phrase}] {mem['content']}"))
    append_log(f"[概念路] 概念「{cname}」带回 {len(timed)} 条（{len(episodes)} 段，意图={time_intent}）")
    return timed, cname, degraded


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

def generate_response(user_input: str, current_speaker: str = None) -> tuple:
    """
    核心回复逻辑：
    1、预处理输入
    2、LLM拆解
    3、语义查找
    4、体力行走扩散
    5、LLM拼接

    返回 (reply, user_input, new_mem_ids, should_speak)：
      reply        —— 生成的文本（可能是内心独白）
      user_input   —— 原始输入（保持既有调用方兼容）
      new_mem_ids  —— **本轮**新建的记忆 ID 列表（按轮次返回，不依赖全局累加器）
      should_speak —— 本轮是否应把 reply 公开说出口；False 表示这是内心话，
                      调用方不应显示/发送，记忆里也已记为"我想"而非"我说"
    """
    # 预处理
    #clear_log()     # 可选择不清空，不清空的话直接把这行注释掉
    user_input = user_input.replace("*","")     # 对输入进行预处理，防止污染，必要时可以注释这一行
    user_input = mask_brackets(user_input)      # 对输入进行预处理，防止污染，必要时可以注释这一行

    # 阶段A：LLM 拆解输入，拿到关键词
    # （概念与时间指代供 QQ 链路的概念层使用，此路径不消费）
    mem_fragments, mode, new_state, keywords, _concept, _time_intent = decompose_input(user_input)

    # 更新对话状态
    if new_state:
        set_state(new_state)
        save_state()

    # 存储记忆（按模式设置半衰期），不去重
    half_life = MODE_HALF_LIFE.get(mode, DEFAULT_HALF_LIFE)
    user_mem_ids = []
    for frag in mem_fragments:
        mid = create_memory(frag, half_life=half_life)
        user_mem_ids.append(mid)

    # 阶段B：关键词驱动的语义检索（faiss）
    from core.memory_engine import memories
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

    # 阶段C：体力行走式扩散（从种子出发，沿有向图走）
    activated_memories = []  # 扩散激活的记忆
    if seed_ids:
        activated = pathfind_activation(seed_ids, max_stamina=3, top_k=8)
        activated_memories = [(mem["content"], score, mem["id"]) for mem, score in activated]

    # 合并：扩散结果 + 两路检索结果
    #
    # 注意不要像此前那样把 all_retrieved 清空后只重跑一次 faiss 检索——
    # 那会丢掉精确关键词命中的结果，使"只被精确命中、BFS 又无邻居"的孤立记忆
    # 完全进不了生成上下文，同时还白跑一遍 embedding（F17）。
    combined = {}
    for content, score, mem_id in activated_memories:
        mem = memories.get(mem_id) or _load_memory_from_db(mem_id)
        if mem and (mem_id not in combined or score > combined[mem_id][0]):
            combined[mem_id] = (score, content, mem)

    for score, mem in faiss_results + list(exact_results):
        mem_id = mem["id"]
        if mem_id not in combined or score > combined[mem_id][0]:
            combined[mem_id] = (score, mem["content"], mem)

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
    should_speak = False
    if isinstance(result, dict):
        reply = result.get("text", "")
        should_speak = bool(result.get("say", False))
    elif result:
        reply = str(result)
        should_speak = True

    if reply:
        reply = reply.strip("“")
        reply = reply.strip("”")
        # 去掉句末句号（入库之前，只去末尾这一个）
        reply = _strip_trailing_period(reply)

        # 尊重 say 决策：此前只读 text、忽略 say，把"内心话"也当成公开回复
        # 返回并记成"我说"，导致 CLI 会把纯内心独白显示出来（F10）。
        if should_speak:
            if current_speaker:
                reply_memory = f"我告诉{current_speaker}，{reply}"
            else:
                reply_memory = f"我说，{reply}"
        else:
            # 未发言：记为"我想"，调用方据此决定不公开显示
            reply_memory = f"我想：{reply}"

        bot_mem_id = create_memory(reply_memory)
        user_mem_ids.append(bot_mem_id)
        if len(user_mem_ids) > 1:
            add_link(user_mem_ids[0], bot_mem_id, 0.8, "causal")
    return reply, user_input, user_mem_ids, should_speak

def reset_dialogue():
    reset_state()

# ====== 附加功能：QQ接口 ======

import asyncio
from typing import Tuple, List

# process_dialogue 已移除：它唯一的调用方是控制面板的"对话测试"页，
# 该页已随 F10 删除。CLI（main.py）直接调用 generate_response。


def request_graceful_stop():
    """
    请求认知循环在完成当前轮次后优雅停止。
    当前轮会正常输出回复，但跳过复搜步骤，然后退出。
    """
    global _graceful_stop, _cognitive_running
    _graceful_stop = True
    _cognitive_running = False  # 阻止在空闲时启动新的一轮


def _select_dialogue_window(history: list, dialogue_count: int) -> list:
    """按"最近 N 条他人消息"定位起点，返回该起点之后的完整消息段。

    返回的段落里同时包含他人与自己的消息，从而保住一问一答的完整脉络；
    只按他人消息定位，是为了让"窗口长度"由外部输入量决定，
    不会被自己频繁插话稀释成一小截。
    没有他人消息时返回空列表（此时无从定位对话起点）。
    """
    other_idx = [i for i, m in enumerate(history) if m.get("sender") != BOT_NAME]
    if not other_idx:
        return []
    # 他人消息足够时取倒数第 dialogue_count 条的起始位置；
    # 不足时退回到第一条他人消息，保证窗口从对话真正开始处起算。
    anchor = other_idx[-dialogue_count] if len(other_idx) >= dialogue_count else other_idx[0]
    return history[anchor:]


def _resolve_target_group(fallback: str, reply_group: str = None) -> str:
    """解析本轮发送的目标群（F09 + F28）。

    优先级：
      1. 有待回复的消息时，回它所在的群（修复"群 B 的消息引出一句发到群 A 的回复"）。
         来源群有两个渠道，取自关键词队列的 round_group，或 qq_bot 登记的待回复群；
      2. 当前配置的主动发言群 —— 每轮重新读取，支持热更新（F28）；
      3. 启动时捕获的 fallback。
    """
    if not reply_group:
        try:
            import qq_bot
            reply_group = qq_bot.take_pending_reply_group()
        except Exception:
            reply_group = None
    if reply_group:
        return str(reply_group)
    try:
        import qq_bot
        current = qq_bot.get_active_group_id()
        if current:
            return current
    except Exception:
        pass
    return fallback


def _consume_note_memory():
    """取出并清空记事本回看通道，返回 (内容, 真实时间戳)；无内容时返回 (None, None)。

    一次性：取走即清，保证文件内容只在紧接着的下一轮出现一次，且不进记忆库。
    """
    global _pending_note_memory
    if not _pending_note_memory:
        return None, None
    mem = _pending_note_memory
    _pending_note_memory = None
    return mem, clock.to_real_time(clock.now())


async def _run_action_decision(loop, thought_text, should_speak, talk_sent,
                               keywords, media_send_func, target_group_id):
    """执行一次动作抉择并落实动作。返回描述字符串（无动作时返回 None）。

    抉择本身是同步 LLM 调用，放进线程池执行；语义上仍属于本轮的最后一步。
    """
    from .action_layer import decide_action, action_enabled
    from .llm_interface import add_to_history
    from utils.event_bus import BUS

    global _pending_note_memory

    # 已收到停止信号：不再发起新的 LLM 抉择，让本轮尽快收尾。
    # 抉择一轮最多要连发 5 次 API 请求（翻页），会把关停拖住。
    if _graceful_stop:
        return None

    if not action_enabled():
        return None

    # 本轮手里有没有可用的东西：收藏、刚收到的媒体，或记事本功能
    # 记事本允许从零开始写，故开启时即便收藏为空也值得询问一次
    from . import asset_library as ASSETS
    from .action_layer import note_enabled
    if not (ASSETS.available_kinds() or ASSETS.list_pending() or note_enabled()):
        return None

    # 只在"刚说过话"或"有新消息进来"时给动作机会，避免纯内心轮次也冲动
    if not (talk_sent or keywords):
        return None

    result = await loop.run_in_executor(
        None, lambda: decide_action(
            thought_text=thought_text,
            should_speak=should_speak,
            said_text=thought_text if talk_sent else "",
            keywords=keywords,
            context_hint="刚刚有人在群里说话" if keywords else "",
        )
    )
    if not result:
        return None
    action = result.get("action")
    if not action or action == "none":
        return None
    detail = result.get("detail") or ""

    # -------- 发送类：把描述直接补进历史记忆 --------
    if action in ("image", "sticker"):
        entry = result.get("entry") or {}
        path = entry.get("path")
        kind = entry.get("kind") or ASSETS.IMAGE
        tag = "图片" if kind == ASSETS.IMAGE else "表情包"
        if not path:
            return None
        # 目标群同样动态解析（F28）
        target_group = _resolve_target_group(target_group_id)
        if media_send_func is None or not target_group:
            append_log("[动作抉择] 没有可用的媒体发送通道，放弃")
            return None
        # 按类型走对应发送方式：表情包需带 sub_type，否则 QQ 会当普通图片显示
        ok = await media_send_func(
            target_group, path, as_sticker=(kind == ASSETS.STICKER)
        )
        if not ok:
            append_log("[动作抉择] 媒体发送失败，未记入历史")
            return None
        ASSETS.mark_sent(kind, detail)
        measure = "张" if kind == ASSETS.IMAGE else "个"
        create_memory(f"我发了{measure}{tag}，内容是：{detail}")
        add_to_history(None, None, f"（我发了{measure}{tag}，内容是：{detail}）")
        BUS.message.emit(BOT_NAME, f"[{tag}] {detail}", "QQ")
        return f"发送{tag}: {detail}"

    # -------- 收藏类 --------
    if action in ("save_image", "save_sticker"):
        if not result.get("added"):
            return None
        # 以实际入库的类型为准（暂存时可能按真实格式重新归类）
        saved = result.get("saved") or {}
        kind = saved.get("kind") or (ASSETS.IMAGE if action == "save_image" else ASSETS.STICKER)
        tag = "图片" if kind == ASSETS.IMAGE else "表情包"
        measure = "张" if kind == ASSETS.IMAGE else "个"
        create_memory(f"我收藏了{measure}{tag}，内容是：{detail}")
        add_to_history(None, None, f"（我收藏了{measure}{tag}，内容是：{detail}）")
        BUS.message.emit(BOT_NAME, result.get("reply") or f"（收下了这个{tag}）", "QQ")
        return f"收藏{tag}: {detail}"

    # -------- 记事本：写入/修改/翻开，内容留给下一轮特供记忆（不入库）--------
    if action in ("write_txt", "edit_txt", "read_txt"):
        name = result.get("note_name") or ""
        text = result.get("note_text") or ""
        # F22：行为记录与"全文回看"解耦。
        # 此前 `if not text: return None` 会在删除最后一行（文件变空、read_note
        # 返回 None）时提前退出，文件已被修改却没有留下任何行为记忆——
        # 违背"改动需留痕"。现在空文件只表示"没有可回看的内容"，
        # 行为本身照常记录；只有 read_txt 在无内容时才无事可做。
        if text:
            # 形如：[现在]我打开了我写的“xxx”文件，内容是：xxx
            _pending_note_memory = f"[现在]我打开了我写的“{name}”文件，内容是：{text}"
        elif action == "read_txt":
            return None

        if action == "write_txt":
            # 写入这个行为本身值得记住，但只记"写过"，不把文件全文灌进记忆库
            written = result.get("written") or ""
            create_memory(f"我在记事本「{name}」里写下：{written}")
            add_to_history(None, None, f"（我在记事本「{name}」里写下：{written}）")
            BUS.message.emit(BOT_NAME, f"[笔记] {written}", "QQ")
            return f"写笔记: {name}"

        if action == "edit_txt":
            # 修改既有条目：记下"改了什么"，便于日后追溯（伦理上改动需留痕）
            old_text = result.get("old_text") or ""
            new_text = result.get("new_text") or ""
            if result.get("deleted"):
                create_memory(f"我把记事本「{name}」里的「{old_text}」划掉了")
                add_to_history(None, None, f"（我把记事本「{name}」里的「{old_text}」划掉了）")
                BUS.message.emit(BOT_NAME, f"[笔记] 划掉了：{old_text}", "QQ")
                return f"改笔记（删除）: {name}"
            create_memory(f"我把记事本「{name}」里的「{old_text}」改成了「{new_text}」")
            add_to_history(None, None, f"（我把记事本「{name}」里的「{old_text}」改成了「{new_text}」）")
            BUS.message.emit(BOT_NAME, f"[笔记] {new_text}", "QQ")
            return f"改笔记: {name}"

        append_log(f"[认知循环] 记事本「{name}」内容已载入下一轮特供记忆")
        return f"翻开笔记: {name}"

    return None


async def cognitive_loop(send_func=None, target_group_id: str = None, media_send_func=None):
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
    global _pending_note_memory

    from .llm_interface import add_to_history
    from .memory_engine import memories, access_memory

    # 重置停止标志和抑制状态，允许新会话启动
    _graceful_stop = False
    _cognitive_running = True
    _pending_note_memory = None
    _keyword_continuity.clear()
    _inhibited_seeds.clear()
    _inhibited_edges.clear()
    _inhibited_keywords.clear()
    _keyword_queue.clear()
    current_keywords = []
    prev_thought_text = None
    prev_should_speak = False
    last_eviction_time = time.time()

    append_log("="*40)
    append_log("[认知循环] 永续认知循环启动")
    append_log("="*40)

    while not _graceful_stop:
        # 睡眠暂停：由生物钟的睡眠压力涌现判定，而非固定钟点。
        # 入睡后当前轮会自然跑完，从下一轮开始挂起，直到精力恢复自然醒来。
        BIORHYTHM.tick()
        if BIORHYTHM.is_asleep():
            append_log("[认知循环] 生物钟判定已入睡，认知循环挂起")
            while not _graceful_stop and BIORHYTHM.is_asleep():
                await asyncio.sleep(5)
                BIORHYTHM.tick()
            append_log("[认知循环] 自然醒来（或被唤醒），认知循环恢复")
            continue

        try:
            loop = asyncio.get_event_loop()

            # ============================
            # Step 1: 消费队列中的全部关键词（取空即清空队列）
            # 队列元素为 (keywords, group_id)，同时记下本轮的来源群，
            # 供发送时路由（F09：回复回来源群而非固定主动群）。
            # ============================
            new_kws_batch = []
            round_group = None
            while _keyword_queue:
                kws, src_group = _keyword_queue.popleft()
                new_kws_batch.extend(kws)
                if src_group and not round_group:
                    round_group = src_group

            if new_kws_batch:
                current_keywords = list(dict.fromkeys(new_kws_batch))
                append_log(f"[认知循环] 消费队列关键词: {current_keywords}"
                           f"{f'（来源群 {round_group}）' if round_group else ''}")

            # ============================
            # Step 2: 若无关键词，从记忆库取种子（线程安全）
            # ============================
            if not current_keywords:
                from .memory_engine import get_random_memory_id, get_memory_content
                seed_text = None
                if _shallow_pool:
                    mem_id = random.choice(list(_shallow_pool))
                    content = get_memory_content(mem_id)
                    if content:
                        seed_text = content[:50]
                if not seed_text:
                    mem_id = get_random_memory_id()
                    if mem_id:
                        content = get_memory_content(mem_id)
                        if content:
                            seed_text = content[:50]
                            access_memory(mem_id)

                if seed_text:
                    current_keywords = [seed_text]
                    append_log(f"[认知循环] 无新消息，随机种子: {seed_text[:30]}...")
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
                    forced_content = get_memory_content(forced_seed_id)
                    if forced_content:
                        current_keywords = [forced_content[:50]]
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
            # ============================
            # Step 3: 概念定向检索（优先）
            # 关键词命中概念时，走"概念 → 事件段 → 段内直读"，不做相似度排序。
            # 这批记忆随后作为 pinned 进入提示词，不参与评分也不被关键词过滤丢弃。
            #
            # F08：概念匹配与向量检索内部都会发同步 HTTP（ollama 嵌入），
            # 放进线程池执行，避免卡住事件循环。
            # ============================
            time_intent_now = consume_time_intent()
            concept_memories, concept_name, concept_degraded = await loop.run_in_executor(
                None, lambda: retrieve_by_concept(current_keywords, time_intent_now)
            )
            concept_texts = [text for _, text in concept_memories]
            concept_ts = [ts for ts, _ in concept_memories]

            # ============================
            # Step 3': 向量检索与扩散（概念未命中时为主，命中时保留少量做跨概念联想）
            # ============================
            related_pairs = await loop.run_in_executor(
                None, lambda: retrieve_and_diffuse(
                    current_keywords, max_memories=3 if concept_memories else 10,
                    inhibited_seeds=set(_inhibited_seeds.keys()),
                    inhibited_edges=set(_inhibited_edges.keys())
                )
            )
            if not related_pairs and not concept_memories:
                append_log("[认知循环] 无相关记忆，跳过本轮")
                current_keywords = []
                await asyncio.sleep(3)
                continue

            related_memories = [text for _, text in related_pairs]
            related_ts = [ts for ts, _ in related_pairs]

            # ============================
            # Step 3b: 历史对话作为记忆并入
            # 先定位最近 4~6 条他人消息在完整历史中的起点，再取该起点之后的全部消息
            # （包含自己的），这样窗口内是一段完整的对话往来，而不是只有对方的单方发言。
            # ============================
            from utils.message_history import get_all, quote_suffix
            from utils.time_phrases import get_relative_time_phrase

            dialogue_count = random.randint(4, 6)
            selected_msgs = _select_dialogue_window(get_all(), dialogue_count)

            dialogue_memories = []
            dialogue_ts = []
            for msg in selected_msgs:
                # 引用后缀还原"这句话在回应什么"，短期上下文才连得上
                suffix = quote_suffix(msg)
                if msg["sender"] == BOT_NAME:
                    d_content = f"我说：{msg['content']}{suffix}"
                else:
                    d_content = f"{msg['sender']}说：{msg['content']}{suffix}"
                d_msg_time = msg.get("time")
                if d_msg_time is None:
                    d_msg_time = clock.now()
                d_real_ts = clock.to_real_time(d_msg_time)
                d_phrase = get_relative_time_phrase(d_real_ts)
                dialogue_memories.append(f"[{d_phrase}] {d_content}")
                dialogue_ts.append(d_real_ts)

            related_memories = related_memories + dialogue_memories
            related_ts = related_ts + dialogue_ts
            append_log(
                f"[认知循环] 历史对话并入 {len(dialogue_memories)} 条"
                f"（按最近 {dialogue_count} 条他人消息定位，含自己的消息）: {dialogue_memories}"
            )

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
            # Step 3e: 记事本回看附加（一次性）
            # 上一轮读过或写过文件时，把内容作为特供记忆呈现给这一轮；用过即清，不入库。
            # ============================
            note_mem, note_ts = _consume_note_memory()
            if note_mem:
                related_memories.append(note_mem)
                related_ts.append(note_ts)
                append_log(f"[认知循环] 记事本内容并入本轮特供记忆: {note_mem[:60]}...")

            # ============================
            # Step 4: 拼接层处理
            # ============================
            state = get_state()

            pinned_memories = (
                dialogue_memories
                + ([prev_mem] if prev_mem else [])
                + ([drowsy_mem] if drowsy_mem else [])
                + ([note_mem] if note_mem else [])
                # 概念路带回的记忆作为特供：不参与评分、不被关键词过滤丢弃
                + concept_texts
            )
            pinned_ts = (
                dialogue_ts
                + ([prev_ts] if prev_ts is not None else [])
                + ([drowsy_ts] if drowsy_ts is not None else [])
                + ([note_ts] if note_ts is not None else [])
                + concept_ts
            )

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
                #await asyncio.sleep(3)
                continue

            thought_text = result.get("text", "").strip()
            should_speak = result.get("say", False)

            # 去掉句末句号（在做入库与对话历史之前，只去末尾这一个）
            thought_text = _strip_trailing_period(thought_text)

            if not thought_text:
                append_log("[认知循环] verbalize 返回空，跳过")
                #await asyncio.sleep(3)
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

            # F08：写记忆含同步嵌入请求，放线程池避免阻塞事件循环
            mem_id = await loop.run_in_executor(None, create_memory, memory_text)
            _shallow_pool.append(mem_id)

            append_log(f"[认知循环] {'【发言】' if should_speak else '【内心】'}: {thought_text}")

            # ============================
            # Step 6: 发送消息（若应发言）
            # 先发、再按**实际送达结果**决定怎么记历史：
            #   送达成功 → 记为"我说"，下一轮正常并入；
            #   未送达   → 仍记入历史但追加"（消息发送失败）"后缀，
            #              下一轮读到的是带标记的文本，不会被误当成"已经说出口"。
            # 此前不看返回值就无条件 talk_sent=True 并记历史，
            # 发送失败时会产生虚假的"说过"记忆（F20）。
            # ============================
            talk_sent = False
            # F09：有待回复的来源群时优先回它；F28：否则每轮重读配置（热更新）。
            # 从关键词队列取到的 round_group 是本轮关键词的来源群。
            target_group = _resolve_target_group(target_group_id, reply_group=round_group)
            if should_speak and thought_text and send_func and target_group:
                delivered = await send_func(target_group, thought_text)
                talk_sent = delivered is not False     # 兼容未返回值的旧签名
                if delivered is False:
                    append_log(f"[认知循环] 消息发送失败，历史标记为未送达: {thought_text[:30]}")
                seq = add_to_history(None, None, thought_text)
                if delivered is False:
                    try:
                        from utils.message_history import mark_delivery_failed
                        mark_delivery_failed(seq)
                    except Exception as e:
                        append_log(f"[认知循环] 标记发送失败时出错: {e}")
                from utils.event_bus import BUS
                BUS.message.emit(BOT_NAME, thought_text, "QQ")

            # ============================
            # Step 6b: 动作抉择（本能冲动，本轮最后一步）
            # 本轮必须等抉择完成才继续；实际执行在线程池中，
            # 以免阻塞事件循环导致 NapCat 收消息与生物钟 tick 停摆。
            # ============================
            try:
                acted = await _run_action_decision(
                    loop, thought_text, should_speak, talk_sent, current_keywords,
                    media_send_func, target_group_id
                )
                if acted:
                    append_log(f"[认知循环] 动作抉择已执行: {acted}")
            except Exception as e:
                append_log(f"[认知循环] 动作抉择异常: {e}")

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
                # 保持来源群，使后续轮次的回复仍能回到同一群
                _keyword_queue.append((reply_keywords, round_group))
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

            # ============================
            # 阶段6：定时冷热数据下沉（每10分钟）
            # ============================
            if time.time() - last_eviction_time >= 600:
                last_eviction_time = time.time()
                try:
                    from .memory_engine import periodic_cold_eviction
                    periodic_cold_eviction()
                except Exception as e:
                    append_log(f"[认知循环] 定时下沉异常: {e}")

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