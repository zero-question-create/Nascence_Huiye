# core/cognition.py
# ========================================================================
# 本地认知循环（平台无关）。
#
# 这里只做本地计算：关键词信箱、概念/向量检索、图扩散、反刍抑制、
# 短期想法窗口、软件上下文合并、记忆写入。它不知道 QQ/微信/网页的存在。
#
# 每一轮的“行为思考”通过 BehaviorEngine 交给当前插件配置的 LLM；
# 插件负责软件读写（打开/关闭、对话内容）、行为通道与 LLM 连接信息，
# 不得向行为提示词注入平台元数据（人格涌现约束，见 core/behavior.py）。
# ========================================================================

import inspect
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
from .behavior import BehaviorEngine
from .short_term import SHORT_TERM
from .apps import APP_CONTEXT
from .app_router import APP_ROUTER
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
_keyword_queue = deque(maxlen=100)           # 关键词消费队列（每轮取空处理）
_shallow_pool = deque(maxlen=20)             # 浅层意识池
_cognitive_running = False                   # 循环运行状态
_graceful_stop = False                       # 优雅停止标志（完成当前轮，不复搜）


class KeywordMailbox:
    """关键词信箱：本轮在用的关键词 + 待并入的关键词。

    旧实现每轮用新批次**替换** current_keywords，导致上一轮还在进行的
    话题被瞬间丢掉。这里改为“追加合并”：新消息、上一轮回复、媒体描述
    依次入 pending，每轮开始时并入 active 并去重、截断。

    来源标记只用于日志与排障，不进入提示词。
    """

    def __init__(self, max_active: int = 12):
        self.max_active = max(1, int(max_active))
        self._pending = []          # [(source, keyword)]
        self._active = []           # [keyword]

    def add(self, keywords, source: str = "msg"):
        for kw in keywords or []:
            text = str(kw or "").strip()
            if text:
                self._pending.append((source, text))

    def merge(self) -> tuple:
        """把 pending 并入 active，返回 (本轮关键词, 来源摘要)。"""
        if self._pending:
            # 保留 active 里仍在队列的，优先本轮新增
            merged = []
            seen = set()
            for _source, kw in self._pending:
                if kw not in seen:
                    seen.add(kw)
                    merged.append(kw)
            for kw in self._active:
                if kw not in seen:
                    seen.add(kw)
                    merged.append(kw)
            self._active = merged[:self.max_active]
            sources = {}
            for source, _kw in self._pending:
                sources[source] = sources.get(source, 0) + 1
            self._pending = []
        else:
            sources = {}
        return list(self._active), sources

    def retain(self, keywords):
        """用实际参与检索的关键词回写 active（例如反刍抑制过滤之后）。"""
        self._active = [str(k) for k in (keywords or []) if str(k).strip()][:self.max_active]

    def clear(self):
        self._pending = []
        self._active = []

    def __len__(self):
        return len(self._active)

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


# ========== 关键词信箱（模块级单例）==========
# 认知循环每轮从这里取合并后的关键词；消息层/媒体层/上一轮回复都往这里追加。
KEYWORDS = KeywordMailbox(max_active=12)


def inject_keywords(keywords: list, source: str = "msg"):
    """把关键词追加进信箱（不立即消费），供下一轮合并使用。"""
    KEYWORDS.add(keywords, source)


def keyword_snapshot() -> tuple:
    """取出本轮应使用的关键词（pending 与 active 合并）。"""
    return KEYWORDS.merge()


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


# ========== 当前输入通道 ==========
# 消息层把这一轮的整句输入放进来，供概念路做整句向量召回。
# 只存最近一次：整句描述的是"当前这句话"，跨轮沿用会把旧问题当成新问题。
_pending_user_input: str = ""


def inject_user_input(text: str):
    """由消息处理层调用，记录本轮用户输入的整句原文。"""
    global _pending_user_input
    if text:
        _pending_user_input = str(text).strip()


def consume_user_input() -> str:
    """取出并重置本轮整句输入（一次性）。"""
    global _pending_user_input
    text = _pending_user_input
    _pending_user_input = ""
    return text

# ========== 反刍抑制状态 ==========
_keyword_continuity: dict = {}                      # {关键词: 连续出现次数}
_inhibited_seeds: dict = {}                         # {种子节点ID: 剩余抑制轮数}
_inhibited_edges: dict = {}                         # {(src_id, tgt_id): 剩余抑制轮数}
_inhibited_keywords: dict = {}                      # {关键词: 剩余抑制轮数}


