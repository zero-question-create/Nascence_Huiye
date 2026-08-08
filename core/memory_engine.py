# core/memory_engine.py
import uuid
#import heapq   # Dijkstra算法（已弃用）
import jieba
import os
import time
import json
import requests
import faiss
import numpy as np
import sqlite3
import threading
from collections import deque
from .virtual_clock import clock
from utils.monitor import append_log

_data_lock = threading.RLock()

# ========== 配置常量 ==========
from config.api_config import config
OLLAMA_BASE_URL = config["ollama_base_url"]
OLLAMA_EMBED_MODEL = config["ollama_embed_model"]
DB_FILE = "data/test/memory.db"     # 冷热数据交换保存地址
SIMILARITY_THRESHOLD = 0.22         # 建立语义链接的最低相似度
DEDUP_THRESHOLD = 0.92              # 余弦相似度超过此值视为重复
DEFAULT_HALF_LIFE = 2 * 24 * 3600   # 默认记忆半衰值2天
LINK_HALF_LIFE = 7 * 24 * 3600      # 统一链接半衰值7天
TEMP_HALF_LIFE = 600                # 临时记忆半衰值10分钟
STRENGTHEN_BASE = 12 * 3600         # 基础强化量12小时
INTERVAL_K = 6 * 3600               # 间隔折扣时间常数K=6小时
K_RETRIEVAL = 8                     # 半衰期常数k=5
RETRIEVAL_DEDUP_THRESHOLD = 0.75    # 检索结果去冗余弦相似度
RETRIEVAL_MIN_EFFECTIVE = 0.02      # 有效相似度最低门槛
WORDWEB_WINDOW_SIZE = 5             # 滑动窗口大小（以词为单位）
WORDWEB_MIN_COOCCURRENCE = 2        # 最小共现次数，低于此值不参与后期扩散
EMBED_DIM = 768                     # dmeta-embedding-zh 实测为 768 维
MAX_OUT_EDGES = 5                   # 每个节点从 links 中保留的最强出边数
MAX_QUEUE_SIZE = 2000               # 队列硬上限，防止爆炸

# ========== 体力消耗系数 ==========
COST_FACTOR = {
    "causal": 0.5,        # 因果链接，最容易追溯
    "temporal": 1.0,      # 时序链接，正常消耗
    "semantic": 1.5,      # 系统建立的语义链接
    "semantic_fast": 2.0, # faiss 快速召回，消耗更高（联想更费力）
}
# ========== 全局状态 ==========
wordweb = {}    # {(词A, 词B): {"forward_count": int, "avg_distance": float, "last_updated": float}}
memories = {}   # {memory_id: memory_dict}
links = {}      # {(src_id, tgt_id): {"weight": float, "type": str}}
pending_deletion = set()            # 待删除记忆集合
_faiss_index = None                 # faiss 索引实例
_faiss_to_mem = []                  # faiss_id → memory_id
_mem_to_faiss = {}                  # memory_id → faiss_id
_db_conn = None                     # 全局数据库实例
hot_ids = set()                     # 当前在内存中的记忆 ID
_last_created_ids = []              # 存储本轮新增的记忆 ID（QQ接口：用于追踪本轮对话新创建的记忆（供 QQ bot 获取））
word_to_memories = {}               # {词: set(memory_id)}

# ========== 每日指标计数器 ==========
# 当日累计值（每次事件发生时 +1）
_count_mem_created = 0
_count_mem_deleted = 0
_count_link_created = 0
_count_link_deleted = 0
_count_wordweb_created = 0

# 其他指标计数器（在 qq_bot 中递增）
_count_message_sent = 0
_count_self_ref = 0
_count_active_attempt = 0
_count_active_success = 0

# 日志文件路径
METRICS_LOG_FILE = "data/test/metrics_daily.jsonl"
METRICS_BASELINE_FILE = "data/test/metrics_baseline.json"

