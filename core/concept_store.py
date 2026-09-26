# core/concept_store.py
# ========================================================================
# 概念层：把记忆组织成「概念 → 事件段 → 记忆成员」三级结构，供定向检索。
#
# 设计动机：
#   全库向量检索要按「相似度 × 衰减 × 新鲜度」评分，但评分函数分不清"相关"
#   与"只是词汇相似"，导致「作业本空白」和「作业本一片空白」同时挤进结果。
#   概念层改为：关键词命中概念 → 取该概念下的若干事件段 → 段内按时间倒序直读，
#   全程不做相似度排序。
#
# 三级结构：
#   L1 概念「物理作业」  只存 event_ids（指向段），不存全部成员 ID
#      └ L2 事件段        每次交互一段，双重切分：静默>GAP 或累积到 CAP 条
#         └ L3 记忆成员   段内直读
#
# 时间回溯：
#   occurrences 保存该概念最近若干次出现的时间戳，配合理解层输出的时间枚举
#   （latest/recent/earlier/any）做范围筛选。系统只做数值换算，不解析时间词。
#
# 存量记忆不回填：concept_tag_ids 保持为空，旧数据照常走原有向量检索。
# ========================================================================

import json
import os
import sqlite3
import threading
import time
import uuid

import faiss
import numpy as np

from utils.monitor import append_log

DB_FILE = "data/test/memory.db"
EMBED_DIM = 768

# 事件段双重切分参数
EPISODE_GAP = 120.0        # 段内相邻记忆的真实时间间隔上限（秒）
EPISODE_CAP = 20           # 单段成员上限

# 概念归并阈值：概念层本就该归并同义，故比记忆层（0.92）宽松
CONCEPT_MERGE_THRESHOLD = 0.88

# occurrences 保留上限（避免单条记录无限增长；总数另记 total_count）
MAX_OCCURRENCES = 20

# 段选择：默认带最近几段
DEFAULT_RECENT_EPISODES = 3
MAX_RETURN_MEMBERS = 30    # 概念路单次带回成员数上限

# 时间枚举 → 回溯窗口（秒）。None 表示不限时间（按最近优先）
TIME_INTENT_WINDOWS = {
    "latest": (0, 3600),                  # 刚才/现在：1 小时内
    "recent": (0, 2 * 86400),             # 最近/这两天：2 天内
    "earlier": (3 * 86400, 14 * 86400),   # 上次/之前/上周：3~14 天前
    "any": None,                          # 还记得吗/那次：全部，最近优先
}
VALID_TIME_INTENTS = {"none", "latest", "recent", "earlier", "any"}

_lock = threading.RLock()
_db_conn = None
_concept_faiss = None      # 独立索引，与记忆 faiss 完全分离
_faiss_to_concept = []     # faiss_id → concept_id
_concept_to_faiss = {}     # concept_id → faiss_id


# ---------- 数据库 ----------
def _get_db():
    """获取（惰性初始化）概念层专用的连接与表结构。

    与 memory_engine 共用同一个 db 文件，但独立连接，避免与其 _data_lock 互相等待。
    """
    global _db_conn
    if _db_conn is None:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        _db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                vector BLOB NOT NULL,
                event_ids TEXT NOT NULL DEFAULT '[]',
                occurrences TEXT NOT NULL DEFAULT '[]',
                total_count INTEGER NOT NULL DEFAULT 0,
                member_count INTEGER NOT NULL DEFAULT 0,
                creation_time REAL NOT NULL,
                last_accessed REAL NOT NULL
            )
        """)
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_events (
                id TEXT PRIMARY KEY,
                concept_id TEXT NOT NULL,
                member_ids TEXT NOT NULL DEFAULT '[]',
                start_time REAL NOT NULL,
                end_time REAL NOT NULL
            )
        """)
        _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_events_concept ON concept_events(concept_id)")
        _db_conn.commit()
    return _db_conn


def _init_concept_faiss():
    """初始化概念 faiss 索引（概念数量少，全量内存索引足够）。"""
    global _concept_faiss, _faiss_to_concept, _concept_to_faiss
    _concept_faiss = faiss.IndexFlatIP(EMBED_DIM)
    _faiss_to_concept = []
    _concept_to_faiss = {}


def _ensure_faiss():
    if _concept_faiss is None:
        _init_concept_faiss()
        _rebuild_concept_faiss()
    return _concept_faiss


