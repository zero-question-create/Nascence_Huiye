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
from collections import OrderedDict

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

# 召回阈值：与合并分开。合并要求"同一件事"，召回只要"聊过相关话题"，
# 所以明显低于 0.88；误召回由 top-k、泛词黑名单与分数排序兜底。
CONCEPT_RECALL_THRESHOLD = 0.68

# 泛词：单独出现指向不了任何具体概念，不参与 reverse 匹配；
# 概念名本身是泛词时同样跳过，避免「大家」这类词抢占名额。
GENERIC_CONCEPT_WORDS = {
    "大家", "群里", "时候", "这个", "那个", "事情", "东西", "今天", "昨天",
}

# 别名命中权重：用户原话比概念名更贴近当下表达，仅次于 exact
ALIAS_MATCH_WEIGHT = 0.9

# occurrences 保留上限（避免单条记录无限增长；总数另记 total_count）
MAX_OCCURRENCES = 20

# 段选择：默认带最近几段（可被 config.concept_recall_episodes 覆盖）
DEFAULT_RECENT_EPISODES = 5
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
_last_concept_id = None    # 最近一次写入的概念，供认知循环把她的回复追加进去

# 召回每轮要把整句和多个关键词各查一次 faiss，关键词又常在轮次间重复；
# 这里缓存文本向量，避免每 3 秒对同一批词反复请求 Ollama。
_embed_cache = OrderedDict()
_embed_cache_lock = threading.Lock()
_EMBED_CACHE_MAX = 256


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
        # 与 memory_engine 的主连接共用一个库：等锁而不是立刻报 database is locked
        _db_conn.execute("PRAGMA busy_timeout=5000")
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
                last_accessed REAL NOT NULL,
                last_recalled REAL DEFAULT 0
            )
        """)
        # 旧库迁移：last_recalled 是后加的列；新库建表已带上，重复列错误直接忽略
        try:
            _db_conn.execute("ALTER TABLE concepts ADD COLUMN last_recalled REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_aliases (
                concept_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (concept_id, alias)
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
        _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_concept ON concept_aliases(concept_id)")
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

    # 把向量模型名编进键：控制面板换模型后旧缓存自然失效
    try:
        from config.api_config import config as _cfg
        model = str(_cfg.get("ollama_embed_model") or "")
    except Exception:
        model = ""
    key = f"{model}\x00{text}"
    with _embed_cache_lock:
        cached = _embed_cache.get(key)
        if cached is not None:
            _embed_cache.move_to_end(key)
            return cached
    try:
        vec = np.array(text_to_vector(text), dtype=np.float32).reshape(1, -1)
    except Exception as e:
        append_log(f"[概念层] 向量化失败: {e}")
        return None
    if vec.shape[1] != EMBED_DIM:
        append_log(f"[概念层] 向量维度异常 {vec.shape[1]} != {EMBED_DIM}")
        return None
    faiss.normalize_L2(vec)
    with _embed_cache_lock:
        _embed_cache[key] = vec
        if len(_embed_cache) > _EMBED_CACHE_MAX:
            _embed_cache.popitem(last=False)
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
def record_concept(concept_name: str, member_ids: list, timestamp: float = None,
                   alias: str = None) -> str | None:
    """把一批记忆归入某个概念；必要时新建概念。

    concept_name 为空或成员为空时不做任何事（该批记忆不入概念层，
    与存量记忆一样只靠向量检索）。
    alias 为用户原话（截 80 字），写入别名表供"换说法"以外的字面召回。
    返回命中的 concept_id；未处理时返回 None。
    """
    global _last_concept_id
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
                _write_alias(cid, alias, now)
                _last_concept_id = cid
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
        _write_alias(cid, alias, now)
        _last_concept_id = cid
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


def _write_alias(concept_id: str, alias: str, now: float):
    """把用户原话写进别名表（截 80 字）；空别名不写。"""
    text = str(alias or "").strip()[:80]
    if not text:
        return
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO concept_aliases (concept_id, alias, created_at) "
        "VALUES (?, ?, ?)",
        (concept_id, text, now),
    )
    db.commit()


def get_last_concept_id() -> str | None:
    """最近一次写入的概念 ID；认知循环据此把她的回复追加进同一概念。"""
    with _lock:
        return _last_concept_id


def append_member(concept_id: str, memory_id: str, ts: float = None) -> bool:
    """把一条记忆追加到该概念最近的事件段（她的回复走这里）。

    超过 EPISODE_CAP 就新开一段；概念不存在或参数为空时返回 False。
    时间取记忆自身的 creation_time，保证与事件段的时间口径一致。
    """
    if not concept_id or not memory_id:
        return False
    with _lock:
        if not get_concept(concept_id):
            return False
        db = _get_db()
        if ts is None:
            row = db.execute(
                "SELECT creation_time FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            ts = float(row[0]) if row else time.time()
        else:
            ts = float(ts)

        prev = _latest_event(concept_id)
        if prev is not None and len(prev["member_ids"]) < EPISODE_CAP:
            members = list(prev["member_ids"]) + [memory_id]
            db.execute(
                "UPDATE concept_events SET member_ids = ?, end_time = ? WHERE id = ?",
                (json.dumps(members, ensure_ascii=False), ts, prev["id"]),
            )
        else:
            eid = str(uuid.uuid4())
            db.execute(
                "INSERT INTO concept_events (id, concept_id, member_ids, start_time, end_time) "
                "VALUES (?, ?, ?, ?, ?)",
                (eid, concept_id, json.dumps([memory_id], ensure_ascii=False), ts, ts),
            )
            _append_event_ids(concept_id, [eid])
        db.execute(
            "UPDATE concepts SET member_count = member_count + 1 WHERE id = ?", (concept_id,)
        )
        db.commit()
    return True


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


def _keyword_list(keywords) -> list:
    out = []
    for kw in keywords or []:
        text = str(kw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


_HOW_PRIORITY = {"exact": 4, "alias": 3, "reverse": 2, "faiss": 1}


def _bump(scores: dict, cid: str, name: str, contrib: float, how: str):
    """给概念累加分数，并记住贡献最大的命中方式（供日志标识）。

    float32 的余弦可能到 1.0000001，与 exact 的 1.0 打平时按通道优先级取
    更可信的 exact/alias，日志才不会把字面命中标成 faiss。
    """
    entry = scores.get(cid)
    if entry is None:
        entry = {"id": cid, "name": name, "score": 0.0, "how": how,
                 "best": 0.0, "how_priority": 0}
        scores[cid] = entry
    entry["score"] += contrib
    priority = _HOW_PRIORITY.get(how, 0)
    if (contrib > entry["best"] + 1e-6
            or (abs(contrib - entry["best"]) <= 1e-6
                and priority > entry["how_priority"])):
        entry["best"] = contrib
        entry["how"] = how
        entry["how_priority"] = priority


def match_concepts(query_text: str, keywords: list, top_k: int = 2) -> list:
    """按整句与关键词给所有概念打分，返回分数最高的 top_k 个。

    与旧 match_concept 的 first-match-wins 不同，这里把四个通道的分数累加后
    统一排序，避免一个泛词把唯一名额抢走：
      exact   概念名包含关键词           +1.0
      alias   用户原话别名包含关键词      +0.9
      reverse 关键词包含概念名            +0.45（概念名是泛词时跳过）
      faiss   向量相似度 × 权重          整句 1.0 / 关键词 0.8，低于召回阈值不算
    返回 [{"id", "name", "score", "how"}, ...]；同分时最近被想起的概念优先。
    """
    q = str(query_text or "").strip()
    kws = _keyword_list(keywords)
    if not q and not kws:
        return []
    try:
        top_k = max(1, int(top_k or 1))
    except (TypeError, ValueError):
        top_k = 1

    with _lock:
        db = _get_db()
        try:
            rows = db.execute(
                "SELECT id, name, COALESCE(last_recalled, 0) FROM concepts"
            ).fetchall()
        except sqlite3.OperationalError:
            # 极端情况下迁移失败：退回不带 last_recalled 的查询，排序按 0 处理
            rows = [(r[0], r[1], 0.0)
                    for r in db.execute("SELECT id, name FROM concepts").fetchall()]
        if not rows:
            return []
        names = {r[0]: r[1] for r in rows}
        last_recalled = {r[0]: float(r[2] or 0.0) for r in rows}

        scores = {}

        # 1) exact：概念名包含关键词（单字词太泛，不参与）
        for cid, name in names.items():
            for kw in kws:
                if len(kw) >= 2 and kw in name:
                    _bump(scores, cid, name, 1.0, "exact")

        # 2) reverse：关键词包含概念名；概念名是泛词时不参与
        for cid, name in names.items():
            if not name or name in GENERIC_CONCEPT_WORDS:
                continue
            for kw in kws:
                if len(kw) >= 3 and kw != name and name in kw:
                    _bump(scores, cid, name, 0.45, "reverse")

        # 3) alias：用户原话被完整或部分写回这个关键词
        try:
            alias_rows = db.execute(
                "SELECT concept_id, alias FROM concept_aliases"
            ).fetchall()
        except sqlite3.OperationalError:
            alias_rows = []
        for ac_id, alias in alias_rows:
            if ac_id not in names:
                continue
            alias = str(alias or "")
            for kw in kws:
                if len(kw) < 2 or kw in GENERIC_CONCEPT_WORDS:
                    continue
                if kw == alias or kw in alias:
                    _bump(scores, ac_id, names[ac_id], ALIAS_MATCH_WEIGHT, "alias")
                    break

        # 4) faiss：整句权重 1.0，长关键词 0.8；低于召回阈值不参与
        probes = []
        if q:
            probes.append((q, 1.0))
        for kw in kws:
            if len(kw) >= 2 and kw != q:
                probes.append((kw, 0.8))
        for text, weight in probes:
            vec = _embed(text)
            if vec is None:
                continue
            cid, score = find_similar_concept(vec)
            if cid is None or cid not in names or score < CONCEPT_RECALL_THRESHOLD:
                continue
            _bump(scores, cid, names[cid], score * weight, "faiss")

        ranked = sorted(
            scores.values(),
            key=lambda m: (-m["score"], -last_recalled.get(m["id"], 0.0), m["name"]),
        )[:top_k]
        if not ranked:
            return []

        # 召回命中后更新 last_recalled，供下次同分排序与人工排查
        now = time.time()
        try:
            db.executemany(
                "UPDATE concepts SET last_recalled = ? WHERE id = ?",
                [(now, m["id"]) for m in ranked],
            )
            db.commit()
        except sqlite3.OperationalError:
            pass

        for m in ranked:
            m.pop("best", None)
            m.pop("how_priority", None)
        return ranked


def match_concept(query: str) -> tuple:
    """按名称匹配单个概念（兼容旧接口，内部走多概念打分）。

    返回 (concept_id, name, 命中方式)；未命中返回 (None, None, None)。
    """
    q = str(query or "").strip()
    if not q:
        return None, None, None
    matches = match_concepts(q, [q], top_k=1)
    if not matches:
        return None, None, None
    m = matches[0]
    return m["id"], m["name"], m["how"]


def _default_episode_limit() -> int:
    """概念路默认带回的段数：config.concept_recall_episodes 优先。"""
    try:
        from config.api_config import config
        return max(1, int(config.get("concept_recall_episodes", DEFAULT_RECENT_EPISODES)))
    except Exception:
        return DEFAULT_RECENT_EPISODES


def select_episodes(concept_id: str, time_intent: str = "none",
                    limit: int = None) -> tuple:
    """按时间意图挑选事件段，返回 (段列表, 是否发生降级)。

    时间意图是理解层给出的闭合枚举，这里只做数值换算，不解析时间词。
    筛选为空时降级为"最近若干段"，保证辉夜仍有材料可回应。
    回看类问题（earlier/any）在最近 N 段之外补上最早的一段，
    避免同一个概念聊过三次以上后，最早那段永远不再出现。
    """
    if limit is None:
        limit = _default_episode_limit()
    limit = max(1, int(limit))
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

    # 回看类问题：最近 N 段装不下全部时，额外补上该概念最早的一段。
    # events 已按 end_time 倒序，所以 events[-1] 就是最早那段。
    if intent in ("earlier", "any") and len(events) > len(selected):
        earliest = events[-1]
        if all(e["id"] != earliest["id"] for e in selected):
            selected = selected + [earliest]
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