def _write_daily_metrics():
    """每日24点调用：计算今日增量，写入日志，更新基线"""
    # 读取昨日基线
    if os.path.exists(METRICS_BASELINE_FILE):
        with open(METRICS_BASELINE_FILE, 'r', encoding='utf-8') as f:
            baseline = json.load(f)
    else:
        baseline = {}

    # 计算今日增量
    increments = {
        "date": time.strftime("%Y-%m-%d", time.localtime()),
        "mem_created": _count_mem_created - baseline.get("mem_created", 0),
        "mem_deleted": _count_mem_deleted - baseline.get("mem_deleted", 0),
        "link_created": _count_link_created - baseline.get("link_created", 0),
        "link_deleted": _count_link_deleted - baseline.get("link_deleted", 0),
        "wordweb_created": _count_wordweb_created - baseline.get("wordweb_created", 0),
        "msg_sent": _count_message_sent - baseline.get("msg_sent", 0),
        "self_ref": _count_self_ref - baseline.get("self_ref", 0),
        "active_attempt": _count_active_attempt - baseline.get("active_attempt", 0),
        "active_success": _count_active_success - baseline.get("active_success", 0),
    }

    # 写入日志（追加一行）
    os.makedirs(os.path.dirname(METRICS_LOG_FILE), exist_ok=True)
    with open(METRICS_LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(increments, ensure_ascii=False) + "\n")

    # 更新基线（保存当前累计值）
    new_baseline = {
        "mem_created": _count_mem_created,
        "mem_deleted": _count_mem_deleted,
        "link_created": _count_link_created,
        "link_deleted": _count_link_deleted,
        "wordweb_created": _count_wordweb_created,
        "msg_sent": _count_message_sent,
        "self_ref": _count_self_ref,
        "active_attempt": _count_active_attempt,
        "active_success": _count_active_success,
    }
    with open(METRICS_BASELINE_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_baseline, f, ensure_ascii=False)

def _init_metrics_counters():
    """启动时调用：从基线文件恢复上次保存的计数器累计值"""
    global _count_mem_created, _count_mem_deleted
    global _count_link_created, _count_link_deleted
    global _count_wordweb_created
    global _count_message_sent, _count_self_ref
    global _count_active_attempt, _count_active_success

    if not os.path.exists(METRICS_BASELINE_FILE):
        # 首次运行，没有基线文件，所有计数器保持为 0
        return

    try:
        with open(METRICS_BASELINE_FILE, 'r', encoding='utf-8') as f:
            baseline = json.load(f)

        _count_mem_created = baseline.get("mem_created", 0)
        _count_mem_deleted = baseline.get("mem_deleted", 0)
        _count_link_created = baseline.get("link_created", 0)
        _count_link_deleted = baseline.get("link_deleted", 0)
        _count_wordweb_created = baseline.get("wordweb_created", 0)
        _count_message_sent = baseline.get("msg_sent", 0)
        _count_self_ref = baseline.get("self_ref", 0)
        _count_active_attempt = baseline.get("active_attempt", 0)
        _count_active_success = baseline.get("active_success", 0)

        print(f"[指标] 计数器已从基线恢复，累计值："
              f"记忆+{_count_mem_created}/-{_count_mem_deleted}，"
              f"链接+{_count_link_created}/-{_count_link_deleted}，"
              f"消息{_count_message_sent}")
    except Exception as e:
        print(f"[指标] 基线文件读取失败，计数器保持为0: {e}")

def _export_metrics_counters() -> dict:
    """导出所有当前计数器值，供持久化使用"""
    return {
        "mem_created": _count_mem_created,
        "mem_deleted": _count_mem_deleted,
        "link_created": _count_link_created,
        "link_deleted": _count_link_deleted,
        "wordweb_created": _count_wordweb_created,
        "msg_sent": _count_message_sent,
        "self_ref": _count_self_ref,
        "active_attempt": _count_active_attempt,
        "active_success": _count_active_success,
    }

def _import_metrics_counters(data: dict):
    """从持久化数据中恢复计数器值"""
    global _count_mem_created, _count_mem_deleted
    global _count_link_created, _count_link_deleted
    global _count_wordweb_created
    global _count_message_sent, _count_self_ref
    global _count_active_attempt, _count_active_success

    _count_mem_created = data.get("mem_created", _count_mem_created)
    _count_mem_deleted = data.get("mem_deleted", _count_mem_deleted)
    _count_link_created = data.get("link_created", _count_link_created)
    _count_link_deleted = data.get("link_deleted", _count_link_deleted)
    _count_wordweb_created = data.get("wordweb_created", _count_wordweb_created)
    _count_message_sent = data.get("msg_sent", _count_message_sent)
    _count_self_ref = data.get("self_ref", _count_self_ref)
    _count_active_attempt = data.get("active_attempt", _count_active_attempt)
    _count_active_success = data.get("active_success", _count_active_success)

# ========== 全局数据库 ==========
def _get_db():
    """获取全局数据库连接（惰性初始化）"""
    global _db_conn
    if _db_conn is None:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        _db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")  # 提高并发读性能
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                vector BLOB NOT NULL,
                half_life REAL NOT NULL,
                last_accessed REAL NOT NULL,
                creation_time REAL NOT NULL,
                last_strengthen_time REAL NOT NULL,
                concept_tag_ids TEXT DEFAULT '[]'
            )
        """)
    return _db_conn

def _load_memory_from_db(mem_id: str) -> dict:
    """从 SQLite 加载一条记忆到内存，并标记为热数据"""
    db = _get_db()
    row = db.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    if row is None:
        return None
    # 构造 memory_dict
    mem = {
        "id": row[0],
        "content": row[1],
        "vector": np.frombuffer(row[2], dtype=np.float32).tolist(),
        "half_life": row[3],
        "last_accessed": row[4],
        "creation_time": row[5],
        "last_strengthen_time": row[6],
        "concept_tag_ids": json.loads(row[7]) if row[7] else []
    }
    memories[mem_id] = mem
    hot_ids.add(mem_id)
    return mem

def _batch_load_memories(mem_ids: list):
    """批量从 SQLite 加载记忆到内存，避免逐条查询导致 Result too large"""
    if not mem_ids:
        return
    db = _get_db()
    placeholders = ','.join('?' * len(mem_ids))
    sql = f"SELECT * FROM memories WHERE id IN ({placeholders})"
    rows = db.execute(sql, mem_ids).fetchall()
    for row in rows:
        mem_id = row[0]
        if mem_id in memories:
            continue
        mem = {
            "id": mem_id,
            "content": row[1],
            "vector": np.frombuffer(row[2], dtype=np.float32).tolist(),
            "half_life": row[3],
            "last_accessed": row[4],
            "creation_time": row[5],
            "last_strengthen_time": row[6],
            "concept_tag_ids": json.loads(row[7]) if row[7] else []
        }
        memories[mem_id] = mem
        hot_ids.add(mem_id)

def _evict_cold_memories(max_hot=10000):
    """将最久未访问的热数据移出内存，保留 SQLite 和 faiss"""
    with _data_lock:
        if len(hot_ids) <= max_hot:
            return
        sorted_ids = sorted(hot_ids, key=lambda mid: memories[mid]["last_accessed"])
        to_evict = sorted_ids[:len(hot_ids) - max_hot]
        for mid in to_evict:
            mem = memories[mid]
            db = _get_db()
            db.execute(
                """UPDATE memories SET last_accessed = ?, half_life = ?, last_strengthen_time = ?
                   WHERE id = ?""",
                (mem["last_accessed"], mem["half_life"], mem["last_strengthen_time"], mid)
            )
            db.commit()
            del memories[mid]
            hot_ids.remove(mid)

def _build_word_to_memories():
    """从所有记忆的 content 重建词→记忆ID的倒排索引"""
    global word_to_memories
    word_to_memories.clear()
    for mem_id, mem in memories.items():
        words = set(jieba.cut(mem["content"]))
        for word in words:
            if word not in word_to_memories:
                word_to_memories[word] = set()
            word_to_memories[word].add(mem_id)
    print(f"[词网] 倒排索引重建完成，共 {len(word_to_memories)} 个词")


# ========== 模型加载 ==========
def get_model():
    print("正在启动语义模型")
    try:
        text_to_vector("启动")
    except Exception as e:
        print(f"[Ollama] 模型预热失败: {e}")
    print("语义模型启动完成！")

def text_to_vector(text: str) -> list:
    """
    使用 Ollama 的 API 将文本转换为向量。
    兼容新旧端点：新版 Ollama 用 /api/embed（旧 /api/embeddings 已移除），
    先尝试新版，404 时回退旧版。
    前提：Ollama 已在本地运行，且已通过 `ollama pull shaw/dmeta-embedding-zh` 拉取模型。
    """
    url_new = f"{OLLAMA_BASE_URL}/api/embed"
    url_old = f"{OLLAMA_BASE_URL}/api/embeddings"
    try:
        # 新版端点 /api/embed
        resp = requests.post(url_new, json={"model": OLLAMA_EMBED_MODEL, "input": text}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            embeddings = data.get("embeddings")
            if embeddings:
                return embeddings[0]
        # 旧版端点 /api/embeddings（新版不可用时回退）
        resp = requests.post(url_old, json={"model": OLLAMA_EMBED_MODEL, "prompt": text}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        embedding = data.get("embedding")
        if embedding is None:
            raise ValueError("Ollama 返回的数据中未找到 'embedding' 字段")
        return embedding
    except requests.exceptions.ConnectionError:
        print("[Ollama] 连接失败，请确认 Ollama 服务是否已启动（默认地址：http://localhost:11434）")
        raise
    except requests.exceptions.Timeout:
        print("[Ollama] 请求超时，请检查网络或 Ollama 服务响应")
        raise
    except requests.exceptions.HTTPError as e:
        print(f"[Ollama] HTTP 错误: {e}")
        raise
    except (KeyError, ValueError) as e:
        print(f"[Ollama] 解析响应失败: {e}")
        raise

def cosine_similarity(vec_a: list, vec_b: list) -> float:
    a = np.array(vec_a)
    b = np.array(vec_b)
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

def _grow_wordweb(content: str, mem_id: str):
    """从记忆文本中提取字词时序关系，织入字词网络。不做任何认知决策。"""
    words = list(jieba.cut(content))
    for i, word_a in enumerate(words):
        # 滑动窗口：看当前词后面的几个词
        for j in range(i + 1, min(i + 1 + WORDWEB_WINDOW_SIZE, len(words))):
            word_b = words[j]
            key = (word_a, word_b)
            distance = j - i
            now = clock.now()
            if key in wordweb:
                entry = wordweb[key]
                # 增量更新平均距离
                total = entry["forward_count"] * entry["avg_distance"]
                entry["forward_count"] += 1
                entry["avg_distance"] = (total + distance) / entry["forward_count"]
                entry["last_updated"] = now
            else:
                wordweb[key] = {
                    "forward_count": 1,
                    "avg_distance": float(distance),
                    "last_updated": now
                }
                global _count_wordweb_created
                _count_wordweb_created += 1
    for word in set(words):  # 每个词只记录一次
        if word not in word_to_memories:
            word_to_memories[word] = set()
        word_to_memories[word].add(mem_id)

# ========== faiss索引操作 ==========
def _init_faiss_index():
    """初始化或加载 faiss 索引"""
    global _faiss_index, _faiss_to_mem, _mem_to_faiss
    _faiss_index = faiss.IndexFlatIP(EMBED_DIM)  # 内积索引，等价于余弦相似度（需归一化向量）
    _faiss_to_mem = []
    _mem_to_faiss = {}
    print(f"[faiss] 索引初始化完成，维度={EMBED_DIM}")

def _rebuild_faiss_index():
    """从 SQLite 全量重建 faiss 索引和映射表"""
    with _data_lock:
        global _faiss_index, _faiss_to_mem, _mem_to_faiss
        _init_faiss_index()
        db = _get_db()
        rows = db.execute("SELECT id, vector FROM memories").fetchall()
        for mem_id, vec_blob in rows:
            vec = np.frombuffer(vec_blob, dtype=np.float32).reshape(1, -1)
            faiss.normalize_L2(vec)
            _faiss_index.add(vec)
            faiss_id = _faiss_index.ntotal - 1
            _faiss_to_mem.append(mem_id)
            _mem_to_faiss[mem_id] = faiss_id
        print(f"[faiss] 从 SQLite 全量重建完成，共 {_faiss_index.ntotal} 条向量")

# ========== 记忆核心操作 ==========
def generate_memory_id() -> str:
    return str(uuid.uuid4())

def add_link(src_id: str, tgt_id: str, weight: float, link_type: str):
    key = (src_id, tgt_id)
    now = clock.now()
    with _data_lock:
        if key in links:
            old_weight = links[key]["weight"]
            new_weight = min(1.0, old_weight + 0.1 * weight)
            links[key]["weight"] = new_weight
            links[key]["type"] = link_type
            links[key]["last_accessed"] = now
        else:
            links[key] = {"weight": weight, "type": link_type, "last_accessed": now, "creation_time": now}
            global _count_link_created
            _count_link_created += 1

def decay_link(src_id: str, tgt_id: str) -> float:
    key = (src_id, tgt_id)
    with _data_lock:
        if key not in links:
            return 0.0
        now = clock.now()
        delta = now - links[key].get("last_accessed", links[key].get("creation_time", now))
        decay = time_decay(delta, LINK_HALF_LIFE)
        new_weight = links[key]["weight"] * decay
        if new_weight < 0.01:
            del links[key]
            return 0.0
        links[key]["weight"] = new_weight
        links[key]["last_accessed"] = now
        return new_weight

def build_initial_links(new_mem_id: str):
    new_mem = memories.get(new_mem_id)
    if not new_mem:
        return
    new_vec = np.array(new_mem["vector"], dtype=np.float32).reshape(1, -1)
    faiss.normalize_L2(new_vec)

    # 用 faiss 找最相似的 top-10 个已有节点
    k = min(10, _faiss_index.ntotal)
    if k == 0:
        return
    scores, faiss_ids = _faiss_index.search(new_vec, k)

    for score, faiss_id in zip(scores[0], faiss_ids[0]):
        if faiss_id == -1:
            continue
        other_id = _faiss_to_mem[faiss_id]
        if other_id == new_mem_id:
            continue
        sim = float(score)  # 内积 = 余弦相似度
        if sim >= SIMILARITY_THRESHOLD:
            add_link(new_mem_id, other_id, sim, "semantic")
            add_link(other_id, new_mem_id, sim, "semantic")

def create_memory(content: str, half_life: float = DEFAULT_HALF_LIFE) -> str:
    vec = text_to_vector(content)
    mem_id = generate_memory_id()
    now = clock.now()
    memory_dict = {
        "id": mem_id,
        "content": content,
        "vector": vec,
        "half_life": half_life,
        "last_accessed": now,
        "creation_time": now,
        "last_strengthen_time": now,
        "concept_tag_ids": []
    }

    with _data_lock:
        db = _get_db()
        db.execute(
            """INSERT INTO memories (id, content, vector, half_life, last_accessed,
               creation_time, last_strengthen_time, concept_tag_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, content, np.array(vec, dtype=np.float32).tobytes(),
             half_life, now, now, now, json.dumps([]))
        )
        db.commit()

        vec_arr = np.array(vec, dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(vec_arr)
        _faiss_index.add(vec_arr)
        faiss_id = _faiss_index.ntotal - 1
        _faiss_to_mem.append(mem_id)
        _mem_to_faiss[mem_id] = faiss_id

        memories[mem_id] = memory_dict
        hot_ids.add(mem_id)

        build_initial_links(mem_id)
        _grow_wordweb(content, mem_id)

    _last_created_ids.append(mem_id)
    global _count_mem_created
    _count_mem_created += 1
    return mem_id

def access_memory(mem_id: str):
    with _data_lock:
        mem = memories.get(mem_id)
        if not mem:
            return
        now = clock.now()
        mem["last_accessed"] = now
        delta_t = now - mem["last_strengthen_time"]
        if delta_t < 0:
            delta_t = 0
        discount = delta_t / (INTERVAL_K + delta_t)
        strengthen_amount = STRENGTHEN_BASE * discount
        mem["half_life"] += strengthen_amount
        mem["last_strengthen_time"] = now

def time_decay(delta_t: float, half_life: float) -> float:
    if half_life <= 0:
        return 1.0
    return 2 ** (-delta_t / half_life)

def check_and_handle_expired(mem_id: str) -> bool:
    mem = memories.get(mem_id)
    if not mem:
        return False
    now = clock.now()
    delta = now - mem["last_accessed"]
    decay = 2 ** (-delta / mem["half_life"])
    if decay < 0.001:
        pending_deletion.add(mem_id)
        del memories[mem_id]
        return False
    return True

def retrieve_similar(query_text: str, k: int = K_RETRIEVAL) -> list:
    append_log(f"=====搜索关键词：{query_text}=====")
    q_vec = np.array(text_to_vector(query_text), dtype=np.float32).reshape(1, -1)
    faiss.normalize_L2(q_vec)

    # 粗筛：faiss 快速召回 k * 3 个候选
    search_k = k * 5
    scores, faiss_ids = _faiss_index.search(q_vec, search_k)

    # 精确筛选：时间衰减 + 去冗余
    now = clock.now()
    results = []
    seen_ids = set()

    for score, faiss_id in zip(scores[0], faiss_ids[0]):
        if faiss_id == -1:            # faiss 返回 -1 表示无效结果
            continue
        mem_id = _faiss_to_mem[faiss_id]

        mem = memories.get(mem_id)
        if not mem:
            mem = _load_memory_from_db(mem_id)

        if not mem or mem_id in seen_ids:
            continue
        seen_ids.add(mem_id)

        delta = now - mem["last_accessed"]
        decay = 2 ** (-delta / mem["half_life"])
        decay = min(decay, 0.95)
        # 时间新鲜度：新记忆天然占优，随时间逐渐失去优势
        time_since_creation = now - mem["creation_time"]
        freshness = 1.0 / (1.0 + time_since_creation / DEFAULT_HALF_LIFE)
        effective = score * decay * freshness       # score 是余弦相似度（内积，因为向量已归一化）

        if effective < RETRIEVAL_MIN_EFFECTIVE:
            continue
        results.append((effective, mem))
        access_memory(mem_id)

    results.sort(key=lambda x: x[0], reverse=True)

    # 去冗余
    deduped = []
    for sim, mem in results:
        content_dup = any(mem["content"] == sel_mem["content"] for _, sel_mem in deduped)
        if content_dup:
            continue
        redundant = False
        for _, sel_mem in deduped:
            if cosine_similarity(mem["vector"], sel_mem["vector"]) > RETRIEVAL_DEDUP_THRESHOLD:
                redundant = True
                break
        if not redundant:
            deduped.append((sim, mem))
            if len(deduped) >= k:
                break

    for sim, mem in deduped:
        content = mem["content"]
        append_log(f"检索到“{content}”,相似度：{sim}")

    return deduped[:k]

def retrieve_by_exact_keywords(keywords: list, k: int = 5) -> list:
    """
    通过词网倒排索引，精确查找包含任意关键词的记忆。
    返回: [(score, memory_dict), ...]
    """
    matched_ids = set()
    MAX_IDS_PER_KEYWORD = 50   # 每个关键词最多取 50 个记忆ID
    for kw in keywords:
        if kw in word_to_memories:
            ids = word_to_memories[kw]
            if len(ids) > MAX_IDS_PER_KEYWORD:
                # 如果太多，随机取一部分（或按最近访问时间排序，这里简单用 set 迭代截断）
                ids = set(list(ids)[:MAX_IDS_PER_KEYWORD])
            matched_ids.update(ids)
    
    # 限制总数
    MAX_TOTAL_IDS = 100
    if len(matched_ids) > MAX_TOTAL_IDS:
        matched_ids = set(list(matched_ids)[:MAX_TOTAL_IDS])
    
    # 收集需要从 SQLite 加载的 ID
    cold_ids = [mid for mid in matched_ids if mid not in memories]
    if cold_ids:
        # 批量加载冷记忆
        _batch_load_memories(cold_ids)
    
    results = []
    now = clock.now()
    for mem_id in matched_ids:
        mem = memories.get(mem_id)
        if not mem:
            continue
        delta = now - mem["last_accessed"]
        decay = 2 ** (-delta / mem["half_life"])
        decay = min(decay, 0.95)
        effective = 0.85 * decay
        if effective < RETRIEVAL_MIN_EFFECTIVE:
            continue
        results.append((effective, mem))
        access_memory(mem_id)
    
    results.sort(key=lambda x: x[0], reverse=True)
    return results[:k]

def pathfind_activation(seed_ids: list, max_stamina: float = 3.0, top_k: int = 8, max_steps: int = 2, inhibited_edges: set = None, output_visited_edges: list = None) -> list:
    """
    受限 BFS 激活扩散（双图合并版）
    - 同时使用有向链接（causal/temporal/semantic）和 faiss 弱语义链接（semantic_fast）
    - 边类型感知体力消耗
    - 每条边最多访问一次（全局 visited 边集合）
    - 每个节点最多扩展 MAX_OUT_EDGES 条最强出边（仅对 links 边剪枝，faiss 边不受限）
    - inhibited_edges: 可选的被抑制边集合 (src_id, tgt_id)，扩散时跳过
    - output_visited_edges: 若提供 list，每条遍历过的边 (src, tgt) 会追加至此
    """
    if inhibited_edges is None:
        inhibited_edges = set()
    with _data_lock:
        adj = {}
        for (src, tgt), link_data in list(links.items()):
            if (src, tgt) in inhibited_edges:
                continue
            if src not in adj:
                adj[src] = []
            w = decay_link(src, tgt)
            if w > 0:
                adj[src].append((tgt, w, link_data.get("type", "semantic")))

        for src in adj:
            adj[src].sort(key=lambda x: x[1], reverse=True)
            if len(adj[src]) > MAX_OUT_EDGES:
                adj[src] = adj[src][:MAX_OUT_EDGES]

        for seed in seed_ids:
            if seed not in memories:
                continue
            seed_vec = np.array(memories[seed]["vector"], dtype=np.float32).reshape(1, -1)
            faiss.normalize_L2(seed_vec)
            k_sem = min(5, _faiss_index.ntotal)
            if k_sem == 0:
                continue
            scores, faiss_ids = _faiss_index.search(seed_vec, k_sem + 1)
            for score, fid in zip(scores[0], faiss_ids[0]):
                if fid == -1:
                    continue
                neighbor_id = _faiss_to_mem[fid]
                if neighbor_id == seed:
                    continue
                sim = float(score)
                if sim < SIMILARITY_THRESHOLD:
                    continue
                adj.setdefault(seed, []).append((neighbor_id, sim, "semantic_fast"))
                adj.setdefault(neighbor_id, []).append((seed, sim, "semantic_fast"))

        activation = {}
        visited_edges = set()

        for seed in seed_ids:
            if seed not in memories:
                continue
            q = deque()
            q.append((seed, max_stamina, 0))
            best = {(seed, 0): max_stamina}
            while q:
                if len(q) > MAX_QUEUE_SIZE:
                    break
                cur, stamina, steps = q.popleft()
                if steps >= max_steps:
                    continue
                for tgt, current_weight, edge_type in adj.get(cur, []):
                    if tgt not in memories:
                        loaded = _load_memory_from_db(tgt)
                        if loaded is None:
                            continue
                    edge_key = (cur, tgt)
                    if edge_key in visited_edges:
                        continue
                    visited_edges.add(edge_key)
                    if output_visited_edges is not None:
                        output_visited_edges.append(edge_key)
                    factor = COST_FACTOR.get(edge_type, 1.0)
                    cost = (1.0 / current_weight) * factor
                    new_stamina = stamina - cost
                    if new_stamina < 0:
                        continue
                    new_steps = steps + 1
                    if tgt != seed and (tgt not in activation or new_stamina > activation[tgt]):
                        activation[tgt] = new_stamina
                    state_key = (tgt, new_steps)
                    if new_stamina > best.get(state_key, -1):
                        best[state_key] = new_stamina
                        q.append((tgt, new_stamina, new_steps))

        sorted_items = sorted(activation.items(), key=lambda x: x[1], reverse=True)
        result = []
        for node_id, score in sorted_items[:top_k]:
            access_memory(node_id)
            result.append((memories[node_id], score))
        return result

def semantic_dedup(content: str, time_tolerance: float = 1.0, now: float = None) -> str | None:
    """
    检查 content 是否与已有记忆高度重复。
    若已有记忆的创建时间与基准时间 now 相差超过 time_tolerance 秒（默认1秒），
    视为不同时间的记忆，不去重。只有内容相似且时间相近才返回已有记忆ID并强化。
    now: 比较基准（虚拟时间）。默认取当前时钟；传入消息发送时间时，可按消息实际时间去重。
    返回重复记忆的 ID；否则返回 None。
    """
    vec = text_to_vector(content)
    if now is None:
        now = clock.now()
    best_id = None
    best_sim = 0.0
    for mem_id, mem in memories.items():
        sim = cosine_similarity(vec, mem["vector"])
        if sim < DEDUP_THRESHOLD:
            continue
        time_diff = abs(now - mem.get("creation_time", 0))
        if time_diff > time_tolerance:
            continue
        if sim > best_sim:
            best_sim = sim
            best_id = mem_id
    if best_id:
        access_memory(best_id)  # 强化半衰期
        return best_id
    return None

def provide_for_monitor() -> int:
    return len(memories), len(links), len(wordweb)

# 模块加载时初始化 faiss 索引
_init_faiss_index()

def _purge_expired_memories(expiration_threshold=0.001):
    """
    物理删除衰减到极低水平的记忆。
    返回：被删除的记忆ID列表。
    """
    db = _get_db()
    now = clock.now()
    # 从 SQLite 中查询所有记忆的ID、last_accessed、half_life
    rows = db.execute("SELECT id, last_accessed, half_life FROM memories").fetchall()
    expired_ids = []
    for mem_id, last_accessed, half_life in rows:
        delta = now - last_accessed
        if delta < 0:
            delta = 0
        decay = 2 ** (-delta / half_life)
        if decay < expiration_threshold:
            expired_ids.append(mem_id)

    if not expired_ids:
        return []

    # 删除 SQLite 中的记录
    placeholders = ','.join('?' * len(expired_ids))
    db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", expired_ids)
    db.commit()

    # 从内存字典和热数据集合中移除
    for mem_id in expired_ids:
        if mem_id in memories:
            del memories[mem_id]
        hot_ids.discard(mem_id)

    global _count_mem_deleted
    _count_mem_deleted += len(expired_ids)

    # faiss 索引无法直接删除，稍后由调用者全量重建
    return expired_ids