def _decay_inhibition() -> None:
    """推进一轮抑制计时，把到期的种子/边/关键词放出来。

    必须每轮调用一次，包括“无相关记忆，跳过本轮”这类提前 continue 的轮次。
    原先只在产出回复的轮末衰减：关键词被抑制后检索必然为空、那一轮直接跳过，
    计数永远等不到归零，信箱又把这些词原样回灌，认知循环就卡在空转里。
    """
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
    返回应注入的第一人称身体感受（睡前/刚醒），由生物钟状态生成。
    这是"内心感受"通道：让辉夜自己感到累，而不只是内部数值。
    只作为 verbalize 的 body_state 提示行，不写成记忆。
    """
    return BIORHYTHM.body_state_text()


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


def retrieve_by_concept(keywords: list, time_intent: str = "none",
                        query_text: str = "", top_k: int = 2) -> tuple:
    """概念定向检索：整句 + 关键词命中概念 → 取若干事件段 → 段内按时间倒序直读。

    与向量检索的区别在于段内不做相似度排序：概念已经把话题定死，
    段内记忆又是同一次交互里的连续内容，直接按时间读即可。
    最多带回 top_k 个概念（不再 first-match-wins），各概念的段合并去重。

    返回 (timed_memories, concept_names, degraded)：
      timed_memories 形如 [(real_ts, "[时间短语] 内容"), ...]
      concept_names 为命中的概念名列表；未命中时为空列表
      degraded 表示时间窗内无记录、已降级为"最近若干段"
    """
    q = str(query_text or "").strip()
    if not keywords and not q:
        return [], [], False

    from .concept_store import match_concepts, select_episodes, fetch_members
    from utils.time_phrases import get_relative_time_phrase

    try:
        matches = match_concepts(q, keywords, top_k=top_k)
    except Exception as e:
        append_log(f"[概念路] 概念匹配失败，跳过: {e}")
        return [], [], False
    if not matches:
        return [], [], False

    names = [m["name"] for m in matches]
    for m in matches:
        append_log(f"[概念路] 命中概念「{m['name']}」（{m['how']}）")
    append_log("[概念路] 命中 %d 个概念：%s" % (
        len(matches), "、".join(f"「{m['name']}」（{m['how']}）" for m in matches)))

    episodes = []
    seen_episode_ids = set()
    degraded = False
    for m in matches:
        try:
            selected, deg = select_episodes(m["id"], time_intent)
        except Exception as e:
            append_log(f"[概念路] 概念「{m['name']}」事件段选择失败，跳过: {e}")
            continue
        degraded = degraded or deg
        for ep in selected:
            if ep["id"] in seen_episode_ids:
                continue
            seen_episode_ids.add(ep["id"])
            episodes.append(ep)
    if not episodes:
        append_log(f"[概念路] 命中的概念下没有事件段，跳过: {names}")
        return [], names, degraded

    # 多概念合并后按时间倒序，保证段间顺序与单概念时一致
    episodes.sort(key=lambda e: e.get("end_time") or 0, reverse=True)
    if degraded:
        append_log(f"[概念路] 时间窗内无记录（意图={time_intent}），降级为最近 {len(episodes)} 段")

    try:
        members = fetch_members(episodes)
    except Exception as e:
        append_log(f"[概念路] 成员展开失败，跳过: {e}")
        return [], names, degraded

    timed = []
    seen_texts = set()
    for mem, real_ts in members:
        text = f"[{get_relative_time_phrase(real_ts)}] {mem['content']}"
        key = (round(float(real_ts), 3), text)
        if key in seen_texts:
            continue
        seen_texts.add(key)
        timed.append((real_ts, text))
    label = "、".join(names)
    append_log(f"[概念路] 概念「{label}」带回 {len(timed)} 条（{len(episodes)} 段，意图={time_intent}）")
    return timed, names, degraded


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

    # 阶段D：LLM 拼接回复（传入关键词作为指引）
    # 身体状态作为独立提示行，不再混进记忆列表（避免被排序/去重/随机轮次消耗）
    result = verbalize(timed_memories, keywords, new_state, user_input,
                       timestamps=timestamps, body_state=_get_drowsy_memory())
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
    """兼容旧调用保留：按“最近 N 条他人消息”定位对话窗口。

    新的认知循环已经不再使用这个窗口：
      - 短期记忆只保留她自己的 4~6 条近期想法（core/short_term.py）；
      - 插件内容通过 AppContext 传入（core/apps.py）。
    函数保留给历史测试与外部调用方，避免一次性删除造成兼容问题。
    """
    other_idx = [i for i, m in enumerate(history) if m.get("sender") != BOT_NAME]
    if not other_idx:
        return []
    # 他人消息足够时取倒数第 dialogue_count 条的起始位置；
    # 不足时退回到第一条他人消息，保证窗口从对话真正开始处起算。
    anchor = other_idx[-dialogue_count] if len(other_idx) >= dialogue_count else other_idx[0]
    return history[anchor:]


def _resolve_target_group(fallback: str) -> str:
    """解析本轮主动发言的目标（兼容无插件的旧调用路径）。

    插件通道会用自己的 resolve_target（例如 QQ 动态读取 active_group_id）；
    这里只在没有 channel 时兜底，直接读配置，避免认知层反向依赖平台模块。
    """
    try:
        from config.api_config import config as _cfg
        current = str(_cfg.get("active_group_id") or "").strip()
        if current:
            return current
    except Exception:
        pass
    return fallback


def _send_kwargs_for(send_func, kwargs: dict) -> dict:
    """按发送函数的真实签名裁剪可选参数。

    send_func 可能是旧签名的自定义/测试发送函数，直接传 reply_msg_id/at_qq
    会抛 TypeError。这里用签名过滤而不是 try/except 重试：发送中途抛 TypeError
    时消息可能已经发出，重试会造成重复发言。
    """
    try:
        params = inspect.signature(send_func).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


class _LegacyChannel:
    """没有插件时，把旧的 send_func/media_send_func 包成行为通道。

    只保留最基础的发送能力；@/引用属于 QQ 插件的平台语义，由插件通道提供。
    这层用于 CLI 调试、旧测试与外部调用方的平滑迁移。
    """

    plugin_id = "legacy"
    source = "QQ"

    def __init__(self, send_func=None, media_send_func=None):
        self.send_func = send_func
        self.media_send_func = media_send_func

    def llm_profile(self):
        # None 表示走全局 primary 模型（旧行为）
        return None

    def resolve_target(self, fallback=None):
        return _resolve_target_group(fallback)

    def outbound_options(self, target):
        return {}

    async def send_text(self, target, text, **kwargs) -> bool:
        if not self.send_func:
            return False
        return await self.send_func(target, text, **_send_kwargs_for(self.send_func, kwargs))

    async def send_media(self, target, path, as_sticker: bool = False) -> bool:
        if not self.media_send_func:
            return False
        return await self.media_send_func(target, path, as_sticker=as_sticker)

    def emit_outbound(self, text, kind: str = "text"):
        from utils.event_bus import BUS
        BUS.message.emit(BOT_NAME, text, self.source)


def _emit_outbound(channel, text, kind: str = "text"):
    """把行为结果广播给面板；无插件时沿用 QQ 来源。"""
    if channel is not None:
        try:
            channel.emit_outbound(text, kind)
            return
        except Exception as e:
            append_log(f"[认知循环] 行为通道广播失败: {e}")
    from utils.event_bus import BUS
    BUS.message.emit(BOT_NAME, text, "QQ")


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


def _pick_seed_text() -> str | None:
    """取一条记忆当检索种子：优先浅层意识池，池空或内容缺失时退回随机记忆。

    返回截断到 50 字的内容；记忆库为空时返回 None。
    """
    from .memory_engine import get_random_memory_id, get_memory_content, access_memory
    if _shallow_pool:
        mem_id = random.choice(list(_shallow_pool))
        content = get_memory_content(mem_id)
        if content:
            return content[:50]
    mem_id = get_random_memory_id()
    if mem_id:
        content = get_memory_content(mem_id)
        if content:
            access_memory(mem_id)
            return content[:50]
    return None


async def _run_action_decision(loop, thought_text, should_speak, talk_sent,
                               keywords, media_send_func, target_group_id,
                               context_hint: str = "", channel=None,
                               reasoning: str = ""):
    """执行一次动作抉择并落实动作。返回描述字符串（无动作时返回 None）。

    抉择本身是同步 LLM 调用，放进线程池执行；语义上仍属于本轮的最后一步。
    channel 为插件行为通道；为 None 时退化为旧的 send_func/media_send_func。
    软件未打开时，插件动作层不参与；打开软件由本地 AppRouter 负责。
    """
    from .action_layer import decide_action, action_enabled, APP_ACTIONS
    from .llm_interface import add_to_history

    global _pending_note_memory

    # 已收到停止信号：不再发起新的 LLM 抉择，让本轮尽快收尾。
    # 抉择一轮最多要连发 5 次 API 请求（翻页），会把关停拖住。
    if _graceful_stop:
        return None

    if not action_enabled():
        return None

    # 本轮手里有没有可用的东西：收藏、刚收到的媒体、记事本，或软件状态
    # 记事本允许从零开始写，故开启时即便收藏为空也值得询问一次
    from . import asset_library as ASSETS
    from .action_layer import note_enabled
    apps = APP_CONTEXT.prompt_state() if channel is not None else []
    open_apps = [app for app in apps if app.get("open")]
    # 只有软件已经打开，插件动作层才参与；打开/退出软件由本地 AppRouter 负责。
    if apps and not open_apps:
        return None
    has_app_action = bool(open_apps)
    if not (ASSETS.available_kinds() or ASSETS.list_pending() or note_enabled() or has_app_action):
        return None
    action_apps = open_apps

    # 调用门槛：不再因为“有 keywords”就每轮都问一次动作层。
    # 旧条件是 `talk_sent or keywords`，而关键词几乎每轮都有，等价于每轮必调。
    # 现在只有三种情形会问：
    #   1) 本轮收到新媒体（可能想收藏）；
    #   2) 记事本刚被读写过（可能想继续整理）；
    #   3) 刚说过话；没说上话的轮次只按 action_spontaneous_prob 小概率触发。
    pending = ASSETS.list_pending()
    spoke_now = bool(talk_sent)
    note_changed = bool(_pending_note_memory)
    if not pending and not note_changed and not spoke_now and not has_app_action:
        try:
            from config.api_config import config as _cfg
            prob = float(_cfg.get("action_spontaneous_prob", 0.25))
        except (TypeError, ValueError):
            prob = 0.25
        if random.random() > max(0.0, min(1.0, prob)):
            return None

    # 动作模型属于“当前打开的软件”；没有打开软件时沿用通道的默认动作模型。
    try:
        fallback_profile = channel.action_llm_profile() if channel is not None else None
        profile = APP_CONTEXT.action_llm_profile(fallback=fallback_profile)
    except Exception as e:
        append_log(f"[动作抉择] 读取插件动作 LLM 失败，回落 primary: {e}")
        profile = None
    try:
        fallback_reasoning = bool(channel and channel.action_include_reasoning())
        use_reasoning = APP_CONTEXT.action_include_reasoning(fallback=fallback_reasoning)
        language_trace = reasoning if use_reasoning else ""
    except Exception:
        language_trace = ""
    result = await loop.run_in_executor(
        None, lambda: decide_action(
            thought_text=thought_text,
            should_speak=should_speak,
            said_text=thought_text if talk_sent else "",
            keywords=keywords,
            context_hint=context_hint or ("刚刚有人在群里说话" if keywords and not action_apps else ""),
            profile=profile,
            apps=action_apps,
            language_trace=language_trace,
        )
    )
    if not result:
        return None
    action = result.get("action")
    if not action or action == "none":
        return None
    detail = result.get("detail") or ""

    # -------- 软件动作：打开/关闭软件、点开对话、翻看更早 --------
    if action in APP_ACTIONS:
        outcome = await APP_CONTEXT.execute(action, result)
        if outcome:
            append_log(f"[动作抉择] 软件动作 {action}: {outcome}")
            return outcome
        return None

    # -------- 发送类：把描述直接补进历史记忆 --------
    if action in ("image", "sticker"):
        entry = result.get("entry") or {}
        path = entry.get("path")
        kind = entry.get("kind") or ASSETS.IMAGE
        tag = "图片" if kind == ASSETS.IMAGE else "表情包"
        if not path:
            return None
        # 目标由插件通道动态解析（QQ 插件会重新读取 active_group_id）
        target_group = (
            channel.resolve_target(target_group_id) if channel is not None
            else _resolve_target_group(target_group_id)
        )
        if not target_group:
            append_log("[动作抉择] 没有可用的发送目标，放弃")
            return None
        # 按类型走对应发送方式：表情包需带 sub_type，否则 QQ 会当普通图片显示
        if channel is not None:
            ok = await channel.send_media(target_group, path, as_sticker=(kind == ASSETS.STICKER))
        elif media_send_func is not None:
            ok = await media_send_func(target_group, path, as_sticker=(kind == ASSETS.STICKER))
        else:
            append_log("[动作抉择] 没有可用的媒体发送通道，放弃")
            return None
        if not ok:
            append_log("[动作抉择] 媒体发送失败，未记入历史")
            return None
        ASSETS.mark_sent(kind, detail)
        measure = "张" if kind == ASSETS.IMAGE else "个"
        create_memory(f"我发了{measure}{tag}，内容是：{detail}")
        add_to_history(None, None, f"（我发了{measure}{tag}，内容是：{detail}）")
        _emit_outbound(channel, f"[{tag}] {detail}", "media")
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
        favorite_mem_id = create_memory(f"我收藏了{measure}{tag}，内容是：{detail}")
        # 收藏这件事紧贴在被收藏的图片记忆之后：同一时间戳 + 因果边，
        # 让记忆图里“我看到这张图”与“我把它收下了”连成一段，而不是散落两处。
        for media_mem_id in (result.get("memory_ids") or []):
            try:
                add_link(media_mem_id, favorite_mem_id, 1.0, "causal")
            except Exception as e:
                append_log(f"[认知循环] 收藏记忆建链失败: {e}")
        add_to_history(None, None, f"（我收藏了{measure}{tag}，内容是：{detail}）")
        _emit_outbound(channel, result.get("reply") or f"（收下了这个{tag}）", "media")
        return f"收藏{tag}: {detail}"

    # -------- 记事本：写入/修改/删除/翻开，内容留给下一轮特供记忆（不入库）--------
    if action in ("write_txt", "edit_txt", "delete_txt", "read_txt"):
        name = result.get("note_name") or ""
        text = result.get("note_text") or ""
        # 行为记录与"全文回看"解耦：文件被改成空时，行为仍要留痕。
        if text:
            # 形如：[现在]我打开了我写的“xxx”文件，内容是：xxx
            _pending_note_memory = f"[现在]我打开了我写的“{name}”文件，内容是：{text}"
        elif action == "read_txt":
            return None

        if action == "write_txt":
            written = result.get("written") or ""
            suffix = "（内容过长，已截断）" if result.get("truncated") else ""
            create_memory(f"我在记事本「{name}」里写下：{written}{suffix}")
            add_to_history(None, None, f"（我在记事本「{name}」里写下：{written}{suffix}）")
            _emit_outbound(channel, f"[笔记] {written}", "note")
            return f"写笔记: {name}"

        if action == "edit_txt":
            # 修改既有条目：记下"改了什么"，便于日后追溯（伦理上改动需留痕）
            old_text = result.get("old_text") or ""
            new_text = result.get("new_text") or ""
            create_memory(f"我把记事本「{name}」里的「{old_text}」改成了「{new_text}」")
            add_to_history(None, None, f"（我把记事本「{name}」里的「{old_text}」改成了「{new_text}」）")
            _emit_outbound(channel, f"[笔记] {new_text}", "note")
            return f"改笔记: {name}"

        if action == "delete_txt":
            old_text = result.get("old_text") or ""
            create_memory(f"我把记事本「{name}」里的「{old_text}」划掉了")
            add_to_history(None, None, f"（我把记事本「{name}」里的「{old_text}」划掉了）")
            _emit_outbound(channel, f"[笔记] 划掉了：{old_text}", "note")
            return f"删笔记行: {name}"

        append_log(f"[认知循环] 记事本「{name}」内容已载入下一轮特供记忆")
        return f"翻开笔记: {name}"

    return None


async def cognitive_loop(send_func=None, target_group_id: str = None, media_send_func=None,
                         channel=None):
    """
    永续认知循环 —— 本地“默认模式网络”永远在线。
    无论是否有用户输入，循环始终运行：
      消费队列关键词 → 本地检索/扩散 → 行为思考(生成内心独白+发言决策)
      → 存入记忆 → 回复jieba分词入队 → 检查新消息 → 循环

    关键词来源：上一轮的回复（心理活动或发送消息）与接收到的消息，
    均以 jieba 搜索引擎模式分词后进入消费队列，每轮取空队列处理全部关键词。

    channel: 插件行为通道（plugins.base.BehaviorChannel）。本地认知与平台无关，
             行为思考使用 channel.llm_profile() 指定的 LLM。为 None 时用
             send_func/media_send_func 包成兼容通道（CLI/旧测试）。
    在 NapCat 连接后启动，断开时 request_graceful_stop 触发：
      当前轮继续执行到回复输出，跳过复搜后退出。
    """
    global _keyword_queue, _shallow_pool, _cognitive_running, _graceful_stop
    global _keyword_continuity, _inhibited_seeds, _inhibited_edges, _inhibited_keywords
    global _pending_note_memory

    from .llm_interface import add_to_history
    # 强制转向分支要按 ID 取记忆内容，必须在本函数作用域内可见
    from .memory_engine import memories, access_memory, get_memory_content

    # 重置停止标志和抑制状态，允许新会话启动
    _graceful_stop = False
    _cognitive_running = True
    _pending_note_memory = None
    _keyword_continuity.clear()
    _inhibited_seeds.clear()
    _inhibited_edges.clear()
    _inhibited_keywords.clear()
    _keyword_queue.clear()
    KEYWORDS.clear()
    SHORT_TERM.clear()
    APP_CONTEXT.clear()
    APP_ROUTER.reset()
    current_keywords = []

    # 没有插件时退化为旧的发送回调；有插件时由插件提供行为通道与 LLM。
    if channel is None:
        channel = _LegacyChannel(send_func=send_func, media_send_func=media_send_func)

    append_log("="*40)
    append_log("[认知循环] 本地永续认知循环启动"
               f"（行为插件：{getattr(channel, 'plugin_id', 'legacy')}）")
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
            # 醒来即清掉睡前残留：睡前关键词、上一轮回复、反刍计数都不该带进新的一觉。
            KEYWORDS.clear()
            _keyword_queue.clear()
            SHORT_TERM.clear()
            APP_CONTEXT.clear()
            APP_ROUTER.reset()
            current_keywords = []
            _keyword_continuity.clear()
            _inhibited_seeds.clear()
            _inhibited_edges.clear()
            _inhibited_keywords.clear()
            continue

        try:
            loop = asyncio.get_event_loop()

            # 抑制计时每轮推进一次，跳过本轮（检索为空）的轮次也算一轮
            _decay_inhibition()

            # 软件状态每轮刷新：未打开时只留下“某软件有 N 条未读”的提醒；
            # 打开后由插件把对话内容作为 AppContext 条目送进来。
            try:
                APP_CONTEXT.refresh()
            except Exception as e:
                append_log(f"[认知循环] 刷新软件状态失败: {e}")

            # 本地软件门控：打开/退出软件由本地层决定，不经过插件动作 LLM。
            # 打开后软件内容会立即进入 AppContext，供本轮语言思考使用。
            try:
                statuses = APP_CONTEXT.prompt_state()
                decision = await loop.run_in_executor(
                    None,
                    APP_ROUTER.tick,
                    statuses,
                    SHORT_TERM.texts(),
                    APP_CONTEXT.last_content_ts(),
                )
                if decision:
                    outcome = await APP_CONTEXT.execute(decision.get("action"), decision)
                    append_log(f"[认知循环] 软件门控: {decision} → {outcome}")
            except Exception as e:
                append_log(f"[认知循环] 软件门控异常: {e}")

            # ============================
            # Step 1: 合并关键词信箱（追加，而不是整批替换）
            # ============================
            while _keyword_queue:
                kws = _keyword_queue.popleft()
                KEYWORDS.add(kws, "queue")

            merged_keywords, kw_sources = keyword_snapshot()
            if merged_keywords:
                current_keywords = merged_keywords
                src_note = ""
                if kw_sources:
                    src_note = "（" + "，".join(f"{k}:{v}" for k, v in kw_sources.items()) + "）"
                append_log(f"[认知循环] 合并关键词{src_note}: {current_keywords}")

            # ============================
            # Step 2: 若无关键词，从记忆库取种子（线程安全）
            # ============================
            if not current_keywords:
                seed_text = _pick_seed_text()
                if seed_text:
                    current_keywords = [seed_text]
                    append_log(f"[认知循环] 无新消息，随机种子: {seed_text[:30]}...")
                elif APP_CONTEXT.has_context():
                    # 没有检索种子，但软件有未读提醒/打开着；仍然要进入行为思考，
                    # 否则动作层永远没有机会“打开软件”。
                    append_log("[认知循环] 无关键词/种子，但软件上下文存在，继续本轮")
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
                    # 被抑制的词不能留在信箱里：下一轮合并会把它们原样带回，形成空转
                    KEYWORDS.retain(current_keywords)
                    if not current_keywords:
                        # 全被抑制光了，检索必然为空；换一个新种子，别硬跑空轮
                        seed_text = _pick_seed_text()
                        if seed_text:
                            current_keywords = [seed_text]
                            append_log(f"[反刍抑制] 关键词全部被抑制，改用新种子: {seed_text[:30]}...")

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
            # ============================
            concept_memories, concept_names, concept_degraded = retrieve_by_concept(
                current_keywords, consume_time_intent(),
                query_text=consume_user_input(), top_k=2,
            )
            concept_texts = [text for _, text in concept_memories]
            concept_ts = [ts for ts, _ in concept_memories]

            # ============================
            # Step 3': 向量检索与扩散（概念未命中时为主，命中时保留少量做跨概念联想）
            # ============================
            related_pairs = retrieve_and_diffuse(
                current_keywords, max_memories=3 if concept_memories else 10,
                inhibited_seeds=set(_inhibited_seeds.keys()),
                inhibited_edges=set(_inhibited_edges.keys())
            )
            if not related_pairs and not concept_memories and not APP_CONTEXT.has_context():
                append_log("[认知循环] 无相关记忆，跳过本轮")
                current_keywords = []
                # 检索不到东西的关键词留在信箱里，只会每 3 秒被原样回灌一次
                KEYWORDS.retain(current_keywords)
                await asyncio.sleep(3)
                continue

            related_memories = [text for _, text in related_pairs]
            related_ts = [ts for ts, _ in related_pairs]

            # ============================
            # Step 3b: 软件上下文（插件传入主进程）
            # 不再按“最近 4~6 条别人的消息”截短期窗口：
            #   - 她自己的近期想法由 SHORT_TERM 保存（4~6 条）；
            #   - 软件未读提醒、打开的对话内容由 APP_CONTEXT 保存；
            #   - 两者随后作为 pinned 进入行为思考，不参与关键词过滤。
            # ============================
            app_pairs = APP_CONTEXT.snapshot()
            app_memories = [text for _, text in app_pairs]
            app_ts = [ts for ts, _ in app_pairs]
            action_context = APP_CONTEXT.latest_line()
            if app_memories:
                append_log(f"[认知循环] 软件上下文并入 {len(app_memories)} 条: {app_memories[:3]}")

            # ============================
            # Step 3c: 身体状态（睡前 / 刚醒）
            # 只作为独立提示行传给行为思考，不写成记忆，也不参与排序去重。
            # ============================
            body_state = BIORHYTHM.body_state_text()

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

            short_pairs = SHORT_TERM.recent()
            pinned_memories = (
                [text for _, text in short_pairs]
                + app_memories
                + ([note_mem] if note_mem else [])
                # 概念路带回的记忆作为特供：不参与评分、不被关键词过滤丢弃
                + concept_texts
            )
            pinned_ts = (
                [ts for ts, _ in short_pairs]
                + app_ts
                + ([note_ts] if note_ts is not None else [])
                + concept_ts
            )

            # 本地检索/扩散已完成；最后一步“行为思考”交给插件配置的 LLM。
            # 提示词只包含名字与记忆（见 core/behavior.py），不注入平台信息。
            try:
                profile = channel.llm_profile()
            except Exception as e:
                append_log(f"[认知循环] 读取插件行为 LLM 失败，回落 primary: {e}")
                profile = None
            try:
                fallback_reasoning = bool(channel and channel.action_include_reasoning())
                include_reasoning = bool(
                    APP_CONTEXT.any_app_open()
                    and APP_CONTEXT.action_include_reasoning(fallback=fallback_reasoning)
                )
            except Exception:
                include_reasoning = False

            result = await loop.run_in_executor(
                None, lambda: BehaviorEngine(
                    profile=profile,
                    plugin_id=getattr(channel, "plugin_id", "core"),
                ).think(
                    related_memories, current_keywords, state,
                    pinned=pinned_memories,
                    timestamps=related_ts,
                    pinned_timestamps=pinned_ts,
                    body_state=body_state,
                    include_reasoning=include_reasoning,
                )
            )

            if not result or not isinstance(result, dict):
                current_keywords = []
                #await asyncio.sleep(3)
                continue

            thought_text = result.get("text", "").strip()
            should_speak = result.get("say", False)
            reasoning = str(result.get("reasoning") or "") if include_reasoning else ""

            # 去掉句末句号（在做入库与对话历史之前，只去末尾这一个）
            thought_text = _strip_trailing_period(thought_text)

            if not thought_text:
                append_log("[认知循环] 行为思考返回空，跳过")
                #await asyncio.sleep(3)
                continue

            # 她自己的近期想法进入短期记忆窗口（4~6 条），
            # 不再用“最近 4~6 条别人的消息”当短期窗口锚点。
            # 发送结果要等 Step 6 才知道，所以“我说/我想”先按是否有发送目标判断；
            # 真正的短期标签在发送后按 talk_sent 写入。
            target_group = channel.resolve_target(target_group_id)
            can_send = getattr(channel, "send_func", True) is not None
            attempted_speech = bool(should_speak and target_group and can_send)

            # ============================
            # Step 5: 存入记忆库
            # ============================
            if attempted_speech:
                memory_text = f"我说：{thought_text}"
            else:
                memory_text = f"我想：{thought_text}"

            # F08：写记忆含同步嵌入请求，放线程池避免阻塞事件循环；
            # 同一次写入里把她的回复追加到刚刚归入的概念，让"你上次说什么"也能召回。
            def _write_reply_memory():
                mid = create_memory(memory_text)
                try:
                    from .concept_store import append_member, get_last_concept_id
                    last_cid = get_last_concept_id()
                    if last_cid:
                        append_member(last_cid, mid)
                except Exception as exc:
                    append_log(f"[概念层] 回复追加失败: {exc}")
                return mid

            mem_id = await loop.run_in_executor(None, _write_reply_memory)
            _shallow_pool.append(mem_id)

            append_log(f"[认知循环] {'【发言】' if should_speak else '【内心】'}: {thought_text}")

            # ============================
            # Step 6: 发送消息（若应发言）
            # 先发、再按**实际送达结果**决定怎么记历史：
            #   送达成功 → 记为"我说"，下一轮正常并入；
            #   未送达   → 仍记入历史但追加"（消息发送失败）"后缀，
            #              下一轮读到的是带标记的文本，不会被误当成"已经说出口"。
            # ============================
            talk_sent = False
            # target_group / can_send 在 Step 5 前已解析，本轮复用同一结果。
            if should_speak and thought_text and target_group and can_send:
                # @/引用等平台语义由插件决定（QQ 插件只回应冲她来的消息）
                try:
                    options = channel.outbound_options(target_group) or {}
                except Exception as e:
                    append_log(f"[认知循环] 解析发送选项失败: {e}")
                    options = {}

                delivered = await channel.send_text(target_group, thought_text, **options)
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
                _emit_outbound(channel, thought_text, "text")
                APP_ROUTER.note_activity()

            # 短期里的“我说/我想”以实际送达为准；没发出去就是只在心里想。
            SHORT_TERM.add_thought(thought_text, bool(talk_sent))

            # ============================
            # Step 6b: 动作抉择（本能冲动，本轮最后一步）
            # 本轮必须等抉择完成才继续；实际执行在线程池中，
            # 以免阻塞事件循环导致 NapCat 收消息与生物钟 tick 停摆。
            # ============================
            try:
                acted = await _run_action_decision(
                    loop, thought_text, should_speak, talk_sent, current_keywords,
                    media_send_func, target_group_id, action_context, channel=channel,
                    reasoning=reasoning,
                )
                if acted:
                    append_log(f"[认知循环] 动作抉择已执行: {acted}")
                    APP_ROUTER.note_activity()
            except Exception as e:
                append_log(f"[认知循环] 动作抉择异常: {e}")

            # ============================
            # Step 7: 优雅停止检查（不复搜）
            # ============================
            if _graceful_stop:
                append_log("[认知循环] 收到停止信号，完成本轮回复，跳过复搜")
                break

            # ============================
            # Step 7b: 关键词信箱 —— 本轮回复分词后追加（下一轮合并消费）
            # ============================
            reply_keywords = extract_keywords_jieba(thought_text)
            if reply_keywords:
                KEYWORDS.add(reply_keywords, "reply")
                append_log(f"[认知循环] 回复分词入信箱: {reply_keywords}")

            # 本轮实际参与检索的关键词留在信箱里继续生效；
            # 只有被抑制/被过滤掉的才去掉。下一轮会把新消息关键词追加进来。
            KEYWORDS.retain(current_keywords)

            # ============================
            # Step 8: 休眠
            # 抑制计数已在本轮开头推进（见 _decay_inhibition），不再在轮末衰减：
            # 放在这里时，“跳过本轮”的分支会直接 continue，衰减永远走不到。
            # ============================
            await asyncio.sleep(3)

            # ============================
            # 阶段6：热记忆下沉交给 HotSetPlanner + MemoryWriter
            # 认知循环不再自己触发淘汰（避免与 drift_loop、auto_save 三处重复）。
            # ============================

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