def _rebuild_concept_faiss():
    """从 SQLite 全量重建概念索引（启动时调用一次）。"""
    db = _get_db()
    rows = db.execute("SELECT id, vector FROM concepts").fetchall()
    for cid, blob in rows:
        vec = np.frombuffer(blob, dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(vec)
        _concept_faiss.add(vec)
        _concept_to_faiss[cid] = _concept_faiss.ntotal - 1
        _faiss_to_concept.append(cid)
    if rows:
        append_log(f"[概念层] 索引重建完成，共 {len(rows)} 个概念")


# ---------- 向量 ----------
def _embed(text: str):
    """取文本向量并归一化（供内积索引当余弦用）。失败返回 None。"""
    from .memory_engine import text_to_vector
    try:
        vec = np.array(text_to_vector(text), dtype=np.float32).reshape(1, -1)
    except Exception as e:
        append_log(f"[概念层] 向量化失败: {e}")
        return None
    if vec.shape[1] != EMBED_DIM:
        append_log(f"[概念层] 向量维度异常 {vec.shape[1]} != {EMBED_DIM}")
        return None
    faiss.normalize_L2(vec)
    return vec


# ---------- 概念查重 ----------
def find_similar_concept(vec) -> tuple:
    """在概念索引中找最相似概念，返回 (concept_id, score)；无概念时 (None, 0.0)。"""
    with _lock:
        idx = _ensure_faiss()
        if idx.ntotal == 0:
            return None, 0.0
        scores, ids = idx.search(vec, 1)
        if ids[0][0] == -1:
            return None, 0.0
        return _faiss_to_concept[ids[0][0]], float(scores[0][0])


# ---------- 事件段 ----------
def _load_event(event_id: str) -> dict | None:
    db = _get_db()
    row = db.execute(
        "SELECT id, concept_id, member_ids, start_time, end_time FROM concept_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if not row:
        return None
    try:
        members = json.loads(row[2])
    except (json.JSONDecodeError, TypeError):
        members = []
    return {
        "id": row[0], "concept_id": row[1], "member_ids": members,
        "start_time": row[3], "end_time": row[4],
    }


def _latest_event(concept_id: str) -> dict | None:
    """取该概念下最近的一个事件段（按 end_time 倒序）。"""
    db = _get_db()
    row = db.execute(
        "SELECT id, concept_id, member_ids, start_time, end_time FROM concept_events "
        "WHERE concept_id = ? ORDER BY end_time DESC LIMIT 1",
        (concept_id,),
    ).fetchone()
    if not row:
        return None
    try:
        members = json.loads(row[2])
    except (json.JSONDecodeError, TypeError):
        members = []
    return {
        "id": row[0], "concept_id": row[1], "member_ids": members,
        "start_time": row[3], "end_time": row[4],
    }


def _segment_members(concept_id: str, member_ids: list, now: float) -> list:
    """把一个批次的成员按双重切分规则分配到事件段。

    先按时间排序，然后：与上一段末条间隔超过 EPISODE_GAP，或当前段已达
    EPISODE_CAP 条，就开新段。返回新建段的信息列表。

    说明：成员时间从 memories 表读取（用本模块自己的连接，避免与
    memory_engine 的锁互相等待）；若某条查不到时间，则沿用当前批次时间，
    避免因单条数据缺失而把整段打散。
    """
    db = _get_db()

    timed = []
    for mid in member_ids:
        row = db.execute("SELECT creation_time FROM memories WHERE id = ?", (mid,)).fetchone()
        timed.append((mid, row[0] if row else now))
    if not timed:
        return []
    timed.sort(key=lambda x: x[1])

    # 找该概念已存在的最后一段，判断能否续接（同一次交互被拆到两批写入的情况）
    prev = _latest_event(concept_id)
    segments = []
    cur_ids, cur_start, cur_end = [], None, None

    def flush():
        if cur_ids:
            segments.append((list(cur_ids), cur_start, cur_end))

    for mid, ts in timed:
        if not cur_ids:
            cur_ids.append(mid); cur_start = ts; cur_end = ts
            continue
        if (ts - cur_end > EPISODE_GAP) or (len(cur_ids) >= EPISODE_CAP):
            flush()
            cur_ids, cur_start, cur_end = [mid], ts, ts
        else:
            cur_ids.append(mid); cur_end = ts
    flush()

    # 首段若与已有末段时间接近且未满，合并进去（同一交互的后续批次）。
    # 必须要求新批次在已有末段之后（diff >= 0），否则时间更早的批次会误并进来。
    if segments and prev:
        first_ids, first_start, first_end = segments[0]
        diff = first_start - prev["end_time"]
        if (0 <= diff <= EPISODE_GAP
                and len(prev["member_ids"]) + len(first_ids) <= EPISODE_CAP):
            merged_ids = prev["member_ids"] + first_ids
            db = _get_db()
            db.execute(
                "UPDATE concept_events SET member_ids = ?, end_time = ? WHERE id = ?",
                (json.dumps(merged_ids, ensure_ascii=False), first_end, prev["id"]),
            )
            db.commit()
            segments = segments[1:]
    return segments


def _insert_events(concept_id: str, segments: list) -> list:
    """写入事件段，返回新段 id 列表。"""
    if not segments:
        return []
    db = _get_db()
    new_ids = []
    for members, start, end in segments:
        eid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO concept_events (id, concept_id, member_ids, start_time, end_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, concept_id, json.dumps(members, ensure_ascii=False), start, end),
        )
        new_ids.append(eid)
    db.commit()
    return new_ids


# ---------- 概念写入 ----------
def record_concept(concept_name: str, member_ids: list, timestamp: float = None) -> str | None:
    """把一批记忆归入某个概念；必要时新建概念。

    concept_name 为空或成员为空时不做任何事（该批记忆不入概念层，
    与存量记忆一样只靠向量检索）。
    返回命中的 concept_id；未处理时返回 None。
    """
    if not concept_name or not str(concept_name).strip():
        return None
    member_ids = [m for m in (member_ids or []) if m]
    if not member_ids:
        return None

    name = str(concept_name).strip()
    now = time.time() if timestamp is None else float(timestamp)
    vec = _embed(name)
    if vec is None:
        return None

    with _lock:
        db = _get_db()
        cid, score = find_similar_concept(vec)

        if cid is not None and score >= CONCEPT_MERGE_THRESHOLD:
            # 归并进已有概念：追加时间戳与事件段，不替换、不丢内容
            row = db.execute(
                "SELECT occurrences, total_count, member_count FROM concepts WHERE id = ?", (cid,)
            ).fetchone()
            if row is None:
                cid = None   # 索引与实际数据不一致，退回新建
            else:
                occurrences = json.loads(row[0]) if row[0] else []
                occurrences.append(now)
                occurrences = occurrences[-MAX_OCCURRENCES:]
                db.execute(
                    "UPDATE concepts SET occurrences = ?, total_count = total_count + 1, "
                    "member_count = member_count + ?, last_accessed = ? WHERE id = ?",
                    (json.dumps(occurrences), len(member_ids), now, cid),
                )
                db.commit()
                segments = _segment_members(cid, member_ids, now)
                new_event_ids = _insert_events(cid, segments)
                if new_event_ids:
                    _append_event_ids(cid, new_event_ids)
                append_log(f"[概念层] 归并到「{name}」→ 已有概念（相似度 {score:.3f}，"
                           f"新增 {len(member_ids)} 条/{len(new_event_ids)} 段）")
                return cid

        # 新建概念
        cid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO concepts (id, name, vector, event_ids, occurrences, total_count, "
            "member_count, creation_time, last_accessed) VALUES (?, ?, ?, '[]', ?, 1, ?, ?, ?)",
            (cid, name, vec.tobytes(), json.dumps([now]), len(member_ids), now, now),
        )
        db.commit()
        segments = _segment_members(cid, member_ids, now)
        new_event_ids = _insert_events(cid, segments)
        if new_event_ids:
            _append_event_ids(cid, new_event_ids)

        # 加入索引
        idx = _ensure_faiss()
        idx.add(vec)
        _concept_to_faiss[cid] = idx.ntotal - 1
        _faiss_to_concept.append(cid)
        append_log(f"[概念层] 新建概念「{name}」（{len(member_ids)} 条/{len(new_event_ids)} 段）")
        return cid


def _append_event_ids(concept_id: str, new_event_ids: list):
    db = _get_db()
    row = db.execute("SELECT event_ids FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    if not row:
        return
    try:
        ids = json.loads(row[0]) if row[0] else []
    except (json.JSONDecodeError, TypeError):
        ids = []
    ids.extend(new_event_ids)
    db.execute("UPDATE concepts SET event_ids = ? WHERE id = ?",
               (json.dumps(ids, ensure_ascii=False), concept_id))
    db.commit()


# ---------- 概念查询 ----------
def get_concept(concept_id: str) -> dict | None:
    db = _get_db()
    row = db.execute(
        "SELECT id, name, event_ids, occurrences, total_count, member_count, "
        "creation_time, last_accessed FROM concepts WHERE id = ?", (concept_id,)
    ).fetchone()
    if not row:
        return None
    try:
        event_ids = json.loads(row[2]) if row[2] else []
    except (json.JSONDecodeError, TypeError):
        event_ids = []
    try:
        occurrences = json.loads(row[3]) if row[3] else []
    except (json.JSONDecodeError, TypeError):
        occurrences = []
    return {
        "id": row[0], "name": row[1], "event_ids": event_ids, "occurrences": occurrences,
        "total_count": row[4], "member_count": row[5],
        "creation_time": row[6], "last_accessed": row[7],
    }


def match_concept(query: str) -> tuple:
    """按名称匹配概念：先精确名包含，再用 faiss 兜底。

    返回 (concept_id, name, 命中方式)；未命中返回 (None, None, None)。
    """
    if not query or not str(query).strip():
        return None, None, None
    q = str(query).strip()
    with _lock:
        db = _get_db()
        # 1) 精确名匹配：概念名包含查询词，或查询词包含概念名
        row = db.execute(
            "SELECT id, name FROM concepts WHERE name = ? OR name LIKE ? "
            "ORDER BY last_accessed DESC LIMIT 1",
            (q, f"%{q}%"),
        ).fetchone()
        if row:
            return row[0], row[1], "exact"
        row = db.execute(
            "SELECT id, name FROM concepts WHERE ? LIKE '%' || name || '%' "
            "ORDER BY last_accessed DESC LIMIT 1",
            (q,),
        ).fetchone()
        if row:
            return row[0], row[1], "reverse"

        # 2) faiss 兜底
        vec = _embed(q)
        if vec is None:
            return None, None, None
        cid, score = find_similar_concept(vec)
        if cid is not None and score >= CONCEPT_MERGE_THRESHOLD:
            c = get_concept(cid)
            return cid, (c or {}).get("name"), "faiss"
    return None, None, None


def select_episodes(concept_id: str, time_intent: str = "none",
                    limit: int = DEFAULT_RECENT_EPISODES) -> tuple:
    """按时间意图挑选事件段，返回 (段列表, 是否发生降级)。

    时间意图是理解层给出的闭合枚举，这里只做数值换算，不解析时间词。
    筛选为空时降级为"最近若干段"，保证辉夜仍有材料可回应。
    """
    db = _get_db()
    rows = db.execute(
        "SELECT id, member_ids, start_time, end_time FROM concept_events "
        "WHERE concept_id = ? ORDER BY end_time DESC",
        (concept_id,),
    ).fetchall()
    if not rows:
        return [], False

    events = []
    for r in rows:
        try:
            members = json.loads(r[1]) if r[1] else []
        except (json.JSONDecodeError, TypeError):
            members = []
        events.append({"id": r[0], "member_ids": members, "start_time": r[2], "end_time": r[3]})

    intent = str(time_intent or "none").lower()
    if intent not in VALID_TIME_INTENTS:
        intent = "none"

    window = TIME_INTENT_WINDOWS.get(intent)

    selected = []
    degraded = False
    if window is None:
        # none（无时间指代）与 any（不确定）：最近优先，不限定时间窗
        selected = events[:limit]
    else:
        lo, hi = window
        now = time.time()
        # 事件时间是虚拟秒，需换算成真实时间再与窗口比较
        for e in events:
            real_end = _to_real(e["end_time"])
            age = now - real_end
            if lo <= age <= hi:
                selected.append(e)
                if len(selected) >= limit:
                    break
        if not selected:
            # 该时间窗内没有记录：降级取最近若干段
            selected = events[:max(1, min(2, limit))]
            degraded = True
    return selected, degraded


def _to_real(virtual_ts: float) -> float:
    """虚拟时间 → 真实 Unix 时间戳。换算失败时原样返回。"""
    try:
        from .virtual_clock import clock
        return clock.to_real_time(virtual_ts)
    except Exception:
        return virtual_ts


def fetch_members(episodes: list, max_members: int = MAX_RETURN_MEMBERS) -> list:
    """把事件段展开为记忆成员，返回 [(memory_dict, real_timestamp), ...]。

    段内按时间倒序（新→旧），段间也按时间倒序。全程不做相似度排序。
    """
    from .memory_engine import memories, _load_memory_from_db

    out = []
    for e in episodes:
        members = list(e.get("member_ids") or [])
        # 段内新→旧
        for mid in reversed(members):
            mem = memories.get(mid) or _load_memory_from_db(mid)
            if not mem:
                continue
            out.append((mem, _to_real(mem.get("creation_time", 0))))
            if len(out) >= max_members:
                return out
    return out


# ---------- 统计 ----------
def stats() -> dict:
    """供监控与调参使用。"""
    db = _get_db()
    concepts = db.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]
    events = db.execute("SELECT COUNT(*) FROM concept_events").fetchone()[0]
    members = db.execute("SELECT SUM(member_count) FROM concepts").fetchone()[0] or 0
    avg_events = db.execute(
        "SELECT AVG(cnt) FROM (SELECT COUNT(*) AS cnt FROM concept_events GROUP BY concept_id)"
    ).fetchone()[0]
    return {
        "concepts": concepts,
        "events": events,
        "covered_members": members,
        "avg_events_per_concept": round(avg_events, 2) if avg_events else 0.0,
    }


def reload_from_db():
    """重新加载概念索引（测试与迁移后使用）。"""
    global _concept_faiss, _faiss_to_concept, _concept_to_faiss
    with _lock:
        _init_concept_faiss()
        _rebuild_concept_faiss